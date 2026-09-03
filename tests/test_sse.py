"""E3.5 SSE liveness: the stream closes at a (configurable) cap, and a reconnect
carrying last_id replays exactly the events missed during the gap."""

import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.request import Request, urlopen

from bigboss.security import token_hash
from bigboss.server import ApprovalHTTPServer
from bigboss.store import Store


def _approval(title):
    return {
        "run_id": "run_" + title,
        "harness": "codex",
        "workspace": "C:/x",
        "title": title,
        "proposed_action": {"kind": "shell_command", "command": "echo " + title},
    }


class SSELivenessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "bigboss.sqlite3")
        self.server = ApprovalHTTPServer(
            ("127.0.0.1", 0), self.store,
            token_hash("admin"), token_hash("adapter"), token_hash("123456"),
        )
        self.server.sse_cap_seconds = 1.0  # short cap so the stream closes fast
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        dev = self.store.enroll_device("Workstation", method="auto-local")
        self.device_id = dev["device_id"]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.tmp.cleanup()

    def _read_stream(self, last_id):
        """Open one SSE connection (fresh single-use token), read until it closes at
        the cap, return the list of (event_type, id) frames seen."""
        tok = self.store.create_stream_token(self.device_id)
        req = Request(f"{self.base}/api/events?stream_token={tok}&last_id={last_id}")
        frames = []
        cur = {}
        with urlopen(req, timeout=8) as resp:
            for raw in resp:  # iterates lines until the server closes at the cap
                line = raw.decode("utf-8").rstrip("\n")
                if line.startswith("id:"):
                    cur["id"] = int(line[3:].strip())
                elif line.startswith("event:"):
                    cur["event"] = line[6:].strip()
                elif line == "":  # blank line terminates a frame
                    if "id" in cur:
                        frames.append((cur.get("event"), cur["id"]))
                    cur = {}
        return frames

    def test_cap_is_configurable_and_stream_closes(self):
        start = time.monotonic()
        self._read_stream(0)  # blocks until the 1s cap closes it
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 4.0)  # closed near the 1s cap, not the 3600s default

    def test_reconnect_with_last_id_replays_only_missed_events(self):
        # Card A exists before the first connection.
        a = self.store.create_approval_request(_approval("A"))
        first = self._read_stream(0)
        self.assertTrue(any(t == "approval.created" for t, _ in first))
        seen_max = max(i for _, i in first)

        # Card B is created AFTER that stream closed (the "idle gap").
        b = self.store.create_approval_request(_approval("B"))

        # Reconnect carrying last_id → B's event replays; A's does not re-appear.
        second = self._read_stream(seen_max)
        ids = [i for _, i in second]
        self.assertTrue(all(i > seen_max for i in ids), f"stale replay: {ids} <= {seen_max}")
        types = [t for t, _ in second]
        self.assertIn("approval.created", types)  # B delivered on reconnect
        self.assertEqual(a["status"], "pending")
        self.assertEqual(b["status"], "pending")

    def test_named_heartbeat_is_emitted_when_idle(self):
        # No events; within the cap we should still see a named heartbeat frame so a
        # client watchdog can tell the socket is alive.
        self.server.sse_cap_seconds = 18.0  # long enough for one 15s heartbeat
        tok = self.store.create_stream_token(self.device_id)
        req = Request(f"{self.base}/api/events?stream_token={tok}&last_id=0")
        saw_heartbeat = False
        with urlopen(req, timeout=25) as resp:
            for raw in resp:
                if raw.decode("utf-8").strip() == "event: heartbeat":
                    saw_heartbeat = True
                    break
        self.assertTrue(saw_heartbeat)


if __name__ == "__main__":
    unittest.main()

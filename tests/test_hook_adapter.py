import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from bigboss.hook_adapter import main as hook_main
from bigboss.security import token_hash
from bigboss.server import ApprovalHTTPServer
from bigboss.store import Store


class HookAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tempdir.name) / "bigboss.sqlite3")
        self.adapter_token = "adapter_test_token"
        self.server = ApprovalHTTPServer(("127.0.0.1", 0), self.store, token_hash("admin"), token_hash(self.adapter_token), token_hash("123456"))
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.tempdir.cleanup()

    def test_codex_permission_hook_creates_approval_request(self):
        payload = {
            "session_id": "sess_codex",
            "hook_event_name": "PermissionRequest",
            "tool_name": "Bash",
            "tool_input": {"command": "git push origin main"},
            "cwd": self.tempdir.name,
        }

        code, output = self.run_hook(payload, "--mode", "approval", "--no-wait")

        self.assertEqual(code, 0)
        response = self.parse_hook_json(output)
        self.assertEqual(response["hookSpecificOutput"]["hookEventName"], "PermissionRequest")
        self.assertEqual(response["hookSpecificOutput"]["decision"]["behavior"], "deny")
        approvals = self.store.list_approvals(status="pending")
        self.assertEqual(len(approvals), 1)
        self.assertEqual(approvals[0]["run_id"], "sess_codex")

    def test_codex_post_tool_hook_creates_update_event(self):
        payload = {
            "session_id": "sess_codex",
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "python -m unittest"},
            "tool_response": {"exit_code": 0},
            "cwd": self.tempdir.name,
        }

        code, output = self.run_hook(payload, "--mode", "update")

        self.assertEqual(code, 0)
        response = self.parse_hook_json(output)
        self.assertEqual(response["systemMessage"], "BigBoss recorded update: codex Bash completed")
        events = self.store.events_after(0)
        self.assertEqual(events[-1]["event_type"], "run.updated")
        self.assertEqual(events[-1]["payload"]["run_id"], "sess_codex")

    def run_hook(self, payload, *extra_args):
        stdin = io.StringIO(json.dumps(payload))
        stdout = io.StringIO()
        args = [
            "--harness",
            "codex",
            "--url",
            self.base_url,
            "--adapter-token",
            self.adapter_token,
            *extra_args,
        ]
        with patch("sys.stdin", stdin), redirect_stdout(stdout):
            code = hook_main(args)
        return code, stdout.getvalue()

    def parse_hook_json(self, output):
        for line in reversed(output.splitlines()):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
        self.fail(f"No JSON hook response found in output: {output!r}")


if __name__ == "__main__":
    unittest.main()

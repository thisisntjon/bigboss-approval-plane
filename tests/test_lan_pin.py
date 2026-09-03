import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from bigboss.ops import acquire_pidfile, reclaim_previous_serve, release_pidfile
from bigboss.security import new_lan_pin, read_or_create_lan_pin, token_hash
from bigboss.server import ApprovalHTTPServer
from bigboss.store import Store


class LanPinTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tempdir.name) / "bigboss.sqlite3")
        self.lan_pin = "123456"
        self.server = ApprovalHTTPServer(
            ("127.0.0.1", 0),
            self.store,
            token_hash("admin"),
            token_hash("adapter"),
            token_hash(self.lan_pin),
        )
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.tempdir.cleanup()

    def request_json(self, method, path, payload=None, headers=None):
        data = None
        request_headers = {"Content-Type": "application/json"}
        request_headers.update(headers or {})
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        request = Request(f"{self.base_url}{path}", data=data, headers=request_headers, method=method)
        with urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_claim_pin_enrolls_device(self):
        claimed = self.request_json(
            "POST",
            "/api/devices/claim-pin",
            {"pin": self.lan_pin, "name": "PIN Phone"},
        )
        self.assertEqual(claimed["device_name"], "PIN Phone")
        device = self.store.authenticate_device(claimed["auth_token"])
        self.assertIsNotNone(device)

    def test_wrong_pin_rejected(self):
        with self.assertRaises(HTTPError) as raised:
            self.request_json("POST", "/api/devices/claim-pin", {"pin": "000000", "name": "Nope"})
        self.assertEqual(raised.exception.code, 401)

    def test_read_or_create_lan_pin(self):
        path = Path(self.tempdir.name) / "lan-pin.txt"
        first = read_or_create_lan_pin(path)
        second = read_or_create_lan_pin(path)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 6)
        self.assertTrue(first.isdigit())

    def test_new_lan_pin_format(self):
        pin = new_lan_pin()
        self.assertEqual(len(pin), 6)
        self.assertTrue(pin.isdigit())

    def test_reclaim_clears_stale_pidfile(self):
        pid_path = Path(self.tempdir.name) / "serve.pid"
        state_path = Path(self.tempdir.name) / "serve.state.json"
        pid_path.write_text("999999\n", encoding="utf-8")
        reclaim_previous_serve(pid_path=pid_path, state_path=state_path, port=59999)
        self.assertFalse(pid_path.exists())

    def test_revoke_device_invalidates_auth(self):
        claimed = self.request_json(
            "POST",
            "/api/devices/claim-pin",
            {"pin": self.lan_pin, "name": "Revoke Me"},
        )
        revoked = self.request_json(
            "POST",
            "/api/admin/devices/revoke",
            {"device_id": claimed["device_id"]},
            headers={"X-Admin-Token": "admin"},
        )
        self.assertIsNotNone(revoked["device"]["revoked_at"])
        self.assertIsNone(self.store.authenticate_device(claimed["auth_token"]))


if __name__ == "__main__":
    unittest.main()

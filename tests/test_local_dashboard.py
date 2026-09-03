import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from bigboss import cli
from bigboss.security import token_hash
from bigboss.server import ApprovalHTTPServer
from bigboss.store import Store


class LocalDashboardServerTests(unittest.TestCase):
    """G0-L: pair-code minting is loopback-only; auto-claim enrolls Workstation;
    loopback alone never grants device access (no auto-trust)."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tempdir.name) / "bigboss.sqlite3")
        self.admin_token = "admin_test_token"
        self.adapter_token = "adapter_test_token"
        self.server = ApprovalHTTPServer(
            ("127.0.0.1", 0),
            self.store,
            token_hash(self.admin_token),
            token_hash(self.adapter_token),
            token_hash("123456"),
        )
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.tempdir.cleanup()

    def test_pair_code_mint_rejected_when_host_is_not_loopback(self):
        self.server.public_host = "my-tunnel.localhost.run"
        try:
            with self.assertRaises(HTTPError) as raised:
                self.request_json(
                    "POST",
                    "/api/pair/codes",
                    {"device_name": "Sneaky Phone"},
                    headers={"Host": "my-tunnel.localhost.run", "X-Forwarded-Proto": "https"},
                )
            self.assertEqual(raised.exception.code, 403)
        finally:
            self.server.public_host = None

    def test_pair_page_rejected_when_host_is_not_loopback(self):
        self.server.public_host = "my-tunnel.localhost.run"
        try:
            with self.assertRaises(HTTPError) as raised:
                self.request_json(
                    "GET",
                    "/pair",
                    headers={"Host": "my-tunnel.localhost.run", "X-Forwarded-Proto": "https"},
                )
            self.assertEqual(raised.exception.code, 403)
        finally:
            self.server.public_host = None

    def test_auto_claim_enrolls_workstation_and_code_is_single_use(self):
        pair = self.store.create_pair_code(
            cli.WORKSTATION_DEVICE_NAME, ttl_seconds=cli.AUTO_CLAIM_TTL_SECONDS
        )
        claimed = self.request_json(
            "POST", "/api/devices/claim", {"code": pair["code"], "name": "Phone"}
        )
        # The code's stored name wins over the client-supplied fallback.
        self.assertEqual(claimed["device_name"], cli.WORKSTATION_DEVICE_NAME)
        self.assertTrue(claimed["auth_token"])
        self.assertTrue(claimed["csrf_token"])

        with self.assertRaises(HTTPError) as raised:
            self.request_json("POST", "/api/devices/claim", {"code": pair["code"]})
        self.assertEqual(raised.exception.code, 400)

    def test_loopback_alone_grants_no_device_access(self):
        # The governance guarantee behind auto-claim: a local process without a
        # token (e.g. a governed harness) cannot read or decide approvals.
        with self.assertRaises(HTTPError) as raised:
            self.request_json("GET", "/api/approvals")
        self.assertEqual(raised.exception.code, 401)

        approval = self.store.create_approval_request(
            {
                "harness": "codex",
                "workspace": self.tempdir.name,
                "title": "Test action",
                "proposed_action": {"kind": "shell", "command": "rm -rf /"},
            }
        )
        with self.assertRaises(HTTPError) as raised:
            self.request_json(
                "POST",
                f"/api/approvals/{approval['id']}/decide",
                {"decision": "approve_once"},
            )
        self.assertEqual(raised.exception.code, 401)

    def request_json(self, method, path, payload=None, headers=None):
        data = None
        request_headers = {"Content-Type": "application/json"}
        request_headers.update(headers or {})
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        request = Request(f"{self.base_url}{path}", data=data, headers=request_headers, method=method)
        with urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))


class ServeParserTests(unittest.TestCase):
    def test_serve_defaults_to_loopback(self):
        args = cli.build_parser().parse_args(["serve"])
        self.assertEqual(args.host, "127.0.0.1")
        self.assertFalse(args.lan)

    def test_lan_flag_and_no_open_alias(self):
        args = cli.build_parser().parse_args(["serve", "--lan", "--no-open"])
        self.assertTrue(args.lan)
        self.assertTrue(args.no_open_pair)

    def test_open_subcommand_defaults(self):
        args = cli.build_parser().parse_args(["open"])
        self.assertEqual(args.command, "open")
        self.assertEqual(args.port, 8787)

    def test_helpers(self):
        self.assertTrue(cli.host_is_loopback("127.0.0.1"))
        self.assertFalse(cli.host_is_loopback("0.0.0.0"))
        self.assertEqual(cli.dashboard_url(8787), "http://127.0.0.1:8787/")
        self.assertEqual(
            cli.dashboard_url(8787, pair_code="AB12-CD34"),
            "http://127.0.0.1:8787/?pair=AB12-CD34",
        )


if __name__ == "__main__":
    unittest.main()

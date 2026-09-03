import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from bigboss.mcp_stdio import MCPStdioServer, tool_definitions
from bigboss.registry.canonical import Candidate, reconcile_candidates
from bigboss.security import token_hash
from bigboss.server import ApprovalHTTPServer
from bigboss.store import Store


def sample_resolved():
    return reconcile_candidates([
        Candidate(
            path="C:/dev/Python/BigBoss",
            markers={".git": True, "pyproject.toml": True},
            git_commit_count=5,
            last_activity_at="2026-07-01T00:00:00Z",
            has_slug_ref=False,
            is_daemonized=False,
            purpose="approval plane",
            sources=["fs-scan"],
        ),
        Candidate(
            path="C:/dev/sampleapp",
            markers={".git": True},
            git_commit_count=23,
            last_activity_at="2026-06-21T00:00:00Z",
            has_slug_ref=False,
            is_daemonized=True,
            purpose="media control plane",
            sources=["fs-scan"],
        ),
    ])


class RegistryHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tempdir.name) / "bigboss.sqlite3")
        self.store.apply_reconciled(sample_resolved())
        self.admin_token = "admin_test_token"
        self.server = ApprovalHTTPServer(
            ("127.0.0.1", 0), self.store, token_hash(self.admin_token), token_hash("adapter_test_token"), token_hash("123456")
        )
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.tempdir.cleanup()

    def pair_device(self):
        pair = self.request_json(
            "POST",
            "/api/admin/pair-codes",
            {"device_name": "Test Phone"},
            headers={"X-Admin-Token": self.admin_token},
        )
        claimed = self.request_json("POST", "/api/devices/claim", {"code": pair["code"]})
        return {
            "Authorization": f"Bearer {claimed['auth_token']}",
            "X-BigBoss-CSRF": claimed["csrf_token"],
        }

    def test_projects_route_requires_device_auth(self):
        with self.assertRaises(HTTPError) as raised:
            self.request_json("GET", "/api/registry/projects")
        self.assertEqual(raised.exception.code, 401)

    def test_daemons_route_requires_device_auth(self):
        with self.assertRaises(HTTPError) as raised:
            self.request_json("GET", "/api/daemons")
        self.assertEqual(raised.exception.code, 401)

    def test_daemons_route_returns_service_health(self):
        headers = self.pair_device()
        data = self.request_json("GET", "/api/daemons", headers=headers)
        self.assertIn("bigboss_services", data)
        self.assertIn("registered_daemons", data)
        self.assertIn("note", data)
        names = {s["name"] for s in data["bigboss_services"]}
        self.assertEqual(names, {"lan-ingest", "cost-router", "squire-proxy"})

    def test_projects_route_lists_portfolio(self):
        headers = self.pair_device()
        data = self.request_json("GET", "/api/registry/projects", headers=headers)
        slugs = [p["slug"] for p in data["projects"]]
        self.assertEqual(slugs, ["BigBoss", "sampleapp"])  # heat order

    def test_pin_route_toggles_and_reorders(self):
        headers = self.pair_device()
        projects = self.request_json("GET", "/api/registry/projects", headers=headers)["projects"]
        sampleapp = next(p for p in projects if p["slug"] == "sampleapp")
        pinned = self.request_json(
            "POST",
            "/api/registry/pin",
            {"project_id": sampleapp["id"], "pinned": True, "pin_rank": 1},
            headers=headers,
        )
        self.assertTrue(pinned["project"]["pinned"])
        reordered = self.request_json("GET", "/api/registry/projects", headers=headers)["projects"]
        self.assertEqual(reordered[0]["slug"], "sampleapp")  # pinned floats to top

    def test_pin_route_unknown_project_is_404(self):
        headers = self.pair_device()
        with self.assertRaises(HTTPError) as raised:
            self.request_json(
                "POST", "/api/registry/pin", {"project_id": "prj_missing", "pinned": True}, headers=headers
            )
        self.assertEqual(raised.exception.code, 404)

    def test_refresh_route_runs_harvest(self):
        headers = self.pair_device()
        with patch("bigboss.server.registry_refresh") as fake_refresh:
            fake_refresh.return_value = {"discovered": 0, "updated": 2, "candidates": 3, "resolved": 2}
            data = self.request_json("POST", "/api/registry/refresh", {}, headers=headers)
        fake_refresh.assert_called_once_with(self.store)
        self.assertEqual(data["summary"]["updated"], 2)

    def request_json(self, method, path, payload=None, headers=None):
        data = None
        request_headers = {"Content-Type": "application/json"}
        request_headers.update(headers or {})
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        request = Request(f"{self.base_url}{path}", data=data, headers=request_headers, method=method)
        with urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))


class RegistryMCPTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tempdir.name) / "bigboss.sqlite3")
        self.store.apply_reconciled(sample_resolved())
        self.server = MCPStdioServer(self.store)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_tools_are_listed(self):
        names = [tool["name"] for tool in tool_definitions()]
        self.assertIn("registry_list", names)
        self.assertIn("registry_refresh", names)

    def test_registry_list_tool(self):
        response = self.server.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "registry_list", "arguments": {}}}
        )
        projects = response["result"]["structuredContent"]["projects"]
        self.assertEqual([p["slug"] for p in projects], ["BigBoss", "sampleapp"])

    def test_registry_refresh_tool(self):
        with patch("bigboss.mcp_stdio.registry_refresh") as fake_refresh:
            fake_refresh.return_value = {"discovered": 1, "updated": 1, "candidates": 4, "resolved": 2}
            response = self.server.handle_message(
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "registry_refresh", "arguments": {}}}
            )
        fake_refresh.assert_called_once_with(self.store)
        self.assertEqual(response["result"]["structuredContent"]["summary"]["discovered"], 1)


if __name__ == "__main__":
    unittest.main()

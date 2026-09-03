import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from bigboss.security import token_hash
from bigboss.server import ApprovalHTTPServer
from bigboss.store import Store


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        store = Store(Path(self.tempdir.name) / "bigboss.sqlite3")
        self.admin_token = "admin_test_token"
        self.adapter_token = "adapter_test_token"
        self.server = ApprovalHTTPServer(
            ("127.0.0.1", 0),
            store,
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

    def test_http_pair_demo_and_decide_flow(self):
        pair = self.request_json(
            "POST",
            "/api/admin/pair-codes",
            {"device_name": "Test Phone"},
            headers={"X-Admin-Token": self.admin_token},
        )
        claimed = self.request_json("POST", "/api/devices/claim", {"code": pair["code"]})
        auth_headers = {
            "Authorization": f"Bearer {claimed['auth_token']}",
            "X-BigBoss-CSRF": claimed["csrf_token"],
        }

        demo = self.request_json("POST", "/api/demo/approval", {}, headers=auth_headers)
        approval_id = demo["approval"]["id"]
        self.assertEqual(demo["approval"]["status"], "pending")

        queue = self.request_json("GET", "/api/approvals?status=pending", headers=auth_headers)
        self.assertEqual(len(queue["approvals"]), 1)

        decided = self.request_json(
            "POST",
            f"/api/approvals/{approval_id}/decide",
            {"decision": "reject", "note": "Not now"},
            headers=auth_headers,
        )
        self.assertEqual(decided["approval"]["status"], "rejected")
        self.assertEqual(decided["approval"]["latest_decision"]["note"], "Not now")

    def test_harness_update_endpoint_requires_adapter_token(self):
        update = self.request_json(
            "POST",
            "/api/harness/updates",
            {
                "run_id": "run_http_update",
                "harness": "codex",
                "workspace": self.tempdir.name,
                "title": "Running",
            },
            headers={"X-Adapter-Token": self.adapter_token},
        )
        self.assertEqual(update["update"]["run_id"], "run_http_update")

    def test_sessions_endpoint_returns_fleet_log(self):
        self.server.store.record_harness_session(
            {"harness": "codex", "vendor": "openai", "project": "a sibling project", "summary": "did stuff"})
        pair = self.request_json("POST", "/api/pair/codes", {"device_name": "Fleet Phone"})
        claimed = self.request_json("POST", "/api/devices/claim", {"code": pair["code"]})
        auth_headers = {"Authorization": f"Bearer {claimed['auth_token']}"}
        data = self.request_json("GET", "/api/sessions?days=1", headers=auth_headers)
        self.assertEqual(len(data["sessions"]), 1)
        self.assertEqual(data["sessions"][0]["harness"], "codex")

    def test_sessions_endpoint_requires_device(self):
        with self.assertRaises(HTTPError) as raised:
            self.request_json("GET", "/api/sessions")
        self.assertEqual(raised.exception.code, 401)

    def test_remote_harness_session_ingest_over_lan(self):
        created = self.request_json(
            "POST", "/api/harness/session",
            {"host": "lanbox", "harness": "codex", "vendor": "openai",
             "project": "the remote-side helper", "summary": "queue work", "session_ref": "r1"},
            headers={"X-Adapter-Token": self.adapter_token},
        )
        self.assertTrue(created["session"]["session_id"].startswith("hs_"))
        rows = self.server.store.list_harness_sessions(host="lanbox")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["host"], "lanbox")

    def test_remote_harness_session_requires_adapter_token(self):
        with self.assertRaises(HTTPError) as raised:
            self.request_json("POST", "/api/harness/session", {"host": "lanbox", "harness": "codex"})
        self.assertEqual(raised.exception.code, 401)

    def test_remote_harness_session_rejects_bad_host_and_missing_harness(self):
        with self.assertRaises(HTTPError) as bad_host:
            self.request_json("POST", "/api/harness/session",
                              {"host": "the remote host!!", "harness": "codex"},
                              headers={"X-Adapter-Token": self.adapter_token})
        self.assertEqual(bad_host.exception.code, 400)
        with self.assertRaises(HTTPError) as no_harness:
            self.request_json("POST", "/api/harness/session",
                              {"host": "lanbox"},
                              headers={"X-Adapter-Token": self.adapter_token})
        self.assertEqual(no_harness.exception.code, 400)

    def test_squire_activity_ingest_then_surfaces_in_status(self):
        self.request_json(
            "POST", "/api/squire/activity",
            {"host": "lanbox", "reported_at": "2026-07-05T21:00:00Z",
             "live_job": {"label": "TRIAGE", "elapsed_ms": 8200},
             "queue": [{"client": "job-fit", "status": "running"}], "depth": 2},
            headers={"X-Adapter-Token": self.adapter_token},
        )
        pair = self.request_json("POST", "/api/pair/codes", {"device_name": "Sq Phone"})
        claimed = self.request_json("POST", "/api/devices/claim", {"code": pair["code"]})
        auth_headers = {"Authorization": f"Bearer {claimed['auth_token']}"}
        status = self.request_json("GET", "/api/squire/status", headers=auth_headers)
        activity = status["squire"]["activity"]
        self.assertEqual(len(activity), 1)
        self.assertEqual(activity[0]["host"], "lanbox")
        self.assertIsNotNone(activity[0]["age_seconds"])

    def test_squire_activity_requires_host(self):
        with self.assertRaises(HTTPError) as raised:
            self.request_json("POST", "/api/squire/activity", {"live_job": {"label": "x"}},
                              headers={"X-Adapter-Token": self.adapter_token})
        self.assertEqual(raised.exception.code, 400)

    def test_command_poll_and_ack_over_lan(self):
        cid = self.server.store.record_remote_command({"host": "lanbox", "action": "queue.pause"})["command_id"]
        # the remote host polls with the adapter token → gets the command, marks it claimed.
        polled = self.request_json("GET", "/api/commands?host=lanbox",
                                   headers={"X-Adapter-Token": self.adapter_token})
        self.assertEqual([c["id"] for c in polled["commands"]], [cid])
        acked = self.request_json("POST", f"/api/commands/{cid}/ack",
                                  {"status": "acked", "result": {"paused": True}},
                                  headers={"X-Adapter-Token": self.adapter_token})
        self.assertEqual(acked["status"], "acked")

    def test_command_poll_requires_adapter_token(self):
        with self.assertRaises(HTTPError) as raised:
            self.request_json("GET", "/api/commands?host=lanbox")
        self.assertEqual(raised.exception.code, 401)

    def test_adapter_auth_is_required(self):
        with self.assertRaises(HTTPError) as raised:
            self.request_json("POST", "/api/harness/approval-requests", {"harness": "codex"})
        self.assertEqual(raised.exception.code, 401)

    def test_pair_page_is_loopback_only(self):
        with urlopen(f"{self.base_url}/pair", timeout=5) as response:
            body = response.read().decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertIn("/static/qrcode.js", body)
        self.assertIn("Pair your phone", body)

    def test_pair_page_trailing_slash(self):
        with urlopen(f"{self.base_url}/pair/", timeout=5) as response:
            body = response.read().decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertIn("Pair your phone", body)

    def test_desk_page_shows_pair_button(self):
        with urlopen(f"{self.base_url}/desk", timeout=5) as response:
            body = response.read().decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertIn("Pair phone", body)
        self.assertIn("show-qr-button", body)

    def test_health_includes_desk_url(self):
        with urlopen(f"{self.base_url}/api/health", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertIn("desk_url", payload)
        self.assertIn("/desk", payload["desk_url"])

    def test_loopback_pair_code_endpoint(self):
        pair = self.request_json("POST", "/api/pair/codes", {"device_name": "QR Phone"})
        self.assertIn("claim_url", pair)
        self.assertIn(pair["code"], pair["claim_url"])
        claimed = self.request_json("POST", "/api/devices/claim", {"code": pair["code"]})
        self.assertEqual(claimed["device_name"], "QR Phone")

    def test_host_header_validation_rejects_unauthorized_host(self):
        with self.assertRaises(HTTPError) as raised:
            self.request_json("GET", "/api/health", headers={"Host": "evil.com"})
        self.assertEqual(raised.exception.code, 403)

    def test_host_header_validation_allows_configured_public_host(self):
        self.server.public_host = "my-tunnel.localhost.run"
        try:
            payload = self.request_json("GET", "/api/health", headers={"Host": "my-tunnel.localhost.run", "X-Forwarded-Proto": "https"})
            self.assertTrue(payload["ok"])
        finally:
            self.server.public_host = None

    def test_loopback_endpoint_rejected_if_host_is_not_loopback(self):
        self.server.public_host = "my-tunnel.localhost.run"
        try:
            with self.assertRaises(HTTPError) as raised:
                self.request_json(
                    "POST",
                    "/api/harness/updates",
                    {
                        "run_id": "run_test_bypass",
                        "harness": "codex",
                        "workspace": self.tempdir.name,
                        "title": "Running",
                    },
                    headers={
                        "Host": "my-tunnel.localhost.run",
                        "X-Forwarded-Proto": "https",
                        "X-Adapter-Token": self.adapter_token,
                    },
                )
            self.assertEqual(raised.exception.code, 403)
        finally:
            self.server.public_host = None

    def request_json(self, method, path, payload=None, headers=None):
        data = None
        request_headers = {"Content-Type": "application/json"}
        request_headers.update(headers or {})
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        request = Request(f"{self.base_url}{path}", data=data, headers=request_headers, method=method)
        with urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def request_raw(self, method, path, body: bytes, headers=None):
        """POST a raw body (for invalid-JSON / oversized cases). Returns the urllib response."""
        request = Request(f"{self.base_url}{path}", data=body, headers=headers or {}, method=method)
        return urlopen(request, timeout=5)


class RegistryIngestRouteTests(unittest.TestCase):
    """E3.6 mode-2: POST /api/registry/ingest — LAN-accepted, adapter-token gated."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tempdir.name) / "bigboss.sqlite3")
        self.adapter_token = "adapter_test_token"
        self.server = ApprovalHTTPServer(
            ("127.0.0.1", 0),
            self.store,
            token_hash("admin_test_token"),
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

    def _bundle(self, projects=None):
        return {
            "bundle_version": 1,
            "host": "lanbox",
            "generated_at": "2026-07-02T00:00:00Z",
            "projects": projects if projects is not None else [
                {"path": "V:/developer/alpha", "markers": {".git": True, "CLAUDE.md": True},
                 "git_commit_count": 5, "purpose": "The alpha service."},
            ],
        }

    def _post(self, path, body: bytes, headers=None):
        req = Request(f"{self.base_url}{path}", data=body, headers=headers or {}, method="POST")
        return urlopen(req, timeout=5)

    def test_valid_bundle_with_token_ingests(self):
        body = json.dumps(self._bundle()).encode("utf-8")
        resp = self._post(
            "/api/registry/ingest", body,
            {"Content-Type": "application/json", "X-Adapter-Token": self.adapter_token},
        )
        self.assertEqual(resp.status, 201)
        summary = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(summary["host"], "lanbox")
        rows = {r["slug"]: r for r in self.store.list_projects(include_ambiguous=True)}
        self.assertIn("alpha", rows)
        self.assertEqual(rows["alpha"]["canonical_path"], "//lanbox/V:/developer/alpha")

    def test_missing_token_rejected(self):
        body = json.dumps(self._bundle()).encode("utf-8")
        with self.assertRaises(HTTPError) as raised:
            self._post("/api/registry/ingest", body, {"Content-Type": "application/json"})
        self.assertEqual(raised.exception.code, 401)

    def test_no_origin_machine_post_is_accepted(self):
        # Regression guard for the M2M path: a client sending no Origin must not be blocked.
        body = json.dumps(self._bundle()).encode("utf-8")
        resp = self._post(
            "/api/registry/ingest", body,
            {"Content-Type": "application/json", "X-Adapter-Token": self.adapter_token},
        )
        self.assertEqual(resp.status, 201)

    def test_invalid_json_is_400(self):
        with self.assertRaises(HTTPError) as raised:
            self._post(
                "/api/registry/ingest", b"not json{",
                {"Content-Type": "application/json", "X-Adapter-Token": self.adapter_token},
            )
        self.assertEqual(raised.exception.code, 400)

    def test_bad_bundle_is_400(self):
        body = json.dumps({"bundle_version": 99}).encode("utf-8")
        with self.assertRaises(HTTPError) as raised:
            self._post(
                "/api/registry/ingest", body,
                {"Content-Type": "application/json", "X-Adapter-Token": self.adapter_token},
            )
        self.assertEqual(raised.exception.code, 400)

    def test_oversized_body_is_413(self):
        from bigboss.server import ApprovalHandler
        original = ApprovalHandler.INGEST_MAX_BYTES
        ApprovalHandler.INGEST_MAX_BYTES = 50
        try:
            body = json.dumps(self._bundle()).encode("utf-8")  # > 50 bytes
            with self.assertRaises(HTTPError) as raised:
                self._post(
                    "/api/registry/ingest", body,
                    {"Content-Type": "application/json", "X-Adapter-Token": self.adapter_token},
                )
            self.assertEqual(raised.exception.code, 413)
        finally:
            ApprovalHandler.INGEST_MAX_BYTES = original

    def test_prune_query_full_syncs_the_host(self):
        hdr = {"Content-Type": "application/json", "X-Adapter-Token": self.adapter_token}
        # First push: two lanbox projects.
        first = self._bundle([
            {"path": "V:/developer/keep", "markers": {".git": True}},
            {"path": "V:/developer/old", "markers": {".git": True}},
        ])
        self._post("/api/registry/ingest", json.dumps(first).encode("utf-8"), hdr)
        # Second push with ?prune=1 dropping 'old' → 201 and 'old' retired.
        second = self._bundle([{"path": "V:/developer/keep", "markers": {".git": True}}])
        resp = self._post("/api/registry/ingest?prune=1", json.dumps(second).encode("utf-8"), hdr)
        self.assertEqual(resp.status, 201)
        summary = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(summary["pruned"], 1)
        visible = {r["slug"] for r in self.store.list_projects(include_ambiguous=False)}
        self.assertIn("keep", visible)
        self.assertNotIn("old", visible)

    def test_source_ip_allowlist_denies_and_allows(self):
        import os
        body = json.dumps(self._bundle()).encode("utf-8")
        os.environ["BIGBOSS_INGEST_HOSTS"] = "198.51.100.9"  # not our loopback client
        try:
            with self.assertRaises(HTTPError) as raised:
                self._post(
                    "/api/registry/ingest", body,
                    {"Content-Type": "application/json", "X-Adapter-Token": self.adapter_token},
                )
            self.assertEqual(raised.exception.code, 403)
        finally:
            del os.environ["BIGBOSS_INGEST_HOSTS"]
        os.environ["BIGBOSS_INGEST_HOSTS"] = "127.0.0.1"  # our client
        try:
            resp = self._post(
                "/api/registry/ingest", body,
                {"Content-Type": "application/json", "X-Adapter-Token": self.adapter_token},
            )
            self.assertEqual(resp.status, 201)
        finally:
            del os.environ["BIGBOSS_INGEST_HOSTS"]


if __name__ == "__main__":
    unittest.main()

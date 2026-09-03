"""P-ops.2a — read-only daemon awareness. Guards that it reuses the ecosystem registry as metadata
only (never runs status_cmd), probes BigBoss services honestly, and never raises."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bigboss import daemon_registry as dr


class DaemonRegistryTests(unittest.TestCase):
    def test_status_shape_and_never_raises(self):
        with mock.patch.object(dr, "_port_open", return_value=False):  # avoid real remote probes in tests
            st = dr.status()
        self.assertIn("bigboss_services", st)
        self.assertIn("remote_services", st)
        self.assertIn("registered_daemons", st)
        self.assertIn("note", st)
        self.assertEqual({s["name"] for s in st["bigboss_services"]},
                         {"lan-ingest", "cost-router", "squire-proxy"})
        self.assertEqual({s["name"] for s in st["remote_services"]},
                         {"squire-lmstudio", "comfyui-web", "comfyui-mcp"})  # watches the remote host's Squire + ComfyUI
        for s in st["bigboss_services"] + st["remote_services"]:
            self.assertIsInstance(s["listening"], bool)  # honest boolean, never an exception

    def test_load_registry_degrades_on_missing_or_corrupt(self):
        with mock.patch.object(dr, "_REGISTRY", Path(tempfile.gettempdir()) / "does_not_exist_xyz.json"):
            self.assertEqual(dr.load_registry(), [])

    def test_load_registry_is_metadata_only(self):
        # A registry entry with a status_cmd must be surfaced as metadata WITHOUT the status_cmd
        # (we never run registered shell commands from here).
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        reg = Path(tmp.name) / "daemons.json"
        reg.write_text(json.dumps({"careerpipeline": {
            "project": "C:/x", "status_cmd": "should-not-run", "healthy_when": "heartbeat fresh",
            "restart_elevated": True, "dashboard_url": "", "notes": "n"}}), encoding="utf-8")
        with mock.patch.object(dr, "_REGISTRY", reg):
            rows = dr.load_registry()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["name"], "careerpipeline")
        self.assertNotIn("status_cmd", row)          # never carried into the payload
        self.assertEqual(row["healthy_when"], "heartbeat fresh")
        self.assertTrue(row["restart_elevated"])

    def test_down_service_probes_false_not_raise(self):
        # Point every service probe at a closed port; must report not-listening, never raise.
        with mock.patch.object(dr, "_port_open", return_value=False):
            for s in dr.service_status():
                self.assertFalse(s["listening"])


if __name__ == "__main__":
    unittest.main()

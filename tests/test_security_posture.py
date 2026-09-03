"""Read-only security-posture view — honest evidence, never a fabricated score/all-clear."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bigboss import security_posture as sp
from bigboss.store import Store


class SecurityPostureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "bigboss.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def test_posture_shape_and_honest_summary(self):
        with mock.patch.object(sp, "_lan_listeners", return_value=[445, 3389, 8787, 9999]):
            p = sp.posture(self.store)
        for key in ("local_lan_exposure", "other_lan_ports", "unauthenticated_endpoints", "egress_audit",
                    "ungoverned_inter_project_mcp", "rulings", "summary"):
            self.assertIn(key, p)
        by_port = {e["port"]: e for e in p["local_lan_exposure"]}
        self.assertTrue(by_port[8787]["gated"])          # ecosystem port, gated
        self.assertFalse(by_port[3389]["gated"])         # RDP surfaced as ungated/notable
        self.assertEqual(by_port[3389]["label"], "Windows RDP")
        self.assertEqual(p["other_lan_ports"]["ports"], [9999])  # unknown port bucketed, not dumped inline
        self.assertIn("RDP", p["summary"])               # notable OS exposure named in the summary
        # honest framing: the trusted-LAN caveat is present, and there is NO numeric score / all-clear.
        self.assertIn("SAFE ONLY IF THE LAN IS FULLY TRUSTED", p["summary"])
        self.assertNotIn("all clear", p["summary"].lower())
        self.assertNotRegex(p["summary"], r"\b\d+\s*/\s*100\b")  # no fabricated score

    def test_degrades_when_netstat_unavailable(self):
        with mock.patch.object(sp.subprocess, "check_output", side_effect=OSError("no netstat")):
            p = sp.posture(self.store)  # must not raise
        self.assertEqual(p["local_lan_exposure"], [])  # degraded gracefully to empty
        self.assertIn("summary", p)

    def test_comfyui_mcp_flagged_code_exec(self):
        p = sp.posture(self.store)
        comfy = [e for e in p["unauthenticated_endpoints"] if e.get("name") == "comfyui-mcp"]
        self.assertTrue(comfy and comfy[0]["code_exec"])  # the sharp soft spot is called out


if __name__ == "__main__":
    unittest.main()

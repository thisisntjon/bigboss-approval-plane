"""M6.3 — the read-only agent tool registry."""
import tempfile
import unittest
from pathlib import Path

from bigboss import agent_tools
from bigboss.store import Store


class AgentToolsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "bigboss.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_registry_is_local_state(self):
        self.assertEqual(set(agent_tools.read_tool_names()),
                         {"portfolio_intel", "project_status", "squire_status", "registry_list",
                          "squire_probe", "daemon_status", "security_posture", "harness_sessions",
                          "squire_activity"})

    def test_squire_probe_degrades_when_unreachable(self):
        # Must never raise even if Squire is down — report up:false and still return the ledger.
        import urllib.request
        from unittest import mock
        with mock.patch.object(urllib.request, "urlopen", side_effect=OSError("unreachable")):
            out = agent_tools.execute("squire_probe", {}, self.store)
        self.assertIn("live", out)
        self.assertIn("ledger", out)
        self.assertIn("note", out)
        self.assertFalse(out["live"]["up"])

    def test_write_registry_is_the_gated_proposers(self):
        self.assertEqual(set(agent_tools.write_tool_names()),
                         {"propose_reprioritize", "propose_exclude", "propose_set_context",
                          "propose_remote_command", "propose_reap", "propose_daemon_restart"})
        for n in agent_tools.write_tool_names():
            self.assertIn(n, agent_tools.tool_names())  # exposed to the model

    def test_brainz_is_not_a_tool(self):
        # Load-bearing privacy guard: Brainz recall is fail-closed / display-only and must NEVER be a
        # model-callable tool until a Brainz-side gated endpoint exists.
        self.assertNotIn("brainz_search", agent_tools.tool_names())
        for spec in (agent_tools.anthropic_tools() + agent_tools.openai_tools()):
            self.assertNotIn("brainz", (spec.get("name") or str(spec)).lower())
        gem = agent_tools.gemini_tools()[0]["function_declarations"]
        self.assertFalse(any("brainz" in d["name"].lower() for d in gem))

    def test_execute_runs_and_never_raises(self):
        self.assertIsInstance(agent_tools.execute("squire_status", {}, self.store), dict)
        self.assertIn("projects", agent_tools.execute("registry_list", {}, self.store))
        self.assertIn("error", agent_tools.execute("unknown_tool", {}, self.store))
        self.assertIn("error", agent_tools.execute("project_status", {"slug": "does-not-exist"}, self.store))

    def test_vendor_schema_shapes(self):
        a = agent_tools.anthropic_tools()[0]
        self.assertIn("input_schema", a)
        o = agent_tools.openai_tools()[0]
        self.assertEqual(o["type"], "function")
        self.assertIn("parameters", o["function"])
        g = agent_tools.gemini_tools()
        self.assertIn("function_declarations", g[0])


if __name__ == "__main__":
    unittest.main()

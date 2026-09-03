"""M6.1b — the derived capability manifest. Guards that Big Boss's self-knowledge is
sourced from the real command definitions (so it can't drift) and stays accurate."""
import unittest

from bigboss import capabilities as cap
from bigboss.chat import CHAT_COMMANDS, HELP_TEXT


class CapabilityManifestTests(unittest.TestCase):
    def test_cli_commands_derived_from_argparse(self):
        cmds = cap.cli_commands()
        names = [n for n, _h in cmds]
        # A representative spread across areas must be present (derived, not hand-listed).
        for expected in ("serve", "council-ask", "chat", "registry-refresh", "ps", "reap",
                         "squire-status", "brainz-recall", "council-research"):
            self.assertIn(expected, names)
        self.assertGreaterEqual(len(cmds), 30)

    def test_mcp_tools_derived(self):
        tools = [n for n, _d in cap.mcp_tools()]
        self.assertIn("portfolio_intel", tools)
        self.assertIn("bigboss_request_approval", tools)
        self.assertGreaterEqual(len(tools), 10)

    def test_every_cli_command_lands_in_a_named_area(self):
        # No known command should fall through to "Other" — keeps the grouping honest.
        strays = [n for n, _h in cap.cli_commands() if cap._area_for(n) == "Other"]
        self.assertEqual(strays, [], f"ungrouped commands: {strays}")

    def test_chat_commands_single_source(self):
        # capabilities reads the same table the REPL + HELP_TEXT use.
        self.assertEqual(cap.chat_commands(), list(CHAT_COMMANDS))
        for cmd, _desc in CHAT_COMMANDS:
            self.assertIn(cmd, HELP_TEXT)

    def test_block_is_accurate_and_bounded(self):
        block = cap.build_capabilities_block()
        self.assertIn("claude-fable-5", block)          # correct throne identity
        self.assertIn("Squire", block)                   # subsystem glossary present
        self.assertNotIn("200k", block)                  # no invented token budget
        self.assertLess(len(block), 8000)                # bounded (now carries real help strings)
        # commands actually appear
        self.assertIn("registry-refresh", block)
        self.assertIn("/recall", block)

    def test_real_help_strings_present_not_names_only(self):
        # M6.1c — the manifest must carry the REAL argparse help so the model can't confabulate
        # (the eval caught it inventing wrong descriptions for these when only names were shown).
        block = cap.build_capabilities_block()
        # substrings taken from the actual registered help strings
        for expected in ("metering proxy",          # squire-proxy
                         "verified-claim accuracy",  # council-leaderboard
                         "Dry-run",                  # reap
                         "app-server"):              # codex-run
            self.assertIn(expected, block, f"real help missing: {expected!r}")


if __name__ == "__main__":
    unittest.main()

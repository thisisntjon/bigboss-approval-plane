"""Regression: the CLI must not crash on non-ASCII output under a cp1252 console.

The suite normally runs UTF-8, so this test explicitly simulates a Windows cp1252
stdout — the environment where `registry-list`/`squire-egress` used to raise
UnicodeEncodeError on an emoji purpose or a `→` in egress text (system audit 2026-07-06).
"""
import io
import sys
import tempfile
import unittest
from pathlib import Path

from bigboss import cli
from bigboss.store import Store


class CliEncodingTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tempdir.name)
        # Seed a project whose purpose carries an emoji — the exact shape that crashed.
        store = Store(self.data_dir / "bigboss.sqlite3")
        self._seed_emoji_project(store)

    def tearDown(self):
        self.tempdir.cleanup()

    def _seed_emoji_project(self, store):
        # Insert directly so the test doesn't depend on a harvest; mirror a real row.
        now = "2026-07-06T00:00:00Z"
        with store.connect() as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(projects)").fetchall()}
            self.assertIn("purpose", cols)
            conn.execute(
                "INSERT INTO projects (id, slug, name, canonical_path, kind, purpose, "
                "status, first_seen_at, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("proj_emoji", "aimemory", "aimemory", "C:/x/aimemory", "code",
                 "Chat Memory System \U0001f9e0", "active", now, now, now),
            )

    def _run_under_cp1252(self, *argv):
        """Run cli.main with stdout/stderr backed by a strict cp1252 buffer."""
        out = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict", newline="")
        err = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict", newline="")
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            code = cli.main(["--data-dir", str(self.data_dir), *argv])
        finally:
            sys.stdout, sys.stderr = old_out, old_err
        return code

    def test_registry_list_survives_emoji_under_cp1252(self):
        # Before the fix this raised UnicodeEncodeError ('charmap' can't encode U+1F9E0).
        code = self._run_under_cp1252("registry-list", "--all")
        self.assertEqual(code, 0)

    def test_main_reconfigures_streams_to_utf8(self):
        cli._force_utf8_streams()
        # After the entry hook, real stdout is UTF-8 (in the test runner it already is).
        self.assertEqual((sys.stdout.encoding or "").lower().replace("-", ""), "utf8")


if __name__ == "__main__":
    unittest.main()

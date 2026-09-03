"""parse_json salvage — recover truncated model JSON so a seat that hit its output cap still
contributes instead of being silently dropped (Council seat-reliability hardening)."""

import unittest

from bigboss.council.parse import parse_json


class ParseRepairTests(unittest.TestCase):
    def test_clean_json_still_parses(self):
        self.assertEqual(parse_json('{"a": 1, "b": [2, 3]}'), {"a": 1, "b": [2, 3]})

    def test_fenced_json_still_parses(self):
        self.assertEqual(parse_json('```json\n{"ok": true}\n```'), {"ok": True})

    def test_truncated_array_keeps_complete_items(self):
        # a ranking cut off mid-fourth-item — the three complete items survive
        truncated = ('{"ranking": [{"slug": "a", "reason": "x"}, {"slug": "b", "reason": "y"}, '
                     '{"slug": "c", "reason": "z"}, {"slug": "d", "reas')
        got = parse_json(truncated)
        self.assertIsNotNone(got)
        slugs = [r["slug"] for r in got["ranking"]]
        self.assertEqual(slugs, ["a", "b", "c"])

    def test_truncated_inside_string_closes_it(self):
        got = parse_json('{"summary": "a long answer that got cut of')
        self.assertIsNotNone(got)
        self.assertIn("summary", got)

    def test_trailing_comma_after_truncation(self):
        got = parse_json('{"items": [1, 2, 3],')
        self.assertEqual(got, {"items": [1, 2, 3]})

    def test_unrecoverable_returns_none(self):
        self.assertIsNone(parse_json("not json at all"))
        self.assertIsNone(parse_json(""))
        self.assertIsNone(parse_json(None))


if __name__ == "__main__":
    unittest.main()

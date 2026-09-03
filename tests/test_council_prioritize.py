"""M2 — project prioritization: Borda aggregation, portfolio-context builder, and the
approve-effect that turns a ratified ranking into pin_rank. M2.1 adds the ideation harvest
(synergy capture, preserved per-seat reasons, the consolidation pass). No live spend."""

import json
import tempfile
import unittest
from pathlib import Path

from bigboss.council import prioritize
from bigboss.council.prioritize import (
    IDEATION_SYSTEM,
    _borda,
    _clean_synergies,
    _collect_reasons,
    build_portfolio_context,
    run_ideation_pass,
    run_prioritization,
)
from bigboss.registry.canonical import ResolvedProject
from bigboss.store import Store


def _seat_reply(answer, status="success", usage=None):
    return {"id": "x", "name": "x", "provider": "x", "model": "x", "status": status,
            "answer": answer, "usage": usage or {"input": 10, "output": 5}, "latency_ms": 1}


def _rp(slug, status="active", commits=1):
    return ResolvedProject(
        canonical_path=f"C:/dev/{slug}", slug=slug, family=slug, markers={".git": True},
        git_commit_count=commits, last_activity_at="2026-06-01T00:00:00Z", status=status,
        purpose=f"{slug} purpose", domain="", sources=["fs-scan"], aliases=[],
    )


class BordaTests(unittest.TestCase):
    def test_borda_rewards_broadly_and_highly_ranked(self):
        per_seat = {
            "claude": ["a", "b", "c"],
            "gpt": ["b", "a", "d"],
            "gemini": ["a", "c", "b"],
        }
        # a: (3)+(2)+(3)=8 ; b: (2)+(3)+(1)=6 ; c: (1)+0+(2)=3 ; d: 0+(1)+0=1
        self.assertEqual(_borda(per_seat, 3), ["a", "b", "c"])

    def test_borda_caps_to_top_n(self):
        per_seat = {"x": ["a", "b", "c", "d", "e"], "y": ["a", "b", "c", "d", "e"]}
        self.assertEqual(_borda(per_seat, 2), ["a", "b"])

    def test_borda_deterministic_tiebreak(self):
        per_seat = {"x": ["a", "b"], "y": ["b", "a"]}  # a and b both 3 pts, 2 seats each -> slug order
        self.assertEqual(_borda(per_seat, 2), ["a", "b"])


class ContextBuilderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "bigboss.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def test_skips_ambiguous_and_gone_includes_intel(self):
        self.store.apply_reconciled([_rp("keep"), _rp("parent", status="ambiguous")])
        # add a gone project via prune
        self.store.apply_reconciled([_rp("stale")])
        self.store.apply_reconciled([_rp("keep")], prune_local=True)  # 'stale','parent' absent -> pruned/gone
        # give 'keep' some intel
        pid = next(p["id"] for p in self.store.list_projects(include_ambiguous=True) if p["slug"] == "keep")
        self.store.upsert_project_intel(pid, {
            "status_line": "shipping", "blockers": ["needs review"], "roadmap_now": "phase X",
            "roadmap_next": "", "source_hash": "h", "source_files": ["PLAN.md"], "model": "gemma",
        })
        ctx = build_portfolio_context(self.store)
        slugs = {c["slug"] for c in ctx}
        self.assertIn("keep", slugs)
        self.assertNotIn("parent", slugs)  # ambiguous skipped
        self.assertNotIn("stale", slugs)   # gone skipped
        keep = next(c for c in ctx if c["slug"] == "keep")
        self.assertEqual(keep["status"], "shipping")
        self.assertEqual(keep["blockers"], ["needs review"])


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "bigboss.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def _get(self, slug):
        return next(p for p in self.store.list_projects(include_ambiguous=True) if p["slug"] == slug)

    def test_set_baseline_writes_and_surfaces_in_context(self):
        self.store.apply_reconciled([_rp("cap")])
        self.store.set_baseline("cap", purpose="submitted capstone", lifecycle="done", ownership="personal")
        p = self._get("cap")
        self.assertEqual((p["lifecycle"], p["ownership"], p["purpose"]), ("done", "personal", "submitted capstone"))
        ctx = next(c for c in build_portfolio_context(self.store) if c["slug"] == "cap")
        self.assertEqual(ctx["lifecycle"], "done")
        self.assertEqual(ctx["ownership"], "personal")

    def test_baseline_durable_across_registry_refresh(self):
        self.store.apply_reconciled([_rp("proj")])
        self.store.set_baseline("proj", purpose="real purpose", lifecycle="active", ownership="work")
        self.store.apply_reconciled([_rp("proj")])  # refresh; project still on disk
        p = self._get("proj")
        self.assertEqual((p["lifecycle"], p["ownership"], p["purpose"]), ("active", "work", "real purpose"))

    def test_baseline_validates_enum_values(self):
        self.store.apply_reconciled([_rp("p")])
        with self.assertRaises(ValueError):
            self.store.set_baseline("p", lifecycle="finished")   # not a valid lifecycle
        with self.assertRaises(ValueError):
            self.store.set_baseline("p", ownership="employer")   # not a valid ownership

    def test_archived_at_stamped_on_completion(self):
        self.store.apply_reconciled([_rp("d")])
        self.assertIsNone(self._get("d")["archived_at"])
        self.store.set_baseline("d", lifecycle="archived")
        self.assertIsNotNone(self._get("d")["archived_at"])

    def test_baseline_unknown_slug_raises(self):
        with self.assertRaises(KeyError):
            self.store.set_baseline("ghost", lifecycle="active")


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "bigboss.sqlite3")
        self.store.apply_reconciled([_rp("child"), _rp("parent")])

    def tearDown(self):
        self.tmp.cleanup()

    def _get(self, slug):
        return next(p for p in self.store.list_projects(include_ambiguous=True) if p["slug"] == slug)

    def test_set_context_writes_notes_lineage_trackit(self):
        self.store.set_context("child", notes="the operator's own words", evolved_into=["parent"],
                               track_it=True, track_note="sensitive")
        p = self._get("child")
        self.assertEqual(p["notes"], "the operator's own words")
        self.assertEqual(p["evolved_into"], "parent")   # list normalized to comma-string
        self.assertTrue(p["track_it"])
        self.assertEqual(p["track_note"], "sensitive")

    def test_context_durable_across_refresh(self):
        self.store.set_context("child", notes="keep me", evolved_into="parent", track_it=True)
        self.store.apply_reconciled([_rp("child"), _rp("parent")])  # refresh
        p = self._get("child")
        self.assertEqual(p["notes"], "keep me")
        self.assertEqual(p["evolved_into"], "parent")
        self.assertTrue(p["track_it"])

    def test_context_surfaces_in_prioritization_context(self):
        self.store.set_context("child", notes="hi", supersedes="parent", track_it=True)
        ctx = next(c for c in build_portfolio_context(self.store) if c["slug"] == "child")
        self.assertEqual(ctx["notes"], "hi")
        self.assertEqual(ctx["supersedes"], "parent")
        self.assertTrue(ctx["track_it"])

    def test_set_context_requires_a_field(self):
        with self.assertRaises(ValueError):
            self.store.set_context("child")

    def test_set_context_unknown_slug_raises(self):
        with self.assertRaises(KeyError):
            self.store.set_context("ghost", notes="x")


class ExcludeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "bigboss.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def test_excluded_project_drops_from_prioritization_context(self):
        self.store.apply_reconciled([_rp("keep"), _rp("done"), _rp("work")])
        self.store.set_excluded("done", True)      # e.g. submitted capstone
        self.store.set_excluded("work", True)      # e.g. a work ticketing system
        slugs = {c["slug"] for c in build_portfolio_context(self.store)}
        self.assertEqual(slugs, {"keep"})

    def test_exclude_is_reversible_and_durable_across_refresh(self):
        self.store.apply_reconciled([_rp("proj")])
        self.store.set_excluded("proj", True)
        # a registry-refresh (project still on disk) must NOT silently re-include it
        self.store.apply_reconciled([_rp("proj")])
        self.assertTrue(next(p for p in self.store.list_projects() if p["slug"] == "proj")["excluded"])
        self.store.set_excluded("proj", False)
        self.assertFalse(next(p for p in self.store.list_projects() if p["slug"] == "proj")["excluded"])

    def test_exclude_unknown_slug_raises(self):
        with self.assertRaises(KeyError):
            self.store.set_excluded("nope", True)


class ApproveEffectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "bigboss.sqlite3")
        self.store.apply_reconciled([_rp("alpha"), _rp("beta"), _rp("gamma"), _rp("delta")])
        d = self.store.enroll_device("Workstation", method="auto-local")
        self.device = {"id": d["device_id"], "name": d["device_name"]}

    def tearDown(self):
        self.tmp.cleanup()

    def _pins(self):
        return {p["slug"]: p["pin_rank"] for p in self.store.list_projects(include_ambiguous=True) if p["pinned"]}

    def _card(self, order):
        return self.store.create_approval_request({
            "harness": "bigboss", "title": "prioritize",
            "proposed_action": {"kind": "council_prioritization", "order": order},
        })

    def test_approve_sets_pin_rank_and_unpins_rest(self):
        # pre-pin gamma to prove it gets cleared
        gid = next(p["id"] for p in self.store.list_projects(include_ambiguous=True) if p["slug"] == "gamma")
        self.store.set_pin(gid, pinned=True, pin_rank=1)
        card = self._card(["beta", "alpha"])
        self.store.resolve_approval(card["id"], "approve_once", "", self.device)
        self.assertEqual(self._pins(), {"beta": 1, "alpha": 2})  # gamma unpinned; ranking applied

    def test_reject_leaves_pins_unchanged(self):
        aid = next(p["id"] for p in self.store.list_projects(include_ambiguous=True) if p["slug"] == "alpha")
        self.store.set_pin(aid, pinned=True, pin_rank=1)
        card = self._card(["beta", "gamma"])
        self.store.resolve_approval(card["id"], "reject", "not now", self.device)
        self.assertEqual(self._pins(), {"alpha": 1})  # unchanged

    def test_reprioritized_event_emitted(self):
        card = self._card(["alpha"])
        self.store.resolve_approval(card["id"], "approve_once", "", self.device)
        types = {e["event_type"] for e in self.store.events_after(0, limit=300)}
        self.assertIn("priority.reprioritized", types)


class SynergyCaptureTests(unittest.TestCase):
    def test_clean_synergies_filters_unknown_and_singletons(self):
        valid = {"a", "b", "c"}
        raw = [
            {"projects": ["a", "b"], "insight": "shared infra"},   # keep
            {"projects": ["a", "zzz"], "insight": "one known"},     # -> 1 valid slug -> drop
            {"projects": ["a", "b"], "insight": ""},                # empty insight -> drop
            {"projects": ["a", "a", "c"], "insight": "dedup a"},    # de-dup -> [a,c] keep
            "not a dict",                                            # drop
        ]
        out = _clean_synergies(raw, valid)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0], {"projects": ["a", "b"], "insight": "shared infra"})
        self.assertEqual(out[1], {"projects": ["a", "c"], "insight": "dedup a"})

    def test_collect_reasons_preserves_every_seat(self):
        seat_results = [
            {"seat": "claude", "reasons": {"a": "claude take", "b": "cb"}},
            {"seat": "gpt", "reasons": {"a": "gpt take"}},
            {"seat": "gemini", "reasons": {}},
        ]
        detail = _collect_reasons(seat_results, ["a", "b"])
        self.assertEqual([t["seat"] for t in detail["a"]], ["claude", "gpt"])  # both, not flattened
        self.assertEqual(detail["b"], [{"seat": "claude", "reason": "cb"}])


class IdeationPassTests(unittest.TestCase):
    def setUp(self):
        self._orig = prioritize.call_seat

    def tearDown(self):
        prioritize.call_seat = self._orig

    def test_ideation_pass_parses_structured_output(self):
        payload = {
            "synergies": [{"projects": ["a", "b"], "insight": "a feeds b"}],
            "opportunities": [{"idea": "combine a+b into a product", "projects": ["a", "b"]}],
            "tensions": [{"topic": "focus", "detail": "a vs b for time"}],
            "brief": "The portfolio's spine is a; b amplifies it.",
        }
        calls = {}

        def fake(sid, system, prompt, max_tokens=0):
            calls["system"] = system
            return _seat_reply(json.dumps(payload), usage={"input": 100, "output": 40})

        prioritize.call_seat = fake
        usage = {"input": 0, "output": 0}
        result = run_ideation_pass(
            ["a", "b"], {"a": [{"seat": "claude", "reason": "r"}]},
            [{"projects": ["a", "b"], "insight": "raw", "seat": "gpt"}], [], {},
            {"a", "b"}, "claude", usage,
        )
        self.assertIn("synthesis lead", calls["system"])          # used the ideation system prompt
        self.assertEqual(result["synthesized_by"], "claude")
        self.assertFalse(result["degraded"])
        self.assertEqual(result["brief"], payload["brief"])
        self.assertEqual(len(result["opportunities"]), 1)
        self.assertEqual(len(result["tensions"]), 1)
        self.assertEqual(usage, {"input": 100, "output": 40})     # usage accumulated

    def test_ideation_pass_degrades_to_raw_synergies_on_failure(self):
        def fake(sid, system, prompt, max_tokens=0):
            return _seat_reply(None, status="error")

        prioritize.call_seat = fake
        result = run_ideation_pass(
            ["a"], {}, [{"projects": ["a", "b"], "insight": "raw", "seat": "gpt"}], [], {},
            {"a", "b"}, "claude", {"input": 0, "output": 0},
        )
        self.assertTrue(result["degraded"])
        self.assertEqual(result["brief"], "")
        self.assertEqual(result["synergies"], [{"projects": ["a", "b"], "insight": "raw"}])


class RunPrioritizationEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "bigboss.sqlite3")
        self.store.apply_reconciled([_rp("alpha"), _rp("beta"), _rp("gamma")])
        self._orig = prioritize.call_seat

    def tearDown(self):
        prioritize.call_seat = self._orig
        self.tmp.cleanup()

    def test_run_prioritization_captures_ideation_and_preserves_reasons(self):
        rank_json = json.dumps({
            "ranking": [{"slug": "alpha", "reason": "momentum"}, {"slug": "beta", "reason": "unblocks"},
                        {"slug": "gamma", "reason": "leverage"}],
            "synergies": [{"projects": ["alpha", "beta"], "insight": "alpha unblocks beta"}],
            "overall": "alpha leads.",
        })
        ideation_json = json.dumps({
            "synergies": [{"projects": ["alpha", "beta"], "insight": "consolidated: alpha feeds beta"}],
            "opportunities": [{"idea": "ship alpha then beta", "projects": ["alpha", "beta"]}],
            "tensions": [], "brief": "Focus alpha, then beta.",
        })

        def fake(sid, system, prompt, max_tokens=0):
            if "synthesis lead" in system:
                return _seat_reply(ideation_json)
            return _seat_reply(rank_json)

        prioritize.call_seat = fake
        result = run_prioritization(self.store, top_n=3, seats=["claude", "gpt", "gemini"])
        self.assertEqual(result["consensus"], ["alpha", "beta", "gamma"])
        # every seat's take is preserved, not flattened to one
        self.assertEqual(len(result["reasons_detail"]["alpha"]), 3)
        self.assertEqual(result["ideation"]["brief"], "Focus alpha, then beta.")
        self.assertEqual(result["ideation"]["synthesized_by"], "claude")
        self.assertTrue(result["ideation"]["synergies"])

    def test_ideation_persists_to_council_sessions(self):
        ideation = {"synergies": [{"projects": ["alpha", "beta"], "insight": "x"}], "brief": "b"}
        self.store.record_council_session({
            "mode": "prioritization", "seats": ["claude", "gpt"],
            "final": {"final_answer": "b"}, "usage": {"input": 1, "output": 1},
            "ideation": ideation,
        })
        with self.store.connect() as con:
            row = con.execute(
                "SELECT ideation_json FROM council_sessions WHERE mode='prioritization' "
                "ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(json.loads(row[0])["brief"], "b")


if __name__ == "__main__":
    unittest.main()

"""M6.1b — the Council's optional self/portfolio context. Without it, seats treat
"Squire" as a medieval attendant; with it, the ecosystem context reaches the answer-
generating rounds (round 1 + synthesis) but is NOT re-spammed into every stage."""
import unittest
from unittest.mock import patch

from bigboss.council import engine

_MARKER = "SQUIRE_IS_THE_REMOTE_ENDPOINT_XYZ"


def _recorder():
    calls = []

    def fake_call_seat(sid, system, user, max_tokens=0):
        calls.append({"sid": sid, "system": system})
        return {"id": sid, "name": sid, "provider": "test", "model": "m",
                "answer": "Primary recommendation: drive.", "status": "success",
                "usage": {"input": 1, "output": 1}}

    return calls, fake_call_seat


class CouncilContextTests(unittest.TestCase):
    def test_context_reaches_round1_but_not_every_stage(self):
        calls, fake = _recorder()
        with patch.object(engine, "call_seat", fake):
            engine.run_live_council("walk or drive?", seats=["claude", "gpt"], context=_MARKER)

        round1 = [c for c in calls if "Council role" in c["system"]]
        self.assertTrue(round1, "no round-1 seat calls captured")
        self.assertTrue(all(_MARKER in c["system"] for c in round1),
                        "context missing from a round-1 seat prompt")
        # Bounded injection: at least one later stage (critique/vote/verify) must NOT carry it.
        self.assertTrue(any(_MARKER not in c["system"] for c in calls),
                        "context was spammed into every call")

    def test_no_context_leaves_prompts_clean(self):
        calls, fake = _recorder()
        with patch.object(engine, "call_seat", fake):
            engine.run_live_council("walk or drive?", seats=["claude", "gpt"], context=None)
        self.assertTrue(all("Ecosystem context" not in c["system"] for c in calls))


if __name__ == "__main__":
    unittest.main()

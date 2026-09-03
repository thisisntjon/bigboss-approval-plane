"""M6.3 — the multi-vendor read-only tool loop. Mocks the shared HTTP transport (providers._http_post)
so no keys/spend; asserts each vendor's tool request/parse and the loop's execute-and-feed-back + cap."""
import os
import tempfile
import unittest
from pathlib import Path

from bigboss import chat
from bigboss.council import providers
from bigboss.store import Store


class _Upstream:
    """Returns queued vendor responses in order; records the request bodies it saw."""
    def __init__(self, responses):
        self.responses = list(responses)
        self.bodies = []

    def __call__(self, url, headers, body, timeout):
        self.bodies.append(body)
        return self.responses.pop(0)


class ToolLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "bigboss.sqlite3")
        self._orig_post = providers._http_post
        self._orig_loaded = providers._ENV_LOADED
        providers._ENV_LOADED = True  # stop _ensure_env from clobbering the test keys
        self._keys = {}
        for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "GROK_API_KEY", "XAI_API_KEY"):
            self._keys[k] = os.environ.get(k)
            os.environ[k] = "test-key"

    def tearDown(self):
        providers._http_post = self._orig_post
        providers._ENV_LOADED = self._orig_loaded
        for k, v in self._keys.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def _patch(self, responses):
        up = _Upstream(responses)
        providers._http_post = up
        return up

    def test_anthropic_executes_tool_then_answers(self):
        self._patch([
            {"content": [{"type": "tool_use", "id": "tu1", "name": "squire_status", "input": {}}],
             "stop_reason": "tool_use", "usage": {"input_tokens": 10, "output_tokens": 5}},
            {"content": [{"type": "text", "text": "Squire is up."}],
             "stop_reason": "end_turn", "usage": {"input_tokens": 12, "output_tokens": 4}},
        ])
        r = chat.run_tool_loop(self.store, [{"role": "user", "content": "squire status?"}], seat="claude")
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["answer"], "Squire is up.")
        self.assertIn("squire_status", r["tools_used"])
        self.assertEqual(r["rounds"], 1)
        self.assertFalse(r["multistep"])
        self.assertEqual(r["usage"], {"input": 22, "output": 9})

    def test_openai_tool_call_parses_json_string_args(self):
        self._patch([
            {"choices": [{"message": {"content": None, "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "project_status", "arguments": "{\"slug\": \"bigboss\"}"}}]},
                "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 8, "completion_tokens": 3}},
            {"choices": [{"message": {"content": "Here is bigboss."}}],
             "usage": {"prompt_tokens": 9, "completion_tokens": 6}},
        ])
        r = chat.run_tool_loop(self.store, [{"role": "user", "content": "status of bigboss?"}], seat="gpt")
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["answer"], "Here is bigboss.")
        self.assertIn("project_status", r["tools_used"])

    def test_gemini_functioncall_loop(self):
        self._patch([
            {"candidates": [{"content": {"role": "model", "parts": [
                {"functionCall": {"name": "registry_list", "args": {}}}]}}],
             "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 2}},
            {"candidates": [{"content": {"parts": [{"text": "You have projects."}]}}],
             "usageMetadata": {"promptTokenCount": 8, "candidatesTokenCount": 5}},
        ])
        r = chat.run_tool_loop(self.store, [{"role": "user", "content": "list projects"}], seat="gemini")
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["answer"], "You have projects.")
        self.assertIn("registry_list", r["tools_used"])

    def test_hop_cap_and_multistep_flag(self):
        # A seat that never stops calling tools must be capped, and flagged multistep.
        loop_reply = {"content": [{"type": "tool_use", "id": "tu", "name": "registry_list", "input": {}}],
                      "stop_reason": "tool_use", "usage": {"input_tokens": 1, "output_tokens": 1}}
        self._patch([dict(loop_reply) for _ in range(10)])
        r = chat.run_tool_loop(self.store, [{"role": "user", "content": "loop"}], seat="claude", max_hops=2)
        self.assertTrue(r.get("capped"))
        self.assertEqual(r["rounds"], 2)
        self.assertTrue(r["multistep"])

    def test_no_tool_call_is_plain_answer(self):
        self._patch([
            {"content": [{"type": "text", "text": "Hi there."}], "stop_reason": "end_turn",
             "usage": {"input_tokens": 5, "output_tokens": 2}},
        ])
        r = chat.run_tool_loop(self.store, [{"role": "user", "content": "hi"}], seat="claude")
        self.assertEqual(r["answer"], "Hi there.")
        self.assertEqual(r["rounds"], 0)
        self.assertEqual(r["tools_used"], [])


if __name__ == "__main__":
    unittest.main()

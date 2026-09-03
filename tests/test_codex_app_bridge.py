import io
import json
import unittest

from bigboss.codex_app_bridge import CodexAppBridge, DECISION_MAP


def _script(messages):
    return io.BytesIO(b"".join(json.dumps(m).encode("utf-8") + b"\n" for m in messages))


def _written(writer):
    return [json.loads(line) for line in writer.getvalue().split(b"\n") if line.strip()]


class CodexAppBridgeTests(unittest.TestCase):
    def _run(self, decision_fn, approval_params=None):
        approval_params = approval_params or {
            "command": "echo hi",
            "cwd": "/repo",
            "threadId": "thr_test",
            "turnId": "turn_1",
            "itemId": "item_1",
        }
        server_messages = [
            {"id": 0, "result": {"userAgent": "x"}},
            {"id": 1, "result": {"thread": {"id": "thr_test"}}},
            {"method": "item/commandExecution/requestApproval", "id": 100, "params": approval_params},
            {
                "method": "turn/completed",
                "params": {"threadId": "thr_test", "turn": {"items": [{"type": "agentMessage", "text": "done"}]}},
            },
        ]
        writer = io.BytesIO()
        bridge = CodexAppBridge(writer, _script(server_messages), decision_fn)
        result = bridge.run("do the thing", cwd="/repo")
        return result, _written(writer)

    def test_full_turn_lifecycle_and_approval_mapping(self):
        seen = {}

        def decide(approval):
            seen.update(approval)
            return "approve_once"

        result, written = self._run(decide)

        # Lifecycle: initialize, initialized, thread/start, turn/start were all sent.
        methods = [m.get("method") for m in written if m.get("method")]
        self.assertEqual(methods[:4], ["initialize", "initialized", "thread/start", "turn/start"])

        # thread/start carried cwd; turn/start carried the prompt + threadId.
        thread_start = next(m for m in written if m.get("method") == "thread/start")
        self.assertEqual(thread_start["params"]["cwd"], "/repo")
        turn_start = next(m for m in written if m.get("method") == "turn/start")
        self.assertEqual(turn_start["params"]["threadId"], "thr_test")
        self.assertEqual(turn_start["params"]["input"], [{"type": "text", "text": "do the thing"}])

        # The approval request was normalized and passed to the decision function.
        self.assertEqual(seen["command"], "echo hi")
        self.assertEqual(seen["cwd"], "/repo")
        self.assertEqual(seen["thread_id"], "thr_test")

        # approve_once -> accept, sent as a response to the server request id 100.
        decision_reply = next(m for m in written if m.get("id") == 100)
        self.assertEqual(decision_reply["result"], {"decision": "accept"})

        self.assertEqual(result["turn_status"], "completed")
        self.assertEqual(result["final_message"], "done")
        self.assertEqual(len(result["approvals"]), 1)
        self.assertEqual(result["approvals"][0]["decision"], "accept")

    def test_reject_maps_to_decline(self):
        result, written = self._run(lambda a: "reject")
        reply = next(m for m in written if m.get("id") == 100)
        self.assertEqual(reply["result"], {"decision": "decline"})
        self.assertEqual(result["approvals"][0]["decision"], "decline")

    def test_raw_codex_decision_passthrough(self):
        result, written = self._run(lambda a: "acceptForSession")
        reply = next(m for m in written if m.get("id") == 100)
        self.assertEqual(reply["result"], {"decision": "acceptForSession"})

    def test_unknown_decision_falls_back_to_decline(self):
        result, written = self._run(lambda a: "banana")
        reply = next(m for m in written if m.get("id") == 100)
        self.assertEqual(reply["result"], {"decision": "decline"})

    def test_decision_map_covers_bigboss_decisions(self):
        self.assertEqual(DECISION_MAP["approve_once"], "accept")
        self.assertEqual(DECISION_MAP["approve_for_run"], "acceptForSession")
        self.assertEqual(DECISION_MAP["reject"], "decline")
        self.assertEqual(DECISION_MAP["request_changes"], "decline")


if __name__ == "__main__":
    unittest.main()

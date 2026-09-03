import unittest

from bigboss.adapters import (
    codex_permission_response,
    decision_response,
    normalize_permission_payload,
    normalize_update_payload,
)


class AdapterNormalizationTests(unittest.TestCase):
    def test_shell_permission_payload_normalizes_to_shell_action(self):
        payload = normalize_permission_payload(
            {
                "session_id": "sess_1",
                "tool_name": "Bash",
                "tool_input": {"command": "git push origin main"},
                "cwd": "C:\\repo",
            },
            harness="claude-code",
        )
        self.assertEqual(payload["run_id"], "sess_1")
        self.assertEqual(payload["harness"], "claude-code")
        self.assertEqual(payload["proposed_action"]["kind"], "shell_command")
        self.assertEqual(payload["proposed_action"]["command"], "git push origin main")

    def test_write_permission_payload_normalizes_to_file_write(self):
        payload = normalize_permission_payload(
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": "src/app.py"},
                "cwd": "C:\\repo",
            },
            harness="codex",
        )
        self.assertEqual(payload["proposed_action"]["kind"], "file_write")
        self.assertEqual(payload["proposed_action"]["path"], "src/app.py")

    def test_decision_response_allows_approved_and_denies_changes_requested(self):
        approved = decision_response(
            {
                "id": "apr_1",
                "status": "approved",
                "action_hash": "abc",
                "latest_decision": {"decision": "approve_once", "note": "OK"},
            }
        )
        self.assertTrue(approved["allow"])
        self.assertEqual(approved["permissionDecision"], "allow")

        denied = decision_response(
            {
                "id": "apr_2",
                "status": "changes_requested",
                "action_hash": "def",
                "latest_decision": {"decision": "request_changes", "note": "Use tests only"},
            }
        )
        self.assertFalse(denied["allow"])
        self.assertEqual(denied["reason"], "Use tests only")

    def test_codex_permission_response_uses_permission_request_contract(self):
        approved = codex_permission_response(
            {
                "id": "apr_1",
                "status": "approved",
                "action_hash": "abc",
                "latest_decision": {"decision": "approve_once", "note": "OK"},
            }
        )
        self.assertEqual(approved["hookSpecificOutput"]["hookEventName"], "PermissionRequest")
        self.assertEqual(approved["hookSpecificOutput"]["decision"]["behavior"], "allow")
        self.assertEqual(approved["systemMessage"], "OK")

        denied = codex_permission_response(
            {
                "id": "apr_2",
                "status": "rejected",
                "action_hash": "def",
                "latest_decision": {"decision": "reject", "note": "No"},
            }
        )
        self.assertEqual(denied["hookSpecificOutput"]["decision"]["behavior"], "deny")
        self.assertEqual(denied["hookSpecificOutput"]["decision"]["message"], "No")

    def test_codex_post_tool_payload_normalizes_to_update(self):
        payload = normalize_update_payload(
            {
                "session_id": "sess_1",
                "hook_event_name": "PostToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "python -m unittest"},
                "tool_response": {"exit_code": 1, "stderr": "failure"},
                "cwd": "C:\\repo",
            },
            harness="codex",
        )
        self.assertEqual(payload["run_id"], "sess_1")
        self.assertEqual(payload["severity"], "warning")
        self.assertEqual(payload["details"]["tool_name"], "Bash")
        self.assertIn("python -m unittest", payload["summary"])


if __name__ == "__main__":
    unittest.main()

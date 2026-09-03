import io
import json
import tempfile
import unittest
from pathlib import Path

from bigboss.mcp_stdio import MCPStdioServer
from bigboss.store import Store


class MCPStdioTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tempdir.name) / "bigboss.sqlite3")
        self.server = MCPStdioServer(self.store)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_initialize_and_tools_list(self):
        response = self.server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(response["result"]["serverInfo"]["name"], "bigboss")

        tools = self.server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        names = {tool["name"] for tool in tools["result"]["tools"]}
        self.assertIn("bigboss_request_approval", names)
        self.assertIn("bigboss_submit_update", names)

    def test_request_list_get_and_update_tools(self):
        request = self.server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "bigboss_request_approval",
                    "arguments": {
                        "harness": "codex",
                        "workspace": self.tempdir.name,
                        "title": "Approve command",
                        "summary": "Run a command.",
                        "proposed_action": {"kind": "shell_command", "command": "pytest -q"},
                    },
                },
            }
        )
        approval = request["result"]["structuredContent"]["approval"]
        self.assertEqual(approval["status"], "pending")
        self.assertIn("action_hash", approval)

        listed = self.server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "bigboss_list_pending_approvals", "arguments": {}},
            }
        )
        self.assertEqual(len(listed["result"]["structuredContent"]["approvals"]), 1)

        fetched = self.server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "bigboss_get_approval",
                    "arguments": {"approval_id": approval["id"]},
                },
            }
        )
        self.assertEqual(fetched["result"]["structuredContent"]["approval"]["id"], approval["id"])

        update = self.server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "bigboss_submit_update",
                    "arguments": {
                        "harness": "codex",
                        "workspace": self.tempdir.name,
                        "title": "Still running",
                    },
                },
            }
        )
        self.assertEqual(update["result"]["structuredContent"]["update"]["title"], "Still running")

    def test_stdio_loop_uses_newline_delimited_json(self):
        # Drive the real stdin/stdout loop the way an MCP client does:
        # newline-delimited JSON-RPC, one message per line.
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ]
        stdin = io.BytesIO(b"".join(json.dumps(r).encode("utf-8") + b"\n" for r in requests))
        stdout = io.BytesIO()
        server = MCPStdioServer(self.store, stdin=stdin, stdout=stdout)
        self.assertEqual(server.run(), 0)

        raw = stdout.getvalue()
        self.assertNotIn(b"Content-Length", raw)  # must NOT be LSP-framed
        lines = [line for line in raw.split(b"\n") if line.strip()]
        # initialize + tools/list reply; the notification produces no response.
        self.assertEqual(len(lines), 2)
        init = json.loads(lines[0])
        self.assertEqual(init["id"], 1)
        self.assertEqual(init["result"]["serverInfo"]["name"], "bigboss")
        tools = json.loads(lines[1])
        self.assertEqual(tools["id"], 2)
        names = {tool["name"] for tool in tools["result"]["tools"]}
        self.assertIn("bigboss_request_approval", names)

    def test_permission_prompt_tool_creates_bigboss_approval(self):
        response = self.server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "bigboss_permission_prompt",
                    "arguments": {
                        "harness": "claude-code",
                        "tool_name": "Bash",
                        "tool_input": {"command": "git push origin main"},
                        "cwd": self.tempdir.name,
                        "wait": False,
                    },
                },
            }
        )
        content = response["result"]["structuredContent"]
        self.assertFalse(content["allow"])
        self.assertEqual(content["approval"]["status"], "pending")
        self.assertEqual(content["approval"]["proposed_action"]["command"], "git push origin main")


if __name__ == "__main__":
    unittest.main()

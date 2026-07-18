import tempfile
from pathlib import Path
import unittest

import agent


class WorkspaceToolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name).resolve()

    def tearDown(self):
        self.temporary.cleanup()

    def test_path_cannot_escape_workspace(self):
        with self.assertRaises(agent.AgentError):
            agent.resolve_workspace_path(self.workspace, "../outside.txt")

    def test_write_read_and_replace(self):
        agent.write_file(self.workspace, "src/example.txt", "alpha\nbeta\n")
        self.assertIn("2: beta", agent.read_file(self.workspace, "src/example.txt", 1, 2))
        agent.replace_in_file(self.workspace, "src/example.txt", "beta", "gamma")
        self.assertEqual((self.workspace / "src/example.txt").read_text(), "alpha\ngamma\n")

    def test_existing_file_requires_explicit_overwrite(self):
        agent.write_file(self.workspace, "note.txt", "one")
        with self.assertRaises(agent.AgentError):
            agent.write_file(self.workspace, "note.txt", "two")

    def test_safe_mode_blocks_destructive_command(self):
        with self.assertRaises(agent.AgentError):
            agent.run_command(self.workspace, "Remove-Item note.txt", 5, allow_risky=False)

    def test_web_url_validation(self):
        self.assertEqual(agent.validate_web_url("https://www.bilibili.com"), "https://www.bilibili.com")
        with self.assertRaises(agent.AgentError):
            agent.validate_web_url("file:///C:/Windows/System32")
        with self.assertRaises(agent.AgentError):
            agent.validate_web_url("https://user:secret@example.com")

    def test_runtime_intent_detection(self):
        self.assertTrue(agent.asks_for_active_model("现在是什么模型？"))
        self.assertEqual(
            agent.requested_website("请用 Edge 打开 B站登录页面"),
            "https://www.bilibili.com/",
        )
        self.assertIsNone(agent.requested_website("介绍一下 B站"))

    def test_turn_emits_structured_tool_events(self):
        config = {
            "model": "test-model",
            "base_url": "http://127.0.0.1:11434",
            "max_steps": 3,
            "max_tool_output_chars": 1000,
        }
        local_agent = agent.LocalAgent(self.workspace, config)
        responses = iter(
            [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {"function": {"name": "inspect_workspace", "arguments": {"max_depth": 0}}}
                        ],
                    }
                },
                {"message": {"role": "assistant", "content": "done"}},
            ]
        )
        local_agent.api_chat = lambda: next(responses)
        events = []
        result = local_agent.turn("inspect", on_event=events.append)
        self.assertEqual(result, "done")
        self.assertEqual(
            [event["type"] for event in events],
            ["step", "tool_start", "tool_result", "step", "assistant"],
        )


if __name__ == "__main__":
    unittest.main()


import tempfile
from pathlib import Path
import threading
import time
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

    def test_command_timeout_stops_process_tree(self):
        started = time.monotonic()
        with self.assertRaisesRegex(agent.AgentError, "timed out after 1s"):
            agent.run_command(self.workspace, "Start-Sleep -Seconds 30", 1, allow_risky=True)
        self.assertLess(time.monotonic() - started, 8)

    def test_command_can_be_cancelled(self):
        cancel_event = threading.Event()
        timer = threading.Timer(0.25, cancel_event.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(agent.AgentError, "cancelled by user"):
                agent.run_command(
                    self.workspace,
                    "Start-Sleep -Seconds 30",
                    30,
                    allow_risky=True,
                    cancel_event=cancel_event,
                )
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - started, 8)

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
        self.assertIn("尚未安装经过验证", agent.requested_missing_capability("帮我在 QQ 给联系人发送消息"))
        self.assertIsNone(agent.requested_missing_capability("帮我编写一个 QQ 自动化脚本"))
        self.assertIsNone(agent.requested_missing_capability("先帮我安装并接入 OCR 能力"))

    def test_missing_capability_is_blocked_before_model_call(self):
        config = {"model": "test-model", "base_url": "http://127.0.0.1:11434"}
        local_agent = agent.LocalAgent(self.workspace, config)
        local_agent.api_chat = lambda: self.fail("model must not be called for a missing capability")
        events = []
        result = local_agent.turn("用 OCR 在 QQ 找联系人并发送消息", on_event=events.append)
        self.assertIn("尚未安装经过验证", result)
        self.assertEqual(events[-1]["type"], "assistant")
        self.assertTrue(events[-1]["final"])

    def test_unverified_action_claim_is_rejected(self):
        config = {
            "model": "test-model",
            "base_url": "http://127.0.0.1:11434",
            "max_steps": 4,
        }
        local_agent = agent.LocalAgent(self.workspace, config)
        responses = iter(
            [
                {"message": {"role": "assistant", "content": "我会创建文件。"}},
                {"message": {"role": "assistant", "content": "文件已经创建。"}},
                {"message": {"role": "assistant", "content": "完成。"}},
            ]
        )
        local_agent.api_chat = lambda: next(responses)
        result = local_agent.turn("创建 note.txt")
        self.assertIn("没有调用任何已注册工具", result)

    def test_length_limited_answer_is_continued_and_merged(self):
        config = {
            "model": "test-model",
            "base_url": "http://127.0.0.1:11434",
            "max_steps": 3,
        }
        local_agent = agent.LocalAgent(self.workspace, config)
        responses = iter(
            [
                {"done_reason": "length", "message": {"role": "assistant", "content": "第一部分"}},
                {"done_reason": "stop", "message": {"role": "assistant", "content": "第二部分"}},
            ]
        )
        local_agent.api_chat = lambda: next(responses)
        result = local_agent.turn("解释架构")
        self.assertEqual(result, "第一部分\n\n第二部分")

    def test_multiple_length_continuations_are_bounded(self):
        config = {
            "model": "test-model",
            "base_url": "http://127.0.0.1:11434",
            "max_steps": 5,
            "max_output_continuations": 2,
        }
        local_agent = agent.LocalAgent(self.workspace, config)
        responses = iter(
            [
                {"done_reason": "length", "message": {"role": "assistant", "content": "一"}},
                {"done_reason": "length", "message": {"role": "assistant", "content": "二"}},
                {"done_reason": "stop", "message": {"role": "assistant", "content": "三"}},
            ]
        )
        local_agent.api_chat = lambda: next(responses)
        self.assertEqual(local_agent.turn("完整回答"), "一\n\n二\n\n三")

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
        self.assertTrue(
            any(
                message.get("role") == "user" and "Use the tool results above" in message.get("content", "")
                for message in local_agent.messages
            )
        )


if __name__ == "__main__":
    unittest.main()

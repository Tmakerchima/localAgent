import types
import unittest
import json
from email.message import Message
from pathlib import Path
import tempfile
from unittest import mock

import web_server
from web_server import AgentWebHandler


class StartupTests(unittest.TestCase):
    @mock.patch("web_server.threading.Thread")
    def test_model_warmup_starts_in_background(self, thread_class):
        state = web_server.AppState(
            Path.cwd(),
            {"model": "test:latest", "base_url": "http://127.0.0.1:11434"},
            False,
        )
        self.assertTrue(state.model_warming)
        thread_class.assert_called_once_with(
            target=state._warm_in_background, daemon=True
        )
        thread_class.return_value.start.assert_called_once_with()

    @mock.patch("web_server.LOCAL_OPENER.open")
    @mock.patch("web_server.threading.Thread")
    def test_release_model_requests_zero_keep_alive(self, thread_class, open_mock):
        state = web_server.AppState(
            Path.cwd(),
            {"model": "test:latest", "base_url": "http://127.0.0.1:11434"},
            False,
        )
        state.config["model"] = "test:latest"
        response = mock.MagicMock()
        response.read.return_value = b'{"done_reason":"unload"}'
        open_mock.return_value.__enter__.return_value = response
        state.release_model()
        request = open_mock.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request.full_url, "http://127.0.0.1:11434/api/generate")
        self.assertEqual(payload["keep_alive"], 0)
        self.assertEqual(payload["model"], "test:latest")

    @mock.patch("web_server.threading.Thread")
    def test_sessions_bind_to_selected_workspace(self, thread_class):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            with mock.patch.object(web_server, "WORKSPACES_PATH", Path(first) / "workspaces.json"):
                state = web_server.AppState(
                    Path(first),
                    {"model": "test:latest", "base_url": "http://127.0.0.1:11434"},
                    False,
                )
                selected = state.register_workspace(Path(second))
                session = state.get_session("task-two", selected["workspace_id"])
                self.assertEqual(session.agent.workspace, Path(second).resolve())
                reloaded = web_server.AppState(
                    Path(first),
                    {"model": "test:latest", "base_url": "http://127.0.0.1:11434"},
                    False,
                )
                self.assertEqual(
                    reloaded.resolve_workspace(selected["workspace_id"]), Path(second).resolve()
                )
                with self.assertRaises(web_server.AgentError):
                    state.get_session("missing", "expired-workspace")


class PairingAuthorizationTests(unittest.TestCase):
    def make_handler(self, origin="", token="", host="127.0.0.1:8765"):
        handler = AgentWebHandler.__new__(AgentWebHandler)
        headers = Message()
        if origin:
            headers["Origin"] = origin
        if token:
            headers["X-Local-Agent-Token"] = token
        headers["Host"] = host
        handler.headers = headers
        handler.server = types.SimpleNamespace(
            app=types.SimpleNamespace(
                allowed_origins={"https://localagent-test.vercel.app"},
                pairing_token="pairing-secret",
            )
        )
        return handler

    def test_local_same_origin_does_not_require_pairing(self):
        handler = self.make_handler(origin="http://127.0.0.1:8765")
        self.assertTrue(handler.cross_origin_authorized())

    def test_allowed_public_origin_requires_exact_token(self):
        missing = self.make_handler(origin="https://localagent-test.vercel.app")
        correct = self.make_handler(
            origin="https://localagent-test.vercel.app", token="pairing-secret"
        )
        self.assertFalse(missing.cross_origin_authorized())
        self.assertTrue(correct.cross_origin_authorized())

    def test_unlisted_origin_is_rejected_even_with_token(self):
        handler = self.make_handler(
            origin="https://attacker.example", token="pairing-secret"
        )
        self.assertFalse(handler.cross_origin_authorized())

    def test_allowed_origin_must_be_exact_https_origin(self):
        self.assertEqual(
            web_server.validate_https_origin("https://agent.example/"),
            "https://agent.example",
        )
        for invalid in (
            "http://agent.example",
            "https://agent.example/path",
            "https://user:password@agent.example",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(web_server.argparse.ArgumentTypeError):
                    web_server.validate_https_origin(invalid)


if __name__ == "__main__":
    unittest.main()

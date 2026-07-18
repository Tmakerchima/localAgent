import types
import unittest
from email.message import Message
from pathlib import Path
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

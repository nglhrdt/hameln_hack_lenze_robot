import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from input_to_cmd.__main__ import EXTRACTION_INSTRUCTIONS, app


class CommandTests(unittest.TestCase):
    def setUp(self) -> None:
        settings = patch.multiple(
            "input_to_cmd.__main__",
            AI_API_URL="https://ki.mwl.merona.de/v1",
            MODEL="smart-city-fast",
            COMMAND_API_URL="http://192.168.5.202:8000/command",
        )
        settings.start()
        self.addCleanup(settings.stop)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    @patch.dict("os.environ", {"TEST_KEY": "test-key"})
    @patch("input_to_cmd.__main__.OpenAI")
    @patch("input_to_cmd.__main__.http.client.HTTPConnection")
    @patch("builtins.print")
    def test_forwards_each_valid_command(self, mock_print, mock_connection, mock_openai):
        client = mock_openai.return_value.__enter__.return_value
        connection = mock_connection.return_value
        connection.getresponse.return_value.status = 200

        for extracted in ("home", "move right", "move up", "move left", "stop"):
            with self.subTest(command=extracted):
                text = "Stop the robot" if extracted == "stop" else "My request"
                client.chat.completions.create.return_value.choices = [
                    SimpleNamespace(message=SimpleNamespace(content=extracted))
                ]
                response = self.client.post(
                    "/command", json={"inputType": "text", "data": text}
                )

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"result": "command exectued"})
                self.assertEqual(response.headers["content-type"], "application/json")
                mock_openai.assert_called_once_with(
                    base_url="https://ki.mwl.merona.de/v1", api_key="test-key"
                )
                client.chat.completions.create.assert_called_once_with(
                    model="smart-city-fast",
                    messages=[
                        {"role": "system", "content": EXTRACTION_INSTRUCTIONS},
                        {"role": "user", "content": text},
                    ],
                    max_tokens=1024,
                )
                if extracted == "stop":
                    self.assertIn("stop", EXTRACTION_INSTRUCTIONS)
                mock_connection.assert_called_once_with("192.168.5.202", 8000, timeout=10)
                connection.request.assert_called_once_with(
                    "POST",
                    "/command",
                    body=f'{{"command": "{extracted}"}}'.encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                connection.close.assert_called_once()
                mock_print.assert_called_once_with(extracted, flush=True)
                mock_openai.reset_mock()
                mock_connection.reset_mock()
                mock_print.reset_mock()

    @patch.dict("os.environ", {"TEST_KEY": "test-key"})
    @patch("input_to_cmd.__main__.COMMAND_API_URL", "http://example.test:9000/run?source=api")
    @patch("input_to_cmd.__main__.OpenAI")
    @patch("input_to_cmd.__main__.http.client.HTTPConnection")
    def test_command_api_url_controls_destination_and_path(self, mock_connection, mock_openai):
        client = mock_openai.return_value.__enter__.return_value
        client.chat.completions.create.return_value.choices = [
            SimpleNamespace(message=SimpleNamespace(content="home"))
        ]
        mock_connection.return_value.getresponse.return_value.status = 200

        response = self.client.post("/command", json={"inputType": "text", "data": "Go home"})

        self.assertEqual(response.status_code, 200)
        mock_connection.assert_called_once_with("example.test", 9000, timeout=10)
        self.assertEqual(
            mock_connection.return_value.request.call_args.args[:2], ("POST", "/run?source=api")
        )

    @patch.dict("os.environ", {"TEST_KEY": "test-key"})
    @patch("input_to_cmd.__main__.COMMAND_API_URL", "https://example.test/command")
    @patch("input_to_cmd.__main__.OpenAI")
    @patch("input_to_cmd.__main__.http.client.HTTPConnection")
    def test_https_command_url_is_not_sent_over_http(self, mock_connection, mock_openai):
        client = mock_openai.return_value.__enter__.return_value
        client.chat.completions.create.return_value.choices = [
            SimpleNamespace(message=SimpleNamespace(content="home"))
        ]

        response = self.client.post("/command", json={"inputType": "text", "data": "Go home"})

        self.assertEqual(response.status_code, 500)
        mock_connection.assert_not_called()

    @patch.dict("os.environ", {"TEST_KEY": "test-key"})
    @patch("input_to_cmd.__main__.OpenAI")
    @patch("input_to_cmd.__main__.http.client.HTTPConnection")
    def test_invalid_or_ambiguous_output_is_not_forwarded(self, mock_connection, mock_openai):
        client = mock_openai.return_value.__enter__.return_value
        for result, status in (("NONE", 422), ("move right and move left", 502)):
            with self.subTest(result=result):
                client.chat.completions.create.return_value.choices = [
                    SimpleNamespace(message=SimpleNamespace(content=result))
                ]
                response = self.client.post(
                    "/command", json={"inputType": "text", "data": "My request"}
                )
                self.assertEqual(response.status_code, status)
                mock_connection.assert_not_called()

    @patch.dict("os.environ", {"TEST_KEY": "test-key"})
    @patch("input_to_cmd.__main__.OpenAI")
    @patch("input_to_cmd.__main__.http.client.HTTPConnection")
    def test_command_server_failure_is_not_reported_as_success(self, mock_connection, mock_openai):
        client = mock_openai.return_value.__enter__.return_value
        client.chat.completions.create.return_value.choices = [
            SimpleNamespace(message=SimpleNamespace(content="home"))
        ]
        connection = mock_connection.return_value
        connection.getresponse.return_value.status = 500

        response = self.client.post(
            "/command", json={"inputType": "text", "data": "Go home"}
        )
        self.assertEqual(response.status_code, 502)
        connection.close.assert_called_once()

        connection.reset_mock()
        connection.request.side_effect = TimeoutError("timed out")
        response = self.client.post(
            "/command", json={"inputType": "text", "data": "Go home"}
        )
        self.assertEqual(response.status_code, 502)
        self.assertIn("Could not confirm", response.json()["detail"])
        connection.close.assert_called_once()

    @patch.dict("os.environ", {"TEST_KEY": "test-key"})
    @patch("input_to_cmd.__main__.OpenAI")
    def test_model_failure_returns_server_error(self, mock_openai):
        client = mock_openai.return_value.__enter__.return_value
        client.chat.completions.create.side_effect = RuntimeError("API failed")

        response = self.client.post(
            "/command", json={"inputType": "text", "data": "Move forward"}
        )

        self.assertEqual(response.status_code, 500)

    @patch.dict("os.environ", {"TEST_KEY": "test-key"})
    @patch("input_to_cmd.__main__.OpenAI")
    def test_empty_model_response_returns_server_error(self, mock_openai):
        client = mock_openai.return_value.__enter__.return_value
        client.chat.completions.create.return_value.choices = []

        response = self.client.post(
            "/command", json={"inputType": "text", "data": "Move forward"}
        )

        self.assertEqual(response.status_code, 500)

    @patch.dict("os.environ", {"TEST_KEY": "test-key"})
    @patch("input_to_cmd.__main__.OpenAI")
    @patch("input_to_cmd.__main__.http.client.HTTPConnection")
    def test_text_data_is_sent_to_model_unchanged(self, mock_connection, mock_openai):
        client = mock_openai.return_value.__enter__.return_value
        client.chat.completions.create.return_value.choices = [
            SimpleNamespace(message=SimpleNamespace(content="move right"))
        ]
        mock_connection.return_value.getresponse.return_value.status = 200
        text = "Bewege den Roboter fünf Zentimeter nach rechts."

        response = self.client.post("/command", json={"inputType": "text", "data": text})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"result": "command exectued"})
        self.assertEqual(response.headers["content-type"], "application/json")
        messages = client.chat.completions.create.call_args.kwargs["messages"]
        self.assertEqual(messages[1], {"role": "user", "content": text})

    @patch.dict("os.environ", {"TEST_KEY": "test-key"})
    @patch("input_to_cmd.__main__.OpenAI")
    @patch("input_to_cmd.__main__.http.client.HTTPConnection")
    def test_debug_logging_reports_progress_without_raw_input(self, mock_connection, mock_openai):
        client = mock_openai.return_value.__enter__.return_value
        client.chat.completions.create.return_value.choices = [
            SimpleNamespace(message=SimpleNamespace(content="home"))
        ]
        mock_connection.return_value.getresponse.return_value.status = 200
        with self.assertLogs("input_to_cmd", level="DEBUG") as logs:
            response = self.client.post(
                "/command", json={"inputType": "text", "data": "private request"}
            )
        self.assertEqual(response.status_code, 200)
        output = "\n".join(logs.output)
        self.assertIn("Extracted command: home", output)
        self.assertIn("Command server responded with HTTP 200", output)
        self.assertNotIn("private request", output)
        self.assertNotIn("test-key", output)

    @patch.dict("os.environ", {"TEST_KEY": "test-key"})
    @patch("input_to_cmd.__main__.OpenAI")
    @patch("input_to_cmd.__main__.http.client.HTTPConnection")
    def test_connection_error_is_logged(self, mock_connection, mock_openai):
        client = mock_openai.return_value.__enter__.return_value
        client.chat.completions.create.return_value.choices = [
            SimpleNamespace(message=SimpleNamespace(content="home"))
        ]
        mock_connection.return_value.request.side_effect = ConnectionRefusedError("refused")
        with self.assertLogs("input_to_cmd", level="ERROR") as logs:
            response = self.client.post("/command", json={"inputType": "text", "data": "Home"})
        self.assertEqual(response.status_code, 502)
        self.assertIn("ConnectionRefusedError", logs.output[0])

    @patch("input_to_cmd.__main__.OpenAI")
    @patch("input_to_cmd.__main__.http.client.HTTPConnection")
    def test_invalid_inputs_are_rejected_without_processing(self, mock_connection, mock_openai):
        for payload in (
            {"inputType": "image", "data": "image data"},
            {"inputType": "audio", "data": "audio data"},
            {"inputType": "video", "data": "video data"},
            {"inputType": "text"},
            {"data": "Move right"},
            {"inputType": "text", "data": {"text": "Move right"}},
            {"inputType": "text", "data": 123},
        ):
            with self.subTest(payload=payload):
                response = self.client.post("/command", json=payload)
                self.assertEqual(response.status_code, 422)

        response = self.client.post(
            "/command", content="Move right", headers={"Content-Type": "text/plain"}
        )
        self.assertEqual(response.status_code, 422)
        mock_openai.assert_not_called()
        mock_connection.assert_not_called()


class ConfigurationTests(unittest.TestCase):
    def test_cors_preflight_and_post_response(self):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        env["CORS_ALLOWED_ORIGINS"] = " http://frontend.test:3000, http://other.test "
        script = """
import json
from types import SimpleNamespace
from unittest.mock import patch
from fastapi.testclient import TestClient
from input_to_cmd.__main__ import app

client = TestClient(app)
headers = {"Origin": "http://frontend.test:3000", "Access-Control-Request-Method": "POST",
           "Access-Control-Request-Headers": "content-type"}
allowed = client.options("/command", headers=headers)
headers["Origin"] = "http://untrusted.test"
denied = client.options("/command", headers=headers)
headers["Origin"] = "http://frontend.test:3000"
headers["Access-Control-Request-Headers"] = "x-unapproved"
denied_header = client.options("/command", headers=headers)
post = client.post("/command", headers={"Origin": "http://frontend.test:3000"}, json={})
with patch.dict("os.environ", {"TEST_KEY": "test-key"}), \\
     patch("input_to_cmd.__main__.OpenAI") as ai, \\
     patch("input_to_cmd.__main__.http.client.HTTPConnection") as connection, \\
     patch("builtins.print"):
    ai.return_value.__enter__.return_value.chat.completions.create.return_value.choices = [
        SimpleNamespace(message=SimpleNamespace(content="stop"))
    ]
    connection.return_value.getresponse.return_value.status = 200
    success = client.post("/command", headers={"Origin": "http://frontend.test:3000"},
                          json={"inputType": "text", "data": "Stop"})
print(json.dumps([allowed.status_code, allowed.headers.get("access-control-allow-origin"),
                  allowed.headers.get("access-control-allow-methods"),
                  denied.status_code, denied.headers.get("access-control-allow-origin"),
                  denied_header.status_code, post.status_code,
                  post.headers.get("access-control-allow-origin"), success.status_code,
                  success.headers.get("access-control-allow-origin")]))
"""
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-c", script], cwd=directory, env=env,
                check=True, capture_output=True, text=True,
            )
            values = json.loads(result.stdout)
            self.assertEqual(values[0:2], [200, "http://frontend.test:3000"])
            self.assertIn("POST", values[2])
            self.assertEqual(values[3:], [
                400, None, 400, 422, "http://frontend.test:3000", 200, "http://frontend.test:3000",
            ])

            for setting in (None, "*"):
                with self.subTest(origins=setting):
                    if setting is None:
                        env.pop("CORS_ALLOWED_ORIGINS", None)
                    else:
                        env["CORS_ALLOWED_ORIGINS"] = setting
                    result = subprocess.run(
                        [sys.executable, "-c", script], cwd=directory, env=env,
                        check=True, capture_output=True, text=True,
                    )
                    values = json.loads(result.stdout)
                    self.assertEqual(values[0:2], [200, "*"])
                    self.assertIn("POST", values[2])
                    self.assertEqual(values[3:], [200, "*", 400, 422, "*", 200, "*"])

    def test_log_level_controls_debug_output(self):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        env["TEST_KEY"] = "test-key"
        script = (
            "from unittest.mock import patch; import input_to_cmd.__main__ as config; "
            "patcher = patch.object(config.uvicorn, 'run'); patcher.start(); "
            "config.main(); config.logger.debug('debug message'); patcher.stop()"
        )
        with tempfile.TemporaryDirectory() as directory:
            for level in ("INFO", "DEBUG"):
                with self.subTest(level=level):
                    env["LOG_LEVEL"] = level
                    result = subprocess.run(
                        [sys.executable, "-c", script], cwd=directory, env=env,
                        check=True, capture_output=True, text=True,
                    )
                    self.assertEqual("debug message" in result.stderr, level == "DEBUG")

    def test_dotenv_settings_and_environment_precedence(self):
        names = ("HOST", "PORT", "MODEL", "AI_API_URL", "COMMAND_API_URL", "TEST_KEY")
        file_values = ("127.0.0.2", "9001", "file-model", "https://ai.test/v1",
                       "http://command.test:9002/run", "file-key")
        env = os.environ.copy()
        for name in names:
            env.pop(name, None)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        script = (
            "import json, os; import input_to_cmd.__main__ as config; "
            "print(json.dumps([config.HOST, config.PORT, config.MODEL, "
            "config.AI_API_URL, config.COMMAND_API_URL, os.environ['TEST_KEY']]))"
        )

        with tempfile.TemporaryDirectory() as directory:
            Path(directory, ".env").write_text(
                "".join(f"{name}={value}\n" for name, value in zip(names, file_values)),
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, "-c", script], cwd=directory, env=env,
                check=True, capture_output=True, text=True,
            )
            self.assertEqual(json.loads(result.stdout), [
                "127.0.0.2", 9001, "file-model", "https://ai.test/v1",
                "http://command.test:9002/run", "file-key",
            ])

            env["MODEL"] = "environment-model"
            env["TEST_KEY"] = "environment-key"
            result = subprocess.run(
                [sys.executable, "-c", script], cwd=directory, env=env,
                check=True, capture_output=True, text=True,
            )
            self.assertEqual(json.loads(result.stdout)[2], "environment-model")
            self.assertEqual(json.loads(result.stdout)[5], "environment-key")


if __name__ == "__main__":
    unittest.main()

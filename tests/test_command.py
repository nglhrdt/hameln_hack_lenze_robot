import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from input_to_cmd.__main__ import EXTRACTION_INSTRUCTIONS, app


class CommandTests(unittest.TestCase):
    def setUp(self) -> None:
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

        for extracted in ("home", "move right", "move up", "move left"):
            with self.subTest(command=extracted):
                client.chat.completions.create.return_value.choices = [
                    SimpleNamespace(message=SimpleNamespace(content=extracted))
                ]
                response = self.client.post(
                    "/command", json={"inputType": "text", "data": "My request"}
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
                        {"role": "user", "content": "My request"},
                    ],
                    max_tokens=1024,
                )
                mock_connection.assert_called_once_with("192.168.5.203", 8000, timeout=10)
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


if __name__ == "__main__":
    unittest.main()

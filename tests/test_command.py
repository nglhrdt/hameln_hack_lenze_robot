import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from input_to_cmd.api import EXTRACTION_INSTRUCTIONS, app
from input_to_cmd.command_history import CommandHistory
from input_to_cmd.command_dispatcher import UnsupportedCommandError, execute_command
from input_to_cmd import command_dispatcher, opc_client


class CommandTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        history = patch(
            "input_to_cmd.api.command_history",
            CommandHistory(Path(directory.name) / "history.sqlite3"),
        )
        history.start()
        self.addCleanup(history.stop)
        settings = patch.multiple(
            "input_to_cmd.api",
            AI_API_URL="https://ki.mwl.merona.de/v1",
            MODEL="smart-city-fast",
        )
        settings.start()
        self.addCleanup(settings.stop)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def test_web_page_and_assets_are_served(self):
        for path, content_type in (
            ("/web/", "text/html"),
            ("/web/index.html", "text/html"),
            ("/web/css.css", "text/css"),
            ("/web/webSpeechAPI.js", "javascript"),
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn(content_type, response.headers["content-type"])
                self.assertTrue(response.content)

    @patch.dict("os.environ", {"TEST_KEY": "test-key"})
    @patch("input_to_cmd.command_dispatcher.set_home", new_callable=AsyncMock)
    @patch("input_to_cmd.api.OpenAI")
    @patch("builtins.print")
    def test_home_is_extracted_and_executed(self, mock_print, mock_openai, mock_home):
        client = mock_openai.return_value.__enter__.return_value
        client.chat.completions.create.return_value.choices = [
            SimpleNamespace(message=SimpleNamespace(content='{"command":"home","stepSize":null}'))
        ]
        text = "Bitte fahre nach Hause."
        response = self.client.post("/command", json={"inputType": "text", "data": text})

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
        mock_home.assert_awaited_once_with()
        mock_print.assert_called_once_with("home", flush=True)

    @patch.dict("os.environ", {"TEST_KEY": "test-key"})
    @patch("input_to_cmd.command_dispatcher.stop", new_callable=AsyncMock)
    @patch("input_to_cmd.command_dispatcher.move", new_callable=AsyncMock)
    @patch("input_to_cmd.api.OpenAI")
    def test_move_down_and_stop_are_extracted_and_executed(
        self, mock_openai, mock_move, mock_stop
    ):
        client = mock_openai.return_value.__enter__.return_value
        for result in ("move_down", "stop"):
            with self.subTest(result=result):
                client.chat.completions.create.return_value.choices = [
                    SimpleNamespace(message=SimpleNamespace(content=json.dumps({
                        "command": result, "stepSize": None,
                    })))
                ]
                response = self.client.post(
                    "/command", json={"inputType": "text", "data": result}
                )
                self.assertEqual(response.status_code, 200)
        mock_move.assert_awaited_once_with("move_down", step_size=None)
        mock_stop.assert_awaited_once_with()
        self.assertIn("move_down", EXTRACTION_INSTRUCTIONS)

    @patch.dict("os.environ", {"TEST_KEY": "test-key"})
    @patch("input_to_cmd.command_dispatcher.set_home", new_callable=AsyncMock)
    @patch("input_to_cmd.api.OpenAI")
    def test_ambiguous_and_invalid_commands_are_not_executed(self, mock_openai, mock_home):
        client = mock_openai.return_value.__enter__.return_value
        for result, status in (
            ('{"command":null,"stepSize":null}', 422),
            ('{"command":"move_right and move_left","stepSize":null}', 502),
        ):
            with self.subTest(result=result):
                client.chat.completions.create.return_value.choices = [
                    SimpleNamespace(message=SimpleNamespace(content=result))
                ]
                response = self.client.post(
                    "/command", json={"inputType": "text", "data": "My request"}
                )
                self.assertEqual(response.status_code, status)
                mock_home.assert_not_awaited()

    @patch.dict("os.environ", {"TEST_KEY": "test-key"})
    @patch("input_to_cmd.command_dispatcher.set_home", new_callable=AsyncMock)
    @patch("input_to_cmd.api.OpenAI")
    def test_opc_failure_is_not_reported_as_success(self, mock_openai, mock_home):
        client = mock_openai.return_value.__enter__.return_value
        client.chat.completions.create.return_value.choices = [
            SimpleNamespace(message=SimpleNamespace(content='{"command":"home","stepSize":null}'))
        ]
        mock_home.side_effect = ConnectionError("OPC failed")
        response = self.client.post("/command", json={"inputType": "text", "data": "Home"})
        self.assertEqual(response.status_code, 500)
        mock_home.assert_awaited_once()

    @patch.dict("os.environ", {"TEST_KEY": "test-key"})
    @patch("input_to_cmd.api.OpenAI")
    def test_model_failure_returns_server_error(self, mock_openai):
        client = mock_openai.return_value.__enter__.return_value
        client.chat.completions.create.side_effect = RuntimeError("API failed")
        response = self.client.post("/command", json={"inputType": "text", "data": "Home"})
        self.assertEqual(response.status_code, 500)

    @patch.dict("os.environ", {"TEST_KEY": "test-key"})
    @patch("input_to_cmd.api.OpenAI")
    def test_empty_model_response_returns_server_error(self, mock_openai):
        client = mock_openai.return_value.__enter__.return_value
        client.chat.completions.create.return_value.choices = []
        response = self.client.post("/command", json={"inputType": "text", "data": "Home"})
        self.assertEqual(response.status_code, 500)

    @patch.dict("os.environ", {"TEST_KEY": "test-key"})
    @patch("input_to_cmd.command_dispatcher.set_home", new_callable=AsyncMock)
    @patch("input_to_cmd.api.OpenAI")
    def test_debug_logging_reports_progress_without_raw_input(self, mock_openai, mock_home):
        client = mock_openai.return_value.__enter__.return_value
        client.chat.completions.create.return_value.choices = [
            SimpleNamespace(message=SimpleNamespace(content='{"command":"home","stepSize":null}'))
        ]
        with self.assertLogs("input_to_cmd", level="DEBUG") as logs:
            response = self.client.post(
                "/command", json={"inputType": "text", "data": "private request"}
            )
        self.assertEqual(response.status_code, 200)
        output = "\n".join(logs.output)
        self.assertIn("Extracted command: home", output)
        self.assertIn("Command execution finished: home", output)
        self.assertNotIn("private request", output)
        self.assertNotIn("test-key", output)

    @patch("input_to_cmd.api.OpenAI")
    @patch("input_to_cmd.command_dispatcher.set_home", new_callable=AsyncMock)
    def test_invalid_inputs_are_rejected_without_processing(self, mock_home, mock_openai):
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
        mock_home.assert_not_awaited()

    @patch("input_to_cmd.api.OpenAI")
    @patch("input_to_cmd.command_dispatcher.write_xz_points", new_callable=AsyncMock)
    def test_point_input_maps_normalized_values_and_dispatches(self, write_points, mock_openai):
        response = self.client.post(
            "/command",
            json={"inputType": "point", "points": [
                {"x": 0.0, "z": 0.0}, {"x": 1.0, "z": 0.0},
                {"x": 0.0, "z": 1.0}, {"x": 1.0, "z": 1.0},
                {"x": 0.5, "z": 0.5}, {"x": 0.25, "z": 0.75},
            ]},
        )

        self.assertEqual(response.status_code, 200)
        write_points.assert_awaited_once_with([
            (16.0, 155.0), (319.0, 155.0), (16.0, -8.0), (319.0, -8.0),
            (167.5, 73.5), (91.75, 32.75),
        ])
        mock_openai.assert_not_called()

    @patch("input_to_cmd.api.OpenAI")
    @patch("input_to_cmd.command_dispatcher.write_xz_points", new_callable=AsyncMock)
    def test_invalid_points_are_rejected(self, write_points, mock_openai):
        for points in (
            None, [], [{"x": -0.1, "z": 0.5}], [{"x": 0.5, "z": 1.1}],
            [{"x": True, "z": 0.5}], [{"x": "0.5", "z": 0.5}],
            [{"x": 0.5}], [{"z": 0.5}], [[0.5, 0.5]],
            [{"x": float("nan"), "z": 0.5}], [{"x": 0.5, "z": float("inf")}],
            [{"x": 50.0, "z": 0.5}],
        ):
            with self.subTest(points=points):
                response = self.client.post(
                    "/command", content=json.dumps({"inputType": "point", "points": points}),
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(response.status_code, 422)
        write_points.assert_not_awaited()
        mock_openai.assert_not_called()

    @patch("input_to_cmd.command_dispatcher.write_xz_points", new_callable=AsyncMock)
    def test_point_write_failure_is_not_reported_as_success(self, write_points):
        write_points.side_effect = ConnectionError("OPC failed")
        response = self.client.post(
            "/command", json={"inputType": "point", "points": [{"x": 0.5, "z": 0.5}]}
        )
        self.assertEqual(response.status_code, 500)
        write_points.assert_awaited_once()


class CommandHistoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "nested" / "history.sqlite3"
        self.store = CommandHistory(self.path)
        history = patch("input_to_cmd.api.command_history", self.store)
        history.start()
        self.addCleanup(history.stop)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    @patch("input_to_cmd.api.extract_command", return_value=("home", None))
    @patch("input_to_cmd.command_dispatcher.set_home", new_callable=AsyncMock)
    @patch("input_to_cmd.command_dispatcher.stop", new_callable=AsyncMock)
    @patch("input_to_cmd.command_dispatcher.write_xz_points", new_callable=AsyncMock)
    def test_records_sources_outcomes_and_mapped_points(self, write_points, stop, home, extract):
        self.assertEqual(self.client.get("/command-history").json(), [])
        self.assertEqual(self.client.post("/command", json={
            "inputType": "text", "data": "Bitte nach Hause",
        }).status_code, 200)
        stop.side_effect = ConnectionError("OPC failed")
        self.assertEqual(self.client.post("/command", json={
            "inputType": "instantCommand", "data": "stop",
        }).status_code, 500)
        self.assertEqual(self.client.post("/command", json={
            "inputType": "point", "points": [{"x": 0.25, "z": 0.25}],
        }).status_code, 200)
        self.assertEqual(self.client.post("/command", json={
            "inputType": "instantCommand", "data": "invalid",
        }).status_code, 422)

        events = CommandHistory(self.path).read()
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual([event["command"] for event in events], ["point", "stop", "home"])
        self.assertEqual([event["outcome"] for event in events], ["succeeded", "failed", "succeeded"])
        self.assertEqual(events[0]["source"], {
            "inputType": "point", "points": [{"x": 0.25, "z": 0.25}],
        })
        self.assertEqual(events[0]["points"], [[91.75, 114.25]])
        self.assertEqual(events[1]["source"], {"inputType": "instantCommand", "data": "stop"})
        self.assertEqual(events[2]["source"], {"inputType": "text", "data": "Bitte nach Hause"})
        self.assertEqual(self.client.get("/command-history").json(), events)

    def test_keeps_latest_100_and_does_not_restore_evicted_pending_events(self):
        first = self.store.start("home", {"inputType": "instantCommand", "data": "home"}, None)
        for i in range(100):
            self.store.start("stop", {"inputType": "text", "data": str(i)}, None)
        self.store.finish(first, "succeeded")
        events = CommandHistory(self.path).read()
        self.assertEqual(len(events), 100)
        self.assertEqual(events[0]["source"]["data"], "99")
        self.assertEqual(events[-1]["source"]["data"], "0")

    def test_orders_by_dispatch_time_and_persists_across_processes(self):
        now = datetime.now(timezone.utc)
        source = {"inputType": "instantCommand", "data": "stop"}
        self.store.start("stop", source, None, now)
        self.store.start("home", source, None, now - timedelta(seconds=1))
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        result = subprocess.run(
            [sys.executable, "-c",
             "import json,sys; from pathlib import Path; "
             "from input_to_cmd.command_history import CommandHistory; "
             "print(json.dumps(CommandHistory(Path(sys.argv[1])).read()))", str(self.path)],
            env=env, check=True, capture_output=True, text=True,
        )
        self.assertEqual([event["command"] for event in json.loads(result.stdout)], ["stop", "home"])

    @patch("input_to_cmd.command_dispatcher.stop", new_callable=AsyncMock)
    def test_storage_failure_is_reported_without_blocking_stop(self, stop):
        with patch.object(self.store, "start", side_effect=OSError("disk unavailable")):
            with self.assertLogs("input_to_cmd", level="ERROR"):
                response = self.client.post("/command", json={
                    "inputType": "instantCommand", "data": "stop",
                })
        stop.assert_awaited_once()
        self.assertEqual(response.status_code, 200)
        self.assertIn("history_warning", response.json())

    @patch("input_to_cmd.command_dispatcher.stop", new_callable=AsyncMock)
    def test_update_failure_is_reported(self, stop):
        with patch.object(self.store, "finish", side_effect=OSError("disk unavailable")):
            with self.assertLogs("input_to_cmd", level="ERROR"):
                response = self.client.post("/command", json={
                    "inputType": "instantCommand", "data": "stop",
                })
        stop.assert_awaited_once()
        self.assertIn("history_warning", response.json())
        self.assertEqual(self.store.read()[0]["outcome"], "pending")

    def test_read_failure_returns_error(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("invalid database", encoding="utf-8")
        with self.assertLogs("input_to_cmd", level="ERROR"):
            self.assertEqual(self.client.get("/command-history").status_code, 500)


class SequenceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "history.sqlite3"
        self.store = CommandHistory(self.path)
        history = patch("input_to_cmd.api.command_history", self.store)
        history.start()
        self.addCleanup(history.stop)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def add_point_event(self, points):
        return self.store.start("point", {"inputType": "point", "points": points}, None)

    def test_saves_selection_order_and_survives_history_eviction_and_restart(self):
        first = self.add_point_event([{"x": 0.1, "z": 0.2}])
        second = self.add_point_event([{"x": 0.3, "z": 0.4}, {"x": 1, "z": 0}])
        self.store.finish(second, "failed")
        response = self.client.post("/sequences", json={"event_ids": [second, first]})
        self.assertEqual(response.status_code, 201)
        sequence = response.json()
        self.assertEqual(sequence["event_ids"], [second, first])
        self.assertEqual(sequence["points"], [
            {"x": 0.3, "z": 0.4}, {"x": 1.0, "z": 0.0}, {"x": 0.1, "z": 0.2},
        ])
        for _ in range(100):
            self.store.start("stop", {"inputType": "instantCommand", "data": "stop"}, None)
        self.assertNotIn(first, [event["id"] for event in self.store.read()])
        self.assertEqual(self.client.get("/sequences").json(), [sequence])
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        result = subprocess.run(
            [sys.executable, "-c",
             "import json,sys; from pathlib import Path; "
             "from input_to_cmd.command_history import CommandHistory; "
             "print(json.dumps(CommandHistory(Path(sys.argv[1])).read_sequences()))", str(self.path)],
            env=env, check=True, capture_output=True, text=True,
        )
        self.assertEqual(json.loads(result.stdout), [sequence])

    def test_single_entry_with_multiple_points_can_be_saved(self):
        event_id = self.add_point_event([{"x": 0, "z": 0}, {"x": 1, "z": 1}])
        first = self.client.post(
            "/sequences", json={"event_ids": [event_id]},
        )
        second = self.client.post("/sequences", json={"event_ids": [event_id]})
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertEqual(
            [sequence["id"] for sequence in self.client.get("/sequences").json()],
            [second.json()["id"], first.json()["id"]],
        )

    def test_rejects_invalid_or_unavailable_selections(self):
        point = self.add_point_event([{"x": 0, "z": 0}])
        nonpoint = self.store.start("home", {"inputType": "instantCommand", "data": "home"}, None)
        for ids, status in (
            ([], 422), ([point], 422), ([point, point], 422),
            ([nonpoint], 422), ([999999], 409), (["1"], 422),
        ):
            with self.subTest(ids=ids):
                self.assertEqual(self.client.post("/sequences", json={"event_ids": ids}).status_code, status)
        self.assertEqual(self.client.get("/sequences").json(), [])

    def test_storage_failure_is_reported(self):
        point = self.add_point_event([{"x": 0, "z": 0}, {"x": 1, "z": 1}])
        with patch.object(self.store, "save_sequence", side_effect=OSError("disk unavailable")):
            with self.assertLogs("input_to_cmd", level="ERROR"):
                self.assertEqual(
                    self.client.post("/sequences", json={"event_ids": [point]}).status_code, 500
                )
        self.assertEqual(self.client.get("/sequences").json(), [])

    @patch("input_to_cmd.command_dispatcher.write_xz_points", new_callable=AsyncMock)
    def test_execution_maps_saved_points_and_records_sequence_source(self, write_points):
        points = [{"x": 0, "z": 0}, {"x": 0.5, "z": 0.25}, {"x": 1, "z": 1}]
        sequence = self.store.save_sequence([1, 2], points)
        response = self.client.post(f"/sequences/{sequence['id']}/execute")
        self.assertEqual(response.status_code, 200)
        write_points.assert_awaited_once_with([(16.0, 155.0), (167.5, 114.25), (319.0, -8.0)])
        event = self.store.read()[0]
        self.assertEqual(event["command"], "point")
        self.assertEqual(event["outcome"], "succeeded")
        self.assertEqual(event["source"], {
            "inputType": "point", "points": points, "sequence_id": sequence["id"],
        })
        self.assertEqual(
            self.client.post("/sequences", json={"event_ids": [event["id"]]}).status_code, 201
        )

    @patch("input_to_cmd.command_dispatcher.write_xz_points", new_callable=AsyncMock)
    def test_failed_execution_is_recorded(self, write_points):
        sequence = self.store.save_sequence(
            [1], [{"x": 0, "z": 0}, {"x": 1, "z": 1}]
        )
        write_points.side_effect = ConnectionError("OPC unavailable")
        response = self.client.post(f"/sequences/{sequence['id']}/execute")
        self.assertEqual(response.status_code, 500)
        event = self.store.read()[0]
        self.assertEqual(event["outcome"], "failed")
        self.assertEqual(event["source"]["sequence_id"], sequence["id"])

    @patch("input_to_cmd.command_dispatcher.write_xz_points", new_callable=AsyncMock)
    def test_missing_or_invalid_sequence_is_not_executed(self, write_points):
        self.assertEqual(self.client.post("/sequences/999999/execute").status_code, 404)
        sequence = self.store.save_sequence([1], [{"x": 0, "z": 0}, {"x": 1, "z": 1}])
        for points in ([{"x": 0, "z": 0}], [{"x": -0.1, "z": 0}, {"x": 1, "z": 1}]):
            with self.subTest(points=points):
                with closing(sqlite3.connect(self.path)) as db, db:
                    db.execute("UPDATE command_sequences SET points = ? WHERE id = ?",
                               (json.dumps(points), sequence["id"]))
                with self.assertLogs("input_to_cmd", level="ERROR"):
                    self.assertEqual(
                        self.client.post(f"/sequences/{sequence['id']}/execute").status_code, 500
                    )
        write_points.assert_not_awaited()
        self.assertEqual(self.store.read(), [])

    @patch("input_to_cmd.command_dispatcher.write_xz_points", new_callable=AsyncMock)
    def test_sequence_read_failure_is_reported_without_execution(self, write_points):
        with patch.object(self.store, "read_sequence", side_effect=OSError("disk unavailable")):
            with self.assertLogs("input_to_cmd", level="ERROR"):
                self.assertEqual(self.client.post("/sequences/1/execute").status_code, 500)
        write_points.assert_not_awaited()

    @patch("input_to_cmd.command_dispatcher.write_xz_points", new_callable=AsyncMock)
    def test_history_failure_does_not_prevent_sequence_execution(self, write_points):
        sequence = self.store.save_sequence([1], [{"x": 0, "z": 0}, {"x": 1, "z": 1}])
        with patch.object(self.store, "start", side_effect=OSError("disk unavailable")):
            with self.assertLogs("input_to_cmd", level="ERROR"):
                response = self.client.post(f"/sequences/{sequence['id']}/execute")
        self.assertEqual(response.status_code, 200)
        self.assertIn("history_warning", response.json())
        write_points.assert_awaited_once()


class CommandHistoryConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_in_progress_command_is_visible_then_updated(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def home():
            started.set()
            await release.wait()

        with tempfile.TemporaryDirectory() as directory:
            store = CommandHistory(Path(directory) / "history.sqlite3")
            with patch("input_to_cmd.api.command_history", store), \
                 patch("input_to_cmd.command_dispatcher.set_home", side_effect=home):
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                    request = asyncio.create_task(client.post(
                        "/command", json={"inputType": "instantCommand", "data": "home"},
                    ))
                    try:
                        await asyncio.wait_for(started.wait(), 2)
                        for _ in range(100):
                            events = (await client.get("/command-history")).json()
                            if events:
                                break
                            await asyncio.sleep(0.01)
                        self.assertEqual(events[0]["outcome"], "pending")
                    finally:
                        release.set()
                    self.assertEqual((await request).status_code, 200)
                    self.assertEqual((await client.get("/command-history")).json()[0]["outcome"], "succeeded")

    async def test_stop_dispatches_while_history_storage_is_blocked(self):
        entered = threading.Event()
        release = threading.Event()
        dispatched = asyncio.Event()

        def blocked_start(*args):
            entered.set()
            if not release.wait(5):
                raise OSError("history storage timed out")
            return 1

        async def stop():
            dispatched.set()

        with patch("input_to_cmd.api.command_history") as store, \
             patch("input_to_cmd.command_dispatcher.stop", side_effect=stop):
            store.start.side_effect = blocked_start
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                request = asyncio.create_task(client.post(
                    "/command", json={"inputType": "instantCommand", "data": "stop"},
                ))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    await asyncio.wait_for(dispatched.wait(), 2)
                    self.assertFalse(request.done())
                finally:
                    release.set()
                self.assertEqual((await request).status_code, 200)


class StepSizeCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        history = patch(
            "input_to_cmd.api.command_history",
            CommandHistory(Path(directory.name) / "history.sqlite3"),
        )
        history.start()
        self.addCleanup(history.stop)
        settings = patch.multiple(
            "input_to_cmd.api",
            AI_API_URL="https://ki.mwl.merona.de/v1",
            MODEL="smart-city-fast",
        )
        settings.start()
        self.addCleanup(settings.stop)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)


    @patch.dict("os.environ", {"TEST_KEY": "test-key"})
    @patch("input_to_cmd.command_dispatcher.move", new_callable=AsyncMock)
    @patch("input_to_cmd.api.OpenAI")
    def test_step_size_reaches_movement_for_both_input_types(self, mock_openai, mock_move):
        client = mock_openai.return_value.__enter__.return_value
        client.chat.completions.create.return_value.choices = [
            SimpleNamespace(message=SimpleNamespace(content='{"command":"move_left","stepSize":null}'))
        ]
        for payload, step_size in (
            ({"inputType": "instantCommand", "data": "move_right", "stepSize": 10}, 10),
            ({"inputType": "text", "data": "left", "stepSize": 0}, 0),
            ({"inputType": "instantCommand", "data": "move_up"}, None),
            ({"inputType": "instantCommand", "data": "move_down", "stepSize": None}, None),
        ):
            with self.subTest(payload=payload):
                mock_move.reset_mock()
                response = self.client.post("/command", json=payload)
                self.assertEqual(response.status_code, 200)
                command = "move_left" if payload["inputType"] == "text" else payload["data"]
                mock_move.assert_awaited_once_with(command, step_size=step_size)

    @patch.dict("os.environ", {"TEST_KEY": "test-key"})
    @patch("input_to_cmd.command_dispatcher.move", new_callable=AsyncMock)
    @patch("input_to_cmd.api.OpenAI")
    def test_text_distance_takes_precedence_and_missing_distance_uses_field(
        self, mock_openai, mock_move
    ):
        model = mock_openai.return_value.__enter__.return_value
        for text, extracted_size, field_size, expected_size in (
            ("fahre 10 nach links", 10, 4, 10),
            ("move left zero", 0, 4, 0),
            ("fahre nach links", None, 4, 4),
            ("move left", None, None, None),
        ):
            with self.subTest(text=text):
                mock_move.reset_mock()
                model.chat.completions.create.return_value.choices = [
                    SimpleNamespace(message=SimpleNamespace(content=json.dumps({
                        "command": "move_left", "stepSize": extracted_size,
                    })))
                ]
                response = self.client.post("/command", json={
                    "inputType": "text", "data": text, "stepSize": field_size,
                })
                self.assertEqual(response.status_code, 200)
                mock_move.assert_awaited_once_with("move_left", step_size=expected_size)
                self.assertEqual(
                    model.chat.completions.create.call_args.kwargs["messages"][-1]["content"],
                    text,
                )

    @patch.dict("os.environ", {"TEST_KEY": "test-key"})
    @patch("input_to_cmd.command_dispatcher.move", new_callable=AsyncMock)
    @patch("input_to_cmd.api.OpenAI")
    def test_invalid_or_ambiguous_model_distance_never_starts_movement(
        self, mock_openai, mock_move
    ):
        model = mock_openai.return_value.__enter__.return_value
        for result, status in (
            ('{"command":null,"stepSize":null}', 422),
            ('{"command":"move_left","stepSize":-1}', 502),
            ('{"command":"move_left","stepSize":1.5}', 502),
            ('{"command":"move_left","stepSize":true}', 502),
            ('{"command":"move_left","stepSize":"10"}', 502),
            ('{"command":"move_left"}', 502),
            ('{"command":"move_left","stepSize":null,"extra":1}', 502),
            ("move_left", 502),
            ("not json", 502),
        ):
            with self.subTest(result=result):
                model.chat.completions.create.return_value.choices = [
                    SimpleNamespace(message=SimpleNamespace(content=result))
                ]
                response = self.client.post("/command", json={
                    "inputType": "text", "data": "move left by some distance", "stepSize": 5,
                })
                self.assertEqual(response.status_code, status)
                mock_move.assert_not_awaited()

    @patch("input_to_cmd.api.OpenAI")
    @patch("input_to_cmd.command_dispatcher.move", new_callable=AsyncMock)
    def test_invalid_step_sizes_are_rejected_before_processing(self, mock_move, mock_openai):
        for step_size in (-1, 1.5, 1.0, "2", True, {}, []):
            with self.subTest(step_size=step_size):
                for input_type in ("text", "instantCommand"):
                    response = self.client.post("/command", json={
                        "inputType": input_type, "data": "move_left", "stepSize": step_size,
                    })
                    self.assertEqual(response.status_code, 422)
        mock_openai.assert_not_called()
        mock_move.assert_not_awaited()

    @patch("input_to_cmd.command_dispatcher.stop", new_callable=AsyncMock)
    @patch("input_to_cmd.command_dispatcher.set_home", new_callable=AsyncMock)
    def test_step_size_does_not_change_home_or_stop(self, mock_home, mock_stop):
        for command in ("home", "stop"):
            response = self.client.post("/command", json={
                "inputType": "instantCommand", "data": command, "stepSize": 10,
            })
            self.assertEqual(response.status_code, 200)
        mock_home.assert_awaited_once_with()
        mock_stop.assert_awaited_once_with()


class DispatcherTests(unittest.IsolatedAsyncioTestCase):
    @patch("input_to_cmd.command_dispatcher.set_home", new_callable=AsyncMock)
    async def test_dispatcher_rejects_unknown_commands(self, mock_home):
        with self.assertRaises(UnsupportedCommandError):
            await execute_command("unknown")
        mock_home.assert_not_awaited()

    @patch("input_to_cmd.command_dispatcher.stop", new_callable=AsyncMock)
    @patch("input_to_cmd.command_dispatcher.move", new_callable=AsyncMock)
    async def test_dispatcher_routes_movement_and_stop(self, move, stop):
        for command in ("move_left", "move_right", "move_down", "move_up"):
            await execute_command(command)
        await execute_command("stop")
        self.assertEqual([call.args for call in move.await_args_list], [
            ("move_left",), ("move_right",), ("move_down",), ("move_up",),
        ])
        self.assertTrue(all(call.kwargs == {"step_size": None} for call in move.await_args_list))
        stop.assert_awaited_once_with()

    @patch("input_to_cmd.command_dispatcher.get_running_movement", return_value=None)
    @patch("input_to_cmd.command_dispatcher.write_xz_points")
    async def test_point_command_is_tracked_and_cleared_on_failure(self, write_points, movement):
        started = asyncio.Event()
        release = asyncio.Event()

        async def run_points(points):
            started.set()
            await release.wait()
            raise RuntimeError("point failed")

        write_points.side_effect = run_points
        points = [(50.0, 30.0)]
        task = asyncio.create_task(execute_command("point", points))
        await asyncio.wait_for(started.wait(), timeout=1)
        self.assertEqual(command_dispatcher.get_running_command(), "point")
        release.set()
        with self.assertRaisesRegex(RuntimeError, "point failed"):
            await task
        write_points.assert_awaited_once_with(points)
        self.assertIsNone(command_dispatcher.get_running_command())

    @patch("input_to_cmd.command_dispatcher.write_xz_points", new_callable=AsyncMock)
    async def test_point_command_requires_points(self, write_points):
        for points in (None, []):
            with self.subTest(points=points), self.assertRaises(ValueError):
                await execute_command("point", points)
        write_points.assert_not_awaited()

    @patch("input_to_cmd.command_dispatcher.get_running_movement", return_value="move_up")
    @patch("input_to_cmd.command_dispatcher.set_home")
    async def test_home_is_reported_until_it_finishes_or_fails(self, home, movement):
        started = asyncio.Event()
        release = asyncio.Event()

        async def run_home():
            started.set()
            await release.wait()
            raise RuntimeError("home failed")

        home.side_effect = run_home
        task = asyncio.create_task(execute_command("home"))
        await asyncio.wait_for(started.wait(), timeout=1)
        self.assertEqual(command_dispatcher.get_running_command(), "home")
        release.set()
        with self.assertRaisesRegex(RuntimeError, "home failed"):
            await task
        self.assertEqual(command_dispatcher.get_running_command(), "move_up")

    @patch("input_to_cmd.command_dispatcher.get_running_movement", return_value=None)
    @patch("input_to_cmd.command_dispatcher.stop")
    async def test_stop_is_reported_while_in_progress(self, stop, movement):
        started = asyncio.Event()
        release = asyncio.Event()

        async def run_stop():
            started.set()
            await release.wait()

        stop.side_effect = run_stop
        task = asyncio.create_task(execute_command("stop"))
        await asyncio.wait_for(started.wait(), timeout=1)
        self.assertEqual(command_dispatcher.get_running_command(), "stop")
        release.set()
        await task
        self.assertIsNone(command_dispatcher.get_running_command())


class StatusSocketTests(unittest.TestCase):
    @staticmethod
    def receive_history(socket, message_type):
        for _ in range(100):
            message = socket.receive_json()
            if message.get("type") == message_type:
                return message
        raise AssertionError(f"Did not receive {message_type}")

    @patch("input_to_cmd.api.STATUS_POLL_INTERVAL_SECONDS", 0.01)
    @patch("input_to_cmd.api.get_position", new_callable=AsyncMock, return_value=(20.0, 30.0))
    @patch("input_to_cmd.api.execute_command", new_callable=AsyncMock)
    def test_history_snapshot_and_updates_are_pushed_to_each_socket(self, execute, position):
        with tempfile.TemporaryDirectory() as directory:
            store = CommandHistory(Path(directory) / "history.sqlite3")
            for index in range(12):
                event_id = store.start("home", {"inputType": "text", "data": str(index)}, None)
                store.finish(event_id, "succeeded")
            with patch("input_to_cmd.api.command_history", store):
                with TestClient(app) as client:
                    with client.websocket_connect("/ws/status?history=1") as first, \
                         client.websocket_connect("/ws/status?history=1") as second:
                        for socket in (first, second):
                            snapshot = self.receive_history(socket, "history_snapshot")["events"]
                            self.assertEqual(len(snapshot), 10)
                            self.assertEqual(snapshot[0]["source"]["data"], "11")
                            self.assertEqual(snapshot[-1]["source"]["data"], "2")
                        response = client.post("/command", json={
                            "inputType": "instantCommand", "data": "stop",
                        })
                        self.assertEqual(response.status_code, 200)
                        for socket in (first, second):
                            pending = self.receive_history(socket, "history_event")["event"]
                            completed = self.receive_history(socket, "history_event")["event"]
                            self.assertEqual(pending["command"], "stop")
                            self.assertEqual(pending["outcome"], "pending")
                            self.assertEqual(completed["id"], pending["id"])
                            self.assertEqual(completed["outcome"], "succeeded")
                    with client.websocket_connect("/ws/status?history=1") as reconnected:
                        snapshot = self.receive_history(reconnected, "history_snapshot")["events"]
                        self.assertEqual(len(snapshot), 10)
                        self.assertEqual(snapshot[0]["command"], "stop")
                        self.assertEqual(snapshot[0]["outcome"], "succeeded")

    @patch("input_to_cmd.api.STATUS_POLL_INTERVAL_SECONDS", 0.01)
    @patch("input_to_cmd.api.get_position", new_callable=AsyncMock, return_value=(20.0, 30.0))
    @patch("input_to_cmd.api.execute_command", new_callable=AsyncMock)
    def test_command_during_snapshot_is_not_missed(self, execute, position):
        entered = threading.Event()
        release = threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            store = CommandHistory(Path(directory) / "history.sqlite3")
            read = store.read

            def blocked_read(limit=100):
                entered.set()
                if not release.wait(5):
                    raise OSError("snapshot timed out")
                return read(limit)

            with patch.object(store, "read", side_effect=blocked_read), \
                 patch("input_to_cmd.api.command_history", store):
                with TestClient(app) as client:
                    with client.websocket_connect("/ws/status?history=1") as socket:
                        try:
                            self.assertTrue(entered.wait(2))
                            self.assertEqual(client.post("/command", json={
                                "inputType": "instantCommand", "data": "stop",
                            }).status_code, 200)
                        finally:
                            release.set()
                        snapshot = self.receive_history(socket, "history_snapshot")["events"]
                        pending = self.receive_history(socket, "history_event")["event"]
                        completed = self.receive_history(socket, "history_event")["event"]
                        self.assertEqual(snapshot[0]["id"], pending["id"])
                        self.assertEqual(snapshot[0]["outcome"], "succeeded")
                        self.assertEqual(pending["outcome"], "pending")
                        self.assertEqual(completed["outcome"], "succeeded")

    @patch("input_to_cmd.api.STATUS_POLL_INTERVAL_SECONDS", 0.01)
    @patch("input_to_cmd.api.get_position", new_callable=AsyncMock, return_value=(20.0, 30.0))
    @patch("input_to_cmd.api.execute_command", new_callable=AsyncMock)
    def test_failed_command_is_pushed(self, execute, position):
        execute.side_effect = ConnectionError("OPC unavailable")
        with tempfile.TemporaryDirectory() as directory:
            store = CommandHistory(Path(directory) / "history.sqlite3")
            with patch("input_to_cmd.api.command_history", store):
                with TestClient(app, raise_server_exceptions=False) as client:
                    with client.websocket_connect("/ws/status?history=1") as socket:
                        self.receive_history(socket, "history_snapshot")
                        response = client.post("/command", json={
                            "inputType": "instantCommand", "data": "stop",
                        })
                        self.assertEqual(response.status_code, 500)
                        self.assertEqual(
                            self.receive_history(socket, "history_event")["event"]["outcome"], "pending"
                        )
                        self.assertEqual(
                            self.receive_history(socket, "history_event")["event"]["outcome"], "failed"
                        )

    @patch("input_to_cmd.api.STATUS_POLL_INTERVAL_SECONDS", 0.01)
    @patch("input_to_cmd.api.get_position", new_callable=AsyncMock, return_value=(20.0, 30.0))
    def test_history_snapshot_read_failure_is_reported(self, position):
        with patch("input_to_cmd.api.command_history") as store:
            store.read.side_effect = OSError("history unavailable")
            with self.assertLogs("input_to_cmd", level="ERROR"):
                with TestClient(app) as client:
                    with client.websocket_connect("/ws/status?history=1") as socket:
                        self.assertEqual(
                            self.receive_history(socket, "history_error")["error"],
                            "History unavailable",
                        )

    @patch("input_to_cmd.api.STATUS_POLL_INTERVAL_SECONDS", 0.01)
    @patch("input_to_cmd.api.get_running_command", side_effect=["move_left", None])
    @patch("input_to_cmd.api.get_position", new_callable=AsyncMock)
    def test_sends_updated_position_and_command(self, position, command):
        position.side_effect = [(20.0, 30.0), (16.0, 31.0)]
        with TestClient(app) as client:
            with client.websocket_connect("/ws/status") as socket:
                self.assertEqual(socket.receive_json(), {
                    "command": "move_left", "position": {"x": 20.0, "z": 30.0}, "error": None,
                })
                self.assertEqual(socket.receive_json(), {
                    "command": None, "position": {"x": 16.0, "z": 31.0}, "error": None,
                })

    @patch("input_to_cmd.api.STATUS_POLL_INTERVAL_SECONDS", 0.01)
    @patch("input_to_cmd.api.get_running_command", return_value=None)
    @patch("input_to_cmd.api.get_position", new_callable=AsyncMock)
    def test_reports_position_failure_and_recovers(self, position, command):
        position.side_effect = [ConnectionError("OPC unavailable"), (1.0, 2.0)]
        with self.assertLogs("input_to_cmd", level="ERROR"):
            with TestClient(app) as client:
                with client.websocket_connect("/ws/status") as socket:
                    self.assertEqual(socket.receive_json(), {
                        "command": None, "position": None, "error": "Position unavailable",
                    })
                    self.assertEqual(socket.receive_json(), {
                        "command": None, "position": {"x": 1.0, "z": 2.0}, "error": None,
                    })

    @patch("input_to_cmd.api.CORS_ALLOWED_ORIGINS", ["https://allowed.example"])
    def test_rejects_disallowed_browser_origin(self):
        from starlette.websockets import WebSocketDisconnect

        with TestClient(app) as client:
            with self.assertRaises(WebSocketDisconnect) as error:
                with client.websocket_connect(
                    "/ws/status", headers={"origin": "https://other.example"}
                ):
                    pass
        self.assertEqual(error.exception.code, 1008)


class OpcClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await opc_client.shutdown_connection()

    @patch("input_to_cmd.opc_client.create_client")
    async def test_concurrent_position_reads_share_one_connection(self, create_client):
        context = create_client.return_value
        client = context.__aenter__.return_value
        connecting = asyncio.Event()
        connected = asyncio.Event()

        async def connect():
            connecting.set()
            await connected.wait()
            return client

        context.__aenter__.side_effect = connect
        node = MagicMock()
        node.read_value = AsyncMock(return_value=12.0)
        client.get_node = MagicMock(return_value=node)

        reads = asyncio.gather(*(opc_client.get_position() for _ in range(10)))
        await connecting.wait()
        context.__aenter__.assert_awaited_once_with()
        connected.set()
        results = await reads

        self.assertEqual(results, [(12.0, 12.0)] * 10)
        create_client.assert_called_once_with()
        context.__aenter__.assert_awaited_once_with()
        context.__aexit__.assert_not_awaited()
        await opc_client.shutdown_connection()
        context.__aexit__.assert_awaited_once_with(None, None, None)

    @patch("input_to_cmd.opc_client.create_client")
    async def test_position_and_movement_reuse_connection_after_limit(self, create_client):
        context = create_client.return_value
        client = context.__aenter__.return_value
        jog, x, z = MagicMock(), MagicMock(), MagicMock()
        jog.write_value = AsyncMock()
        x.read_value = AsyncMock(return_value=20.0)
        z.read_value = AsyncMock(return_value=30.0)
        client.get_node = MagicMock(side_effect={
            opc_client.X_JOG_NEG_NODE_ID: jog,
            opc_client.X_POSITION_NODE_ID: x,
            opc_client.Z_POSITION_NODE_ID: z,
        }.__getitem__)

        self.assertEqual(await opc_client.get_position(), (20.0, 30.0))
        await opc_client.move("move_left")
        self.assertEqual(await opc_client.get_position(), (20.0, 30.0))
        x.read_value.return_value = 16.0
        await asyncio.wait_for(opc_client._movement_task, timeout=1)
        self.assertEqual(await opc_client.get_position(), (16.0, 30.0))
        create_client.assert_called_once_with()
        context.__aenter__.assert_awaited_once_with()
        context.__aexit__.assert_not_awaited()
        self.assertEqual([call.args for call in jog.write_value.await_args_list], [
            (True,), (False,),
        ])

    @patch("input_to_cmd.opc_client.create_client")
    async def test_cancelled_connection_is_closed_before_retry(self, create_client):
        context = create_client.return_value
        connecting = asyncio.Event()

        async def connect():
            connecting.set()
            await asyncio.Future()

        context.__aenter__.side_effect = connect
        task = asyncio.create_task(opc_client.get_position())
        await connecting.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        context.__aexit__.assert_awaited_once_with(None, None, None)
        self.assertIsNone(opc_client._client_context)

        context.__aenter__.side_effect = None
        node = MagicMock()
        node.read_value = AsyncMock(return_value=1.0)
        context.__aenter__.return_value.get_node = MagicMock(return_value=node)
        self.assertEqual(await opc_client.get_position(), (1.0, 1.0))

    @patch("input_to_cmd.opc_client.create_client")
    async def test_get_running_command_reports_active_movement(self, create_client):
        jog, position = MagicMock(), MagicMock()
        jog.write_value = AsyncMock()
        read_count = 0

        async def read_position():
            nonlocal read_count
            read_count += 1
            if read_count > 1:
                await asyncio.Future()
            return 20.0

        position.read_value = AsyncMock(side_effect=read_position)
        client = create_client.return_value.__aenter__.return_value
        client.get_node = MagicMock(side_effect=lambda node: {
            opc_client.X_JOG_NEG_NODE_ID: jog,
            opc_client.X_POSITION_NODE_ID: position,
        }[node])

        await opc_client.move("move_left")
        self.assertEqual(opc_client.get_running_command(), "move_left")

        await opc_client.shutdown_connection()
        self.assertIsNone(opc_client.get_running_command())
        create_client.return_value.__aexit__.assert_awaited_once_with(None, None, None)

    @patch("input_to_cmd.opc_client.create_client")
    async def test_get_position_returns_x_and_z_coordinates(self, create_client):
        x_position, z_position = MagicMock(), MagicMock()
        x_position.read_value = AsyncMock(return_value=123.4)
        z_position.read_value = AsyncMock(return_value=56.7)
        client = create_client.return_value.__aenter__.return_value
        client.get_node = MagicMock(side_effect=lambda node: {
            opc_client.X_POSITION_NODE_ID: x_position,
            opc_client.Z_POSITION_NODE_ID: z_position,
        }[node])

        position = await opc_client.get_position()

        self.assertEqual(position, (123.4, 56.7))
        x_position.read_value.assert_awaited_once_with()
        z_position.read_value.assert_awaited_once_with()

    @patch("input_to_cmd.opc_client.asyncio.sleep", new_callable=AsyncMock)
    @patch("input_to_cmd.opc_client.create_client")
    async def test_home_waits_for_stable_velocities_and_resets(self, create_client, sleep):
        home, x, z = MagicMock(), MagicMock(), MagicMock()
        home.write_value = AsyncMock()
        x.read_value = AsyncMock(side_effect=[0, 1, 0, 0, 0])
        z.read_value = AsyncMock(return_value=0)
        client = create_client.return_value.__aenter__.return_value
        client.get_node = MagicMock(side_effect=lambda node: {
            opc_client.HOME_NODE_ID: home,
            opc_client.X_VELOCITY_NODE_ID: x,
            opc_client.Z_VELOCITY_NODE_ID: z,
        }[node])
        await opc_client.set_home()
        create_client.assert_called_once_with()
        self.assertEqual(home.write_value.await_args_list[0].args, (True,))
        self.assertEqual(home.write_value.await_args_list[1].args, (False,))
        self.assertEqual(x.read_value.await_count, 5)
        self.assertEqual(sleep.await_count, 4)

    @patch("input_to_cmd.opc_client.create_client")
    async def test_home_resets_after_read_failure(self, create_client):
        home, x, z = MagicMock(), MagicMock(), MagicMock()
        home.write_value = AsyncMock()
        x.read_value = AsyncMock(side_effect=RuntimeError("read failed"))
        client = create_client.return_value.__aenter__.return_value
        client.get_node = MagicMock(side_effect=lambda node: {
            opc_client.HOME_NODE_ID: home,
            opc_client.X_VELOCITY_NODE_ID: x,
            opc_client.Z_VELOCITY_NODE_ID: z,
        }[node])
        with self.assertLogs("input_to_cmd.opc_client", level="ERROR"):
            with self.assertRaisesRegex(RuntimeError, "read failed"):
                await opc_client.set_home()
        self.assertEqual([call.args for call in home.write_value.await_args_list], [(True,), (False,)])

    @patch("input_to_cmd.opc_client.asyncio.sleep", new_callable=AsyncMock)
    @patch("input_to_cmd.opc_client.create_client")
    async def test_move_runs_until_limit_then_resets(self, create_client, sleep):
        jog, position = MagicMock(), MagicMock()
        jog.write_value = AsyncMock()
        position.read_value = AsyncMock(side_effect=[20.0, 16.0])
        client = create_client.return_value.__aenter__.return_value
        client.get_node = MagicMock(side_effect=lambda node: {
            opc_client.X_JOG_NEG_NODE_ID: jog,
            opc_client.X_POSITION_NODE_ID: position,
        }[node])
        await opc_client.move("move_left")
        await opc_client._movement_task
        self.assertEqual([call.args for call in jog.write_value.await_args_list], [
            (True,), (False,),
        ])
        self.assertEqual(position.read_value.await_count, 2)
        sleep.assert_not_awaited()

    @patch("input_to_cmd.opc_client.asyncio.sleep", new_callable=AsyncMock)
    @patch("input_to_cmd.opc_client.create_client")
    async def test_move_stops_at_step_size_in_each_direction(self, create_client, sleep):
        client = create_client.return_value.__aenter__.return_value
        cases = (
            ("move_left", opc_client.X_JOG_NEG_NODE_ID, opc_client.X_POSITION_NODE_ID, 50.0, 49.0, 48.0),
            ("move_right", opc_client.X_JOG_POS_NODE_ID, opc_client.X_POSITION_NODE_ID, 50.0, 51.0, 52.0),
            ("move_down", opc_client.Z_JOG_NEG_NODE_ID, opc_client.Z_POSITION_NODE_ID, 50.0, 49.0, 47.0),
            ("move_up", opc_client.Z_JOG_POS_NODE_ID, opc_client.Z_POSITION_NODE_ID, 50.0, 51.0, 53.0),
        )
        for command, jog_id, position_id, start, intermediate, end in cases:
            with self.subTest(command=command):
                jog, position = MagicMock(), MagicMock()
                jog.write_value = AsyncMock()
                position.read_value = AsyncMock(side_effect=[start, intermediate, end])
                client.get_node = MagicMock(side_effect={jog_id: jog, position_id: position}.__getitem__)
                sleep.reset_mock()
                await opc_client.move(command, step_size=2.0)
                await opc_client._movement_task
                self.assertEqual([call.args for call in jog.write_value.await_args_list], [
                    (True,), (False,),
                ])
                self.assertEqual(position.read_value.await_count, 3)
                sleep.assert_awaited_once_with(opc_client.MOVE_POLL_INTERVAL_SECONDS)

    @patch("input_to_cmd.opc_client.create_client")
    async def test_move_zero_step_does_not_start_jog(self, create_client):
        jog, position = MagicMock(), MagicMock()
        jog.write_value = AsyncMock()
        position.read_value = AsyncMock(return_value=50.0)
        client = create_client.return_value.__aenter__.return_value
        client.get_node = MagicMock(side_effect={
            opc_client.X_JOG_NEG_NODE_ID: jog,
            opc_client.X_POSITION_NODE_ID: position,
        }.__getitem__)
        await opc_client.move("move_left", step_size=0)
        jog.write_value.assert_awaited_once_with(False)
        position.read_value.assert_awaited_once_with()

    @patch("input_to_cmd.opc_client.create_client")
    async def test_move_rejects_invalid_step_sizes_before_stopping_active_move(self, create_client):
        jog, position = MagicMock(), MagicMock()
        jog.write_value = AsyncMock()
        position.read_value = AsyncMock(return_value=50.0)
        client = create_client.return_value.__aenter__.return_value
        client.get_node = MagicMock(side_effect={
            opc_client.X_JOG_NEG_NODE_ID: jog,
            opc_client.X_POSITION_NODE_ID: position,
        }.__getitem__)
        await opc_client.move("move_left")
        for step_size in (-1, float("nan"), float("inf"), True, "2"):
            with self.subTest(step_size=step_size):
                with self.assertRaisesRegex(ValueError, "step_size"):
                    await opc_client.move("move_left", step_size=step_size)
                self.assertEqual(opc_client.get_running_command(), "move_left")
        jog.write_value.assert_awaited_once_with(True)

    @patch("input_to_cmd.opc_client.create_client")
    async def test_move_with_step_size_still_stops_at_axis_limit(self, create_client):
        jog, position = MagicMock(), MagicMock()
        jog.write_value = AsyncMock()
        position.read_value = AsyncMock(side_effect=[20.0, 16.0])
        client = create_client.return_value.__aenter__.return_value
        client.get_node = MagicMock(side_effect={
            opc_client.X_JOG_NEG_NODE_ID: jog,
            opc_client.X_POSITION_NODE_ID: position,
        }.__getitem__)
        await opc_client.move("move_left", step_size=10.0)
        await opc_client._movement_task
        self.assertEqual([call.args for call in jog.write_value.await_args_list], [(True,), (False,)])

    @patch("input_to_cmd.opc_client.create_client")
    async def test_step_monitor_stops_after_move_returns(self, create_client):
        jog, position = MagicMock(), MagicMock()
        jog.write_value = AsyncMock()
        allow_read = asyncio.Event()
        reads = 0

        async def read_position():
            nonlocal reads
            reads += 1
            if reads == 1:
                return 50.0
            await allow_read.wait()
            return 52.0

        position.read_value = AsyncMock(side_effect=read_position)
        client = create_client.return_value.__aenter__.return_value
        client.get_node = MagicMock(side_effect={
            opc_client.X_JOG_POS_NODE_ID: jog,
            opc_client.X_POSITION_NODE_ID: position,
        }.__getitem__)
        await opc_client.move("move_right", step_size=2.0)
        self.assertEqual(opc_client.get_running_command(), "move_right")
        jog.write_value.assert_awaited_once_with(True)
        allow_read.set()
        await asyncio.wait_for(opc_client._movement_task, timeout=1)
        self.assertIsNone(opc_client.get_running_command())
        self.assertEqual([call.args for call in jog.write_value.await_args_list], [(True,), (False,)])

    @patch("input_to_cmd.opc_client.create_client")
    async def test_move_does_not_start_at_limit(self, create_client):
        jog, position = MagicMock(), MagicMock()
        jog.write_value = AsyncMock()
        position.read_value = AsyncMock(return_value=155.0)
        client = create_client.return_value.__aenter__.return_value
        client.get_node = MagicMock(side_effect=lambda node: {
            opc_client.Z_JOG_POS_NODE_ID: jog,
            opc_client.Z_POSITION_NODE_ID: position,
        }[node])
        await opc_client.move("move_up")
        jog.write_value.assert_awaited_once_with(False)
        position.read_value.assert_awaited_once_with()

    @patch("input_to_cmd.opc_client.create_client")
    async def test_move_returns_and_continues_until_stop_after_five_seconds(self, create_client):
        nodes = {node: MagicMock() for node in (
            opc_client.X_JOG_NEG_NODE_ID, opc_client.X_JOG_POS_NODE_ID,
            opc_client.Z_JOG_NEG_NODE_ID, opc_client.Z_JOG_POS_NODE_ID,
        )}
        for node in nodes.values():
            node.write_value = AsyncMock()
        position = MagicMock()
        position.read_value = AsyncMock(return_value=20.0)
        nodes[opc_client.X_POSITION_NODE_ID] = position
        client = create_client.return_value.__aenter__.return_value
        client.get_node = MagicMock(side_effect=nodes.__getitem__)
        try:
            await asyncio.wait_for(opc_client.move("move_left"), timeout=1)
            self.assertFalse(opc_client._movement_task.done())
            await asyncio.sleep(5.2)
            self.assertFalse(opc_client._movement_task.done())
            self.assertEqual(
                [call.args for call in nodes[opc_client.X_JOG_NEG_NODE_ID].write_value.await_args_list],
                [(True,)],
            )
            await opc_client.stop()
            create_client.assert_called_once_with()
            self.assertTrue(opc_client._movement_task is None)
            for node_id in (
                opc_client.X_JOG_NEG_NODE_ID, opc_client.X_JOG_POS_NODE_ID,
                opc_client.Z_JOG_NEG_NODE_ID, opc_client.Z_JOG_POS_NODE_ID,
            ):
                self.assertEqual(nodes[node_id].write_value.await_args_list[-1].args, (False,))
        finally:
            await opc_client.shutdown_movement()

    @patch("input_to_cmd.opc_client.create_client")
    async def test_new_move_stops_previous_before_starting(self, create_client):
        writes = []
        nodes = {}
        for node_id in (opc_client.X_JOG_NEG_NODE_ID, opc_client.X_JOG_POS_NODE_ID):
            node = MagicMock()
            async def write(value, node_id=node_id):
                writes.append((node_id, value))
            node.write_value = AsyncMock(side_effect=write)
            nodes[node_id] = node
        position = MagicMock()
        position.read_value = AsyncMock(return_value=20.0)
        nodes[opc_client.X_POSITION_NODE_ID] = position
        client = create_client.return_value.__aenter__.return_value
        client.get_node = MagicMock(side_effect=nodes.__getitem__)
        try:
            await opc_client.move("move_left")
            await opc_client.move("move_right")
            self.assertEqual(writes[:3], [
                (opc_client.X_JOG_NEG_NODE_ID, True),
                (opc_client.X_JOG_NEG_NODE_ID, False),
                (opc_client.X_JOG_POS_NODE_ID, True),
            ])
        finally:
            await opc_client.shutdown_movement()

    @patch("input_to_cmd.opc_client.create_client")
    async def test_move_start_failure_is_reported_and_resets(self, create_client):
        jog, position = MagicMock(), MagicMock()
        jog.write_value = AsyncMock()
        position.read_value = AsyncMock(side_effect=RuntimeError("position read failed"))
        client = create_client.return_value.__aenter__.return_value
        client.get_node = MagicMock(side_effect=lambda node: {
            opc_client.X_JOG_NEG_NODE_ID: jog,
            opc_client.X_POSITION_NODE_ID: position,
        }[node])
        with self.assertLogs("input_to_cmd.opc_client", level="ERROR"):
            with self.assertRaisesRegex(RuntimeError, "position read failed"):
                await opc_client.move("move_left")
            await asyncio.sleep(0)
        jog.write_value.assert_awaited_once_with(False)

    @patch("input_to_cmd.opc_client.create_client")
    async def test_monitor_failure_stops_movement_and_is_logged(self, create_client):
        jog, position = MagicMock(), MagicMock()
        jog.write_value = AsyncMock()
        position.read_value = AsyncMock(side_effect=[20.0, RuntimeError("position read failed")])
        client = create_client.return_value.__aenter__.return_value
        client.get_node = MagicMock(side_effect=lambda node: {
            opc_client.X_JOG_NEG_NODE_ID: jog,
            opc_client.X_POSITION_NODE_ID: position,
        }[node])
        with self.assertLogs("input_to_cmd.opc_client", level="ERROR"):
            await opc_client.move("move_left")
            with self.assertRaisesRegex(RuntimeError, "position read failed"):
                await opc_client._movement_task
            await asyncio.sleep(0)
        self.assertEqual([call.args for call in jog.write_value.await_args_list], [(True,), (False,)])
        with self.assertRaisesRegex(RuntimeError, "position read failed"):
            await opc_client.shutdown_movement()

    @patch("input_to_cmd.opc_client.create_client")
    async def test_stop_waits_for_inflight_start_then_clears_all_nodes(self, create_client):
        starting = asyncio.Event()
        allow_start = asyncio.Event()
        nodes = {node: MagicMock() for node in (
            opc_client.X_JOG_NEG_NODE_ID, opc_client.X_JOG_POS_NODE_ID,
            opc_client.Z_JOG_NEG_NODE_ID, opc_client.Z_JOG_POS_NODE_ID,
        )}
        for node in nodes.values():
            node.write_value = AsyncMock()
        async def write_left(value):
            if value:
                starting.set()
                await allow_start.wait()
        nodes[opc_client.X_JOG_NEG_NODE_ID].write_value = AsyncMock(side_effect=write_left)
        position = MagicMock()
        position.read_value = AsyncMock(return_value=20.0)
        nodes[opc_client.X_POSITION_NODE_ID] = position
        client = create_client.return_value.__aenter__.return_value
        client.get_node = MagicMock(side_effect=nodes.__getitem__)
        move_task = asyncio.create_task(opc_client.move("move_left"))
        await asyncio.wait_for(starting.wait(), timeout=1)
        stop_task = asyncio.create_task(opc_client.stop())
        await asyncio.sleep(0)
        self.assertFalse(stop_task.done())
        allow_start.set()
        await asyncio.wait_for(asyncio.gather(move_task, stop_task), timeout=1)
        self.assertIsNone(opc_client._movement_task)
        for node_id in (
            opc_client.X_JOG_NEG_NODE_ID, opc_client.X_JOG_POS_NODE_ID,
            opc_client.Z_JOG_NEG_NODE_ID, opc_client.Z_JOG_POS_NODE_ID,
        ):
            self.assertEqual(nodes[node_id].write_value.await_args_list[-1].args, (False,))

    @patch("input_to_cmd.opc_client.create_client")
    async def test_cancelled_move_start_resets_jog_node(self, create_client):
        starting = asyncio.Event()
        jog, position = MagicMock(), MagicMock()
        async def write(value):
            if value:
                starting.set()
                await asyncio.Event().wait()
        jog.write_value = AsyncMock(side_effect=write)
        position.read_value = AsyncMock(return_value=20.0)
        client = create_client.return_value.__aenter__.return_value
        client.get_node = MagicMock(side_effect=lambda node: {
            opc_client.X_JOG_NEG_NODE_ID: jog,
            opc_client.X_POSITION_NODE_ID: position,
        }[node])
        task = asyncio.create_task(opc_client.move("move_left"))
        await asyncio.wait_for(starting.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual([call.args for call in jog.write_value.await_args_list], [(True,), (False,)])
        self.assertIsNone(opc_client._movement_task)

    @patch("input_to_cmd.opc_client.create_client")
    async def test_monitor_stops_at_limit_after_move_returns(self, create_client):
        jog, position = MagicMock(), MagicMock()
        jog.write_value = AsyncMock()
        position.read_value = AsyncMock(return_value=20.0)
        client = create_client.return_value.__aenter__.return_value
        client.get_node = MagicMock(side_effect=lambda node: {
            opc_client.X_JOG_NEG_NODE_ID: jog,
            opc_client.X_POSITION_NODE_ID: position,
        }[node])
        await opc_client.move("move_left")
        self.assertFalse(opc_client._movement_task.done())
        position.read_value.return_value = 16.0
        await asyncio.wait_for(opc_client._movement_task, timeout=1)
        self.assertEqual([call.args for call in jog.write_value.await_args_list], [(True,), (False,)])
        await opc_client.shutdown_movement()

    @patch.dict("os.environ", {"TEST_KEY": "test-key"})
    @patch("input_to_cmd.opc_client.create_client")
    @patch("input_to_cmd.api.OpenAI")
    async def test_api_move_monitor_survives_response_and_stop_cancels_it(self, mock_openai, create_client):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        history = patch(
            "input_to_cmd.api.command_history",
            CommandHistory(Path(directory.name) / "history.sqlite3"),
        )
        history.start()
        self.addCleanup(history.stop)
        nodes = {node: MagicMock() for node in (
            opc_client.X_JOG_NEG_NODE_ID, opc_client.X_JOG_POS_NODE_ID,
            opc_client.Z_JOG_NEG_NODE_ID, opc_client.Z_JOG_POS_NODE_ID,
        )}
        for node in nodes.values():
            node.write_value = AsyncMock()
        position = MagicMock()
        position.read_value = AsyncMock(return_value=20.0)
        nodes[opc_client.X_POSITION_NODE_ID] = position
        client = create_client.return_value.__aenter__.return_value
        client.get_node = MagicMock(side_effect=nodes.__getitem__)
        model = mock_openai.return_value.__enter__.return_value
        model.chat.completions.create.side_effect = [
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({
                "command": command, "stepSize": None,
            })))])
            for command in ("move_left", "stop")
        ]
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as api:
                response = await api.post("/command", json={"inputType": "text", "data": "left"})
                self.assertEqual(response.status_code, 200)
                self.assertFalse(opc_client._movement_task.done())
                response = await api.post("/command", json={"inputType": "text", "data": "stop"})
                self.assertEqual(response.status_code, 200)
                self.assertIsNone(opc_client._movement_task)
                self.assertEqual(nodes[opc_client.X_JOG_NEG_NODE_ID].write_value.await_args_list[-1].args, (False,))
        finally:
            await opc_client.shutdown_movement()

    @patch("input_to_cmd.opc_client.create_client")
    async def test_stop_clears_all_jog_nodes(self, create_client):
        jog_nodes = {node: MagicMock() for node in (
            opc_client.X_JOG_NEG_NODE_ID,
            opc_client.X_JOG_POS_NODE_ID,
            opc_client.Z_JOG_NEG_NODE_ID,
            opc_client.Z_JOG_POS_NODE_ID,
        )}
        for node in jog_nodes.values():
            node.write_value = AsyncMock()
        client = create_client.return_value.__aenter__.return_value
        client.get_node = MagicMock(side_effect=jog_nodes.__getitem__)
        await opc_client.stop()
        for node in jog_nodes.values():
            node.write_value.assert_awaited_once_with(False)

    @patch("input_to_cmd.opc_client.create_client")
    async def test_stop_attempts_every_node_when_a_write_fails(self, create_client):
        error = RuntimeError("write failed")
        jog_nodes = {node: MagicMock() for node in (
            opc_client.X_JOG_NEG_NODE_ID,
            opc_client.X_JOG_POS_NODE_ID,
            opc_client.Z_JOG_NEG_NODE_ID,
            opc_client.Z_JOG_POS_NODE_ID,
        )}
        for node in jog_nodes.values():
            node.write_value = AsyncMock()
        jog_nodes[opc_client.X_JOG_NEG_NODE_ID].write_value.side_effect = error
        client = create_client.return_value.__aenter__.return_value
        client.get_node = MagicMock(side_effect=jog_nodes.__getitem__)
        with self.assertLogs("input_to_cmd.opc_client", level="ERROR"):
            with self.assertRaisesRegex(RuntimeError, "write failed"):
                await opc_client.stop()
        for node in jog_nodes.values():
            node.write_value.assert_awaited_once_with(False)


class ConfigurationTests(unittest.TestCase):
    @patch.dict("os.environ", {"TEST_KEY": "test-key"})
    @patch("input_to_cmd.__main__.uvicorn.run")
    def test_main_starts_extracted_app(self, mock_run):
        from input_to_cmd.__main__ import HOST, PORT, main

        main()
        mock_run.assert_called_once_with(app, host=HOST, port=PORT)

    def test_cors_preflight_and_post_response(self):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        env["CORS_ALLOWED_ORIGINS"] = " http://frontend.test:3000, http://other.test "
        script = """
import json
from types import SimpleNamespace
from unittest.mock import patch
from fastapi.testclient import TestClient
from input_to_cmd.api import app

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
     patch("input_to_cmd.api.OpenAI") as ai, \\
     patch("input_to_cmd.command_dispatcher.set_home") as home, \\
     patch("builtins.print"):
    ai.return_value.__enter__.return_value.chat.completions.create.return_value.choices = [
        SimpleNamespace(message=SimpleNamespace(content='{"command":"home","stepSize":null}'))
    ]
    success = client.post("/command", headers={"Origin": "http://frontend.test:3000"},
                          json={"inputType": "text", "data": "Home"})
print(json.dumps([allowed.status_code, allowed.headers.get("access-control-allow-origin"),
                  allowed.headers.get("access-control-allow-methods"),
                  denied.status_code, denied.headers.get("access-control-allow-origin"),
                  denied_header.status_code, post.status_code,
                  post.headers.get("access-control-allow-origin"), success.status_code,
                  success.headers.get("access-control-allow-origin")]))
"""
        with tempfile.TemporaryDirectory() as directory:
            env["COMMAND_HISTORY_DB_PATH"] = str(Path(directory) / "history.sqlite3")
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
            "from unittest.mock import patch; import input_to_cmd.__main__ as runner; "
            "import input_to_cmd.api as config; "
            "patcher = patch.object(runner.uvicorn, 'run'); patcher.start(); "
            "runner.main(); config.logger.debug('debug message'); patcher.stop()"
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
        names = ("HOST", "PORT", "MODEL", "AI_API_URL", "TEST_KEY")
        file_values = ("127.0.0.2", "9001", "file-model", "https://ai.test/v1", "file-key")
        env = os.environ.copy()
        for name in names:
            env.pop(name, None)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        script = (
            "import json, os; import input_to_cmd.api as config; "
            "print(json.dumps([config.HOST, config.PORT, config.MODEL, "
            "config.AI_API_URL, os.environ['TEST_KEY']]))"
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
                "127.0.0.2", 9001, "file-model", "https://ai.test/v1", "file-key",
            ])
            env["MODEL"] = "environment-model"
            env["TEST_KEY"] = "environment-key"
            result = subprocess.run(
                [sys.executable, "-c", script], cwd=directory, env=env,
                check=True, capture_output=True, text=True,
            )
            self.assertEqual(json.loads(result.stdout)[2], "environment-model")
            self.assertEqual(json.loads(result.stdout)[4], "environment-key")


if __name__ == "__main__":
    unittest.main()

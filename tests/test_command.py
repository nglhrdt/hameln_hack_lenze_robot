import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from input_to_cmd.api import EXTRACTION_INSTRUCTIONS, app
from input_to_cmd.command_dispatcher import UnsupportedCommandError, execute_command
from input_to_cmd import opc_client


class CommandTests(unittest.TestCase):
    def setUp(self) -> None:
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
            SimpleNamespace(message=SimpleNamespace(content="home"))
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
                    SimpleNamespace(message=SimpleNamespace(content=result))
                ]
                response = self.client.post(
                    "/command", json={"inputType": "text", "data": result}
                )
                self.assertEqual(response.status_code, 200)
        mock_move.assert_awaited_once_with("move_down")
        mock_stop.assert_awaited_once_with()
        self.assertIn("move_down", EXTRACTION_INSTRUCTIONS)

    @patch.dict("os.environ", {"TEST_KEY": "test-key"})
    @patch("input_to_cmd.command_dispatcher.set_home", new_callable=AsyncMock)
    @patch("input_to_cmd.api.OpenAI")
    def test_ambiguous_and_invalid_commands_are_not_executed(self, mock_openai, mock_home):
        client = mock_openai.return_value.__enter__.return_value
        for result, status in (("NONE", 422), ("move_right and move_left", 502)):
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
            SimpleNamespace(message=SimpleNamespace(content="home"))
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
            SimpleNamespace(message=SimpleNamespace(content="home"))
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
        stop.assert_awaited_once_with()


class OpcClientTests(unittest.IsolatedAsyncioTestCase):
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

        await opc_client.shutdown_movement()
        self.assertIsNone(opc_client.get_running_command())

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
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=command))])
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
        SimpleNamespace(message=SimpleNamespace(content="home"))
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

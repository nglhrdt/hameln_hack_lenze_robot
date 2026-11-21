import asyncio
import struct
import unittest

from . import opc_client


TEST_PROFILE_POINTS = [
    opc_client.ProfilePoint(
        opc_client.X_MIN + 20 * step,
        opc_client.Z_MIN + 20 * step,
        opc_client.DEFAULT_PATH_VEL,
        opc_client.DEFAULT_PATH_ACC,
        opc_client.DEFAULT_PATH_DEC,
        opc_client.DEFAULT_BLENDING_RADIUS,
        False,
    )
    for step in range(1, 8)
]
TEST_STEP_SIZE = 10.0
TEST_STEP_TIMEOUT_SECONDS = 30.0


def _format_profile(values: list) -> str:
    lines = []
    for index, value in enumerate(values):
        body = getattr(value, "Body", None)
        if isinstance(body, bytes) and len(body) == struct.calcsize("<6d?"):
            value = opc_client.ProfilePoint(*struct.unpack("<6d?", body))
        lines.append(f"  [{index}] {value}")
    return "\n".join(lines)


class OpcDeviceProfileTest(unittest.IsolatedAsyncioTestCase):
    async def test_write_test_profile(self):
        self.addAsyncCleanup(opc_client.shutdown_connection)
        async with opc_client.connection() as client:
            node = client.get_node(opc_client.PROFILE_NODE_ID)
            print(f"Current profile:\n{_format_profile(await node.read_value())}")
        answer = await asyncio.to_thread(
            input,
            f"Write {len(TEST_PROFILE_POINTS)} test points to {opc_client.OPC_URL} "
            "and execute the path (robot will move)? [y/N] ",
        )
        if answer.strip().lower() != "y":
            self.skipTest("Device write was not confirmed")
        await opc_client.write_points(TEST_PROFILE_POINTS)
        async with opc_client.connection() as client:
            node = client.get_node(opc_client.PROFILE_NODE_ID)
            print(f"Profile after write:\n{_format_profile(await node.read_value())}")


class OpcDeviceStepTest(unittest.IsolatedAsyncioTestCase):
    async def test_move_step_size(self):
        self.addAsyncCleanup(opc_client.shutdown_connection)
        x, z = await opc_client.get_position()
        print(f"Current position: X={x}, Z={z}")
        answer = await asyncio.to_thread(
            input,
            f"Jog right by at least {TEST_STEP_SIZE} on {opc_client.OPC_URL} "
            "(robot may overshoot and will move)? [y/N] ",
        )
        if answer.strip().lower() != "y":
            self.skipTest("Device movement was not confirmed")

        start_x, start_z = await opc_client.get_position()
        self.assertTrue(
            opc_client.X_MIN <= start_x < opc_client.X_MAX - TEST_STEP_SIZE,
            "Not enough space to test the step size before the X limit",
        )
        print(f"Starting position: X={start_x}, Z={start_z}")
        await opc_client.move("move_right", step_size=TEST_STEP_SIZE)
        task = opc_client._movement_task
        self.assertIsNotNone(task, "Movement monitor was not started")
        await asyncio.wait_for(task, timeout=TEST_STEP_TIMEOUT_SECONDS)
        end_x, end_z = await opc_client.get_position()
        distance = end_x - start_x
        print(f"Position after jog: X={end_x}, Z={end_z}; X displacement={distance}")
        self.assertGreaterEqual(distance, TEST_STEP_SIZE, "Step distance was not reached")


if __name__ == "__main__":
    unittest.main()

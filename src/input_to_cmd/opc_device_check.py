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


if __name__ == "__main__":
    unittest.main()

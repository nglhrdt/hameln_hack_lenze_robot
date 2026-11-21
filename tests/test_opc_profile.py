import struct
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from asyncua import ua
from asyncua.common.utils import Buffer
from asyncua.ua import ua_binary

from input_to_cmd import opc_client


class ProfileWriteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client_patch = patch("input_to_cmd.opc_client.create_client")
        self.create_client = self.client_patch.start()
        self.addCleanup(self.client_patch.stop)
        sleep_patch = patch("input_to_cmd.opc_client.asyncio.sleep", new_callable=AsyncMock)
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)
        client = self.create_client.return_value.__aenter__.return_value
        self.node = MagicMock()
        self.node.read_data_type = AsyncMock(
            return_value=ua.NodeId.from_string(opc_client.PROFILE_DATA_TYPE_ID)
        )
        self.existing = [
            ua.ExtensionObject(
                TypeId=ua.NodeId("profile-binary", 4),
                Body=struct.pack("<6d?", 1, 2, 3, 4, 5, 6, True),
            )
            for _ in range(2)
        ]
        self.node.read_value = AsyncMock(return_value=self.existing)
        self.node.write_attribute = AsyncMock()
        self.data_type = MagicMock()
        self.encoding_id = ua.ExpandedNodeId("profile-binary", 4)
        encoding = ua.ReferenceDescription()
        encoding.NodeId = self.encoding_id
        encoding.BrowseName = ua.QualifiedName("Default Binary")
        self.data_type.get_references = AsyncMock(return_value=[encoding])
        self.execute = MagicMock()
        self.execute.write_value = AsyncMock()
        self.velocity = MagicMock()
        self.velocity.read_value = AsyncMock(return_value=0.0)
        self.events = []
        self.node.write_attribute.side_effect = lambda *_: self.events.append("profile")
        self.execute.write_value.side_effect = lambda value: self.events.append(value)
        nodes = {
            opc_client.PROFILE_NODE_ID: self.node,
            opc_client.PROFILE_DATA_TYPE_ID: self.data_type,
            opc_client.PATH_EXECUTE_NODE_ID: self.execute,
            opc_client.X_VELOCITY_NODE_ID: self.velocity,
            opc_client.Z_VELOCITY_NODE_ID: self.velocity,
        }
        client.get_node = MagicMock(
            side_effect=lambda node_id: nodes[
                node_id if isinstance(node_id, str) else node_id.to_string()
            ]
        )
        self.points = [
            opc_client.ProfilePoint(50, 30, 350, 1000, 1000, 0, False),
            opc_client.ProfilePoint(110, 140, 250, 900, 800, 5, True),
        ]

    async def asyncTearDown(self):
        await opc_client.shutdown_connection()

    async def test_writes_entire_array_as_binary_extension_objects(self):
        await opc_client.write_points(self.points)

        attribute, data_value, index_range = self.node.write_attribute.await_args.args
        self.assertEqual(attribute, ua.AttributeIds.Value)
        self.assertEqual(index_range, "0:1")
        value = data_value.Value
        self.assertEqual(value.VariantType, ua.VariantType.ExtensionObject)
        self.assertEqual(len(value.Value), 2)
        wire = ua_binary.variant_to_binary(value)
        decoded = ua_binary.variant_from_binary(Buffer(wire))
        for encoded, point in zip(decoded.Value, self.points):
            self.assertEqual(encoded.TypeId, self.encoding_id)
            self.assertEqual(
                struct.unpack("<6d?", encoded.Body),
                (
                    point.x_pos, point.z_pos, point.path_vel, point.path_acc,
                    point.path_dec, point.blending_radius, point.halt,
                ),
            )

    def _written_points(self):
        value = self.node.write_attribute.await_args.args[1].Value
        return [
            opc_client.ProfilePoint(*struct.unpack("<6d?", item.Body))
            for item in value.Value
        ]

    async def test_shorter_array_writes_only_supplied_elements(self):
        await opc_client.write_points(self.points[:1])
        self.assertEqual(self.node.write_attribute.await_args.args[2], "0")
        self.assertEqual(self._written_points(), [self.points[0]])

    async def test_rejects_longer_array(self):
        with self.assertRaisesRegex(ValueError, "at most 2 profile points"):
            await opc_client.write_points(self.points + self.points[:1])
        self.node.write_attribute.assert_not_awaited()
        self.execute.write_value.assert_not_awaited()

    async def test_write_xz_points_uses_defaults(self):
        await opc_client.write_xz_points([(30, 20)])
        self.assertEqual(self._written_points(), [
            opc_client.ProfilePoint(
                30, 20, opc_client.DEFAULT_PATH_VEL, opc_client.DEFAULT_PATH_ACC,
                opc_client.DEFAULT_PATH_DEC, opc_client.DEFAULT_BLENDING_RADIUS, False,
            ),
        ])

    async def test_rejects_wrong_data_type(self):
        self.node.read_data_type.return_value = ua.NodeId(ua.ObjectIds.Double)
        with self.assertRaisesRegex(ValueError, "Unexpected profile data type"):
            await opc_client.write_points(self.points)
        self.node.write_attribute.assert_not_awaited()
        self.execute.write_value.assert_not_awaited()

    async def test_rejects_missing_binary_encoding(self):
        self.data_type.get_references.return_value = []
        with self.assertRaisesRegex(ValueError, "Default Binary"):
            await opc_client.write_points(self.points)
        self.node.write_attribute.assert_not_awaited()
        self.execute.write_value.assert_not_awaited()

    async def test_accepts_plain_node_id_for_encoding(self):
        self.data_type.get_references.return_value[0].NodeId = ua.NodeId(
            "profile-binary", 4
        )
        await opc_client.write_points(self.points)
        self.node.write_attribute.assert_awaited_once()

    async def test_rejects_remote_encoding(self):
        self.data_type.get_references.return_value[0].NodeId = ua.ExpandedNodeId(
            "profile-binary", 4, NamespaceUri="urn:remote"
        )
        with self.assertRaisesRegex(ValueError, "Remote profile"):
            await opc_client.write_points(self.points)
        self.node.write_attribute.assert_not_awaited()
        self.execute.write_value.assert_not_awaited()

    async def test_rejects_invalid_points_before_connecting(self):
        for points in (
            [],
            [opc_client.ProfilePoint(float("nan"), 0, 0, 0, 0, 0, False)],
            [opc_client.ProfilePoint(50, 50, 0, 0, 0, 0, 1)],
            [opc_client.ProfilePoint("50", 50, 0, 0, 0, 0, False)],
        ):
            with self.subTest(points=points), self.assertRaises(ValueError):
                await opc_client.write_points(points)
        self.create_client.assert_not_called()

    async def test_rejects_points_outside_limits_before_connecting(self):
        for x, z in (
            (opc_client.X_MIN - 1, 50),
            (opc_client.X_MAX + 1, 50),
            (50, opc_client.Z_MIN - 1),
            (50, opc_client.Z_MAX + 1),
        ):
            with self.subTest(x=x, z=z), self.assertRaisesRegex(ValueError, "outside limits"):
                await opc_client.write_xz_points([(x, z)])
        self.create_client.assert_not_called()

    async def test_accepts_points_on_limits(self):
        await opc_client.write_xz_points([
            (opc_client.X_MIN, opc_client.Z_MIN), (opc_client.X_MAX, opc_client.Z_MAX),
        ])
        self.node.write_attribute.assert_awaited_once()

    async def test_executes_path_after_write_and_resets_when_stopped(self):
        self.velocity.read_value.side_effect = [5.0, 5.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        await opc_client.write_points(self.points)
        self.assertEqual(self.events, ["profile", True, False])
        self.assertEqual(self.velocity.read_value.await_count, 8)

    async def test_resets_path_execute_when_monitoring_fails(self):
        self.velocity.read_value.side_effect = RuntimeError("read failed")
        with self.assertLogs("input_to_cmd.opc_client", level="ERROR"):
            with self.assertRaisesRegex(RuntimeError, "read failed"):
                await opc_client.write_points(self.points)
        self.assertEqual(self.events, ["profile", True, False])

    async def test_does_not_execute_path_when_write_fails(self):
        self.node.write_attribute.side_effect = RuntimeError("write failed")
        with self.assertRaisesRegex(RuntimeError, "write failed"):
            await opc_client.write_points(self.points)
        self.execute.write_value.assert_not_awaited()

    async def test_propagates_write_failure(self):
        self.node.write_attribute.side_effect = RuntimeError("write failed")
        with self.assertRaisesRegex(RuntimeError, "write failed"):
            await opc_client.write_points(self.points)

import asyncio
import logging
import math
import struct
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from operator import ge, le
from typing import Callable

from asyncua import Client, ua


OPC_URL = "opc.tcp://192.168.5.99:4840"
HOME_NODE_ID = "ns=4;s=|var|c500.Application.GVL_Exchange.g_xHomeExecute"
X_VELOCITY_NODE_ID = "ns=4;s=|var|c500.Application.GVL_Exchange.g_lrActXVel"
Z_VELOCITY_NODE_ID = "ns=4;s=|var|c500.Application.GVL_Exchange.g_lrActZVel"
X_POSITION_NODE_ID = "ns=4;s=|var|c500.Application.GVL_Exchange.g_lrActXPos"
Z_POSITION_NODE_ID = "ns=4;s=|var|c500.Application.GVL_Exchange.g_lrActZPos"
X_JOG_NEG_NODE_ID = "ns=4;s=|var|c500.Application.GVL_Exchange.g_xManJogXNeg"
X_JOG_POS_NODE_ID = "ns=4;s=|var|c500.Application.GVL_Exchange.g_xManJogXPos"
Z_JOG_NEG_NODE_ID = "ns=4;s=|var|c500.Application.GVL_Exchange.g_xManJogZNeg"
Z_JOG_POS_NODE_ID = "ns=4;s=|var|c500.Application.GVL_Exchange.g_xManJogZPos"
#PROFILE_NODE_ID = "ns=4;s=|var|c500.Application.GVL_Exchange.g_ascPointBuffer"
PROFILE_NODE_ID = "ns=4;s=|var|c500.Application.GVL_Exchange.g_ascProfilePar"
PROFILE_DATA_TYPE_ID = "ns=4;s=|type|c500.Application.scProfilePar"
PATH_EXECUTE_NODE_ID = "ns=4;s=|var|c500.Application.GVL_Exchange.g_xPathExecute"
X_MIN = 16.0
X_MAX = 319.0
Z_MIN = -8.0
Z_MAX = 155.0
MOVE_POLL_INTERVAL_SECONDS = 0.01
MOVEMENT_COMMANDS: dict[str, tuple[str, str, float, Callable[[float, float], bool]]] = {
    "move_left": (X_JOG_NEG_NODE_ID, X_POSITION_NODE_ID, X_MIN, le),
    "move_right": (X_JOG_POS_NODE_ID, X_POSITION_NODE_ID, X_MAX, ge),
    "move_down": (Z_JOG_NEG_NODE_ID, Z_POSITION_NODE_ID, Z_MIN, le),
    "move_up": (Z_JOG_POS_NODE_ID, Z_POSITION_NODE_ID, Z_MAX, ge),
}
VELOCITY_TOLERANCE = 0.0001
VELOCITY_POLL_INTERVAL_SECONDS = 0.1
VELOCITY_STABLE_READINGS = 3
SESSION_TIMEOUT_MS = 900_000
logger = logging.getLogger(__name__)
_movement_task: asyncio.Task[None] | None = None
_movement_command: str | None = None
_movement_lock: asyncio.Lock | None = None
_movement_loop: asyncio.AbstractEventLoop | None = None
_client_context: Client | None = None
_client: Client | None = None
_connection_lock: asyncio.Lock | None = None
_connection_loop: asyncio.AbstractEventLoop | None = None


@dataclass(frozen=True)
class ProfilePoint:
    x_pos: float
    z_pos: float
    path_vel: float
    path_acc: float
    path_dec: float
    blending_radius: float
    halt: bool


DEFAULT_PATH_VEL = 350.0
DEFAULT_PATH_ACC = 1000.0
DEFAULT_PATH_DEC = 1000.0
DEFAULT_BLENDING_RADIUS = 0.0


async def write_xz_points(points: Sequence[tuple[float, float]]) -> None:
    """Write (x, z) points using the default motion parameters."""
    await write_points([
        ProfilePoint(
            x, z, DEFAULT_PATH_VEL, DEFAULT_PATH_ACC, DEFAULT_PATH_DEC,
            DEFAULT_BLENDING_RADIUS, False,
        )
        for x, z in points
    ])


async def write_points(points: Sequence[ProfilePoint]) -> None:
    """Write points to the start of the profile array, then execute the path.

    Only the supplied elements are written (OPC UA index range); the rest of
    the array is not sent.
    """
    bodies = []
    for point in points:
        values = (
            point.x_pos,
            point.z_pos,
            point.path_vel,
            point.path_acc,
            point.path_dec,
            point.blending_radius,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in values
        ):
            raise ValueError("Profile point numeric fields must be finite numbers")
        if not isinstance(point.halt, bool):
            raise ValueError("Profile point halt must be a boolean")
        bodies.append(struct.pack("<6d?", *values, point.halt))
    if not bodies:
        raise ValueError("At least one profile point is required")
    for index, point in enumerate(points):
        if not (X_MIN <= point.x_pos <= X_MAX) or not (Z_MIN <= point.z_pos <= Z_MAX):
            raise ValueError(
                f"Profile point {index} ({point.x_pos}, {point.z_pos}) is outside "
                f"limits X {X_MIN}..{X_MAX}, Z {Z_MIN}..{Z_MAX}"
            )

    async with connection() as client:
        node = client.get_node(PROFILE_NODE_ID)
        data_type_id = await node.read_data_type()
        if data_type_id != ua.NodeId.from_string(PROFILE_DATA_TYPE_ID):
            raise ValueError(f"Unexpected profile data type: {data_type_id}")
        current = await node.read_value()
        if not isinstance(current, (list, tuple)):
            raise ValueError("Profile node does not contain a one-dimensional array")
        if len(bodies) > len(current):
            raise ValueError(
                f"Expected at most {len(current)} profile points, received {len(bodies)}"
            )

        data_type = client.get_node(data_type_id)
        encodings = await data_type.get_references(
            refs=ua.ObjectIds.HasEncoding,
            direction=ua.BrowseDirection.Forward,
            nodeclassmask=ua.NodeClass.Object,
        )
        binary_encodings = [
            ref.NodeId
            for ref in encodings
            if ref.BrowseName == ua.QualifiedName("Default Binary")
        ]
        if len(binary_encodings) != 1:
            raise ValueError("Profile data type must have one Default Binary encoding")
        encoding = binary_encodings[0]
        if isinstance(encoding, ua.ExpandedNodeId) and (
            encoding.NamespaceUri or encoding.ServerIndex
        ):
            raise ValueError("Remote profile binary encodings are not supported")
        encoding_id = ua.NodeId(encoding.Identifier, encoding.NamespaceIndex, encoding.NodeIdType)
        value = [ua.ExtensionObject(TypeId=encoding_id, Body=body) for body in bodies]
        index_range = "0" if len(value) == 1 else f"0:{len(value) - 1}"
        await node.write_attribute(
            ua.AttributeIds.Value,
            ua.DataValue(ua.Variant(value, ua.VariantType.ExtensionObject)),
            index_range,
        )
    await _execute_until_stopped(PATH_EXECUTE_NODE_ID, "path")


def create_client() -> Client:
    client = Client(url=OPC_URL)
    client.session_timeout = SESSION_TIMEOUT_MS
    return client


def _get_connection_lock() -> asyncio.Lock:
    global _connection_lock, _connection_loop
    loop = asyncio.get_running_loop()
    if _connection_loop is not loop:
        if _client_context is not None:
            raise RuntimeError("OPC connection belongs to another event loop")
        _connection_lock = asyncio.Lock()
        _connection_loop = loop
    assert _connection_lock is not None
    return _connection_lock


@asynccontextmanager
async def connection() -> AsyncIterator[Client]:
    global _client_context, _client
    async with _get_connection_lock():
        if _client is None:
            if _client_context is not None:
                raise RuntimeError("Previous OPC connection has not been closed")
            _client_context = create_client()
            try:
                _client = await _client_context.__aenter__()
            except BaseException:
                try:
                    await _client_context.__aexit__(None, None, None)
                except BaseException:
                    logger.exception("Failed to clean up OPC connection")
                    raise
                else:
                    _client_context = None
                raise
        client = _client
    yield client


async def shutdown_connection() -> None:
    global _client_context, _client
    try:
        await shutdown_movement()
    finally:
        async with _get_connection_lock():
            if _client_context is not None:
                try:
                    await _client_context.__aexit__(None, None, None)
                except BaseException:
                    logger.exception("Failed to close OPC connection")
                    raise
                else:
                    _client_context = None
                    _client = None


def get_running_command() -> str | None:
    if _movement_task is None or _movement_task.done():
        return None
    return _movement_command


async def get_position() -> tuple[float, float]:
    async with connection() as client:
        x_position = await client.get_node(X_POSITION_NODE_ID).read_value()
        z_position = await client.get_node(Z_POSITION_NODE_ID).read_value()
    return x_position, z_position


async def move(command: str, step_size: float | None = None) -> None:
    """Start a jog, optionally stopping after the axis travels step_size."""
    if command not in MOVEMENT_COMMANDS:
        raise ValueError(f"Unknown movement command: {command}")
    if step_size is not None and (
        isinstance(step_size, bool)
        or not isinstance(step_size, (int, float))
        or (isinstance(step_size, float) and not math.isfinite(step_size))
        or step_size < 0
    ):
        raise ValueError("step_size must be a finite non-negative number")

    global _movement_command, _movement_task
    async with _get_movement_lock():
        await _cancel_movement()
        started = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(_run_movement(command, started, step_size))
        task.add_done_callback(_report_movement_failure)
        _movement_task = task
        _movement_command = command
        try:
            await started
        except BaseException:
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            finally:
                _movement_task = None
                _movement_command = None
            raise


def _get_movement_lock() -> asyncio.Lock:
    global _movement_lock, _movement_loop
    loop = asyncio.get_running_loop()
    if _movement_loop is not loop:
        if _movement_task is not None and not _movement_task.done():
            raise RuntimeError("Movement is active on another event loop")
        _movement_lock = asyncio.Lock()
        _movement_loop = loop
    assert _movement_lock is not None
    return _movement_lock


def _report_movement_failure(task: asyncio.Task[None]) -> None:
    if not task.cancelled():
        error = task.exception()
        if error is not None:
            logger.error("Movement task failed", exc_info=(type(error), error, error.__traceback__))


async def _cancel_movement() -> None:
    global _movement_command, _movement_task
    task = _movement_task
    _movement_task = None
    _movement_command = None
    if task is not None:
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def shutdown_movement() -> None:
    async with _get_movement_lock():
        await _cancel_movement()


async def _run_movement(
    command: str, started: asyncio.Future[None], step_size: float | None = None
) -> None:
    jog_node_id, position_node_id, limit, reached_limit = MOVEMENT_COMMANDS[command]
    logger.info("Starting movement command %s on OPC UA node %s", command, jog_node_id)
    try:
        async with connection() as client:
            jog_node = client.get_node(jog_node_id)
            position_node = client.get_node(position_node_id)
            try:
                position = await position_node.read_value()
                if reached_limit(position, limit):
                    logger.info("%s limit already reached at position %s", command, position)
                elif step_size == 0:
                    logger.info("%s step size is zero; not starting movement", command)
                else:
                    start_position = position
                    await jog_node.write_value(True)
                    started.set_result(None)
                    logger.info("Set movement node to true")
                    while True:
                        position = await position_node.read_value()
                        if reached_limit(position, limit):
                            logger.info("%s limit reached at position %s", command, position)
                            break
                        if step_size is not None and abs(position - start_position) >= step_size:
                            logger.info("%s step size reached at position %s", command, position)
                            break
                        await asyncio.sleep(MOVE_POLL_INTERVAL_SECONDS)
            finally:
                await jog_node.write_value(False)
                logger.info("Set movement node to false")
    except Exception as exc:
        if not started.done():
            started.set_exception(exc)
        raise
    else:
        if not started.done():
            started.set_result(None)


async def stop() -> None:
    logger.info("Stopping manual movement on OPC UA")
    async with _get_movement_lock():
        failures = []
        try:
            await _cancel_movement()
        except Exception as exc:
            failures.append(exc)
        async with connection() as client:
            for node_id in (
                X_JOG_NEG_NODE_ID,
                X_JOG_POS_NODE_ID,
                Z_JOG_NEG_NODE_ID,
                Z_JOG_POS_NODE_ID,
            ):
                try:
                    await client.get_node(node_id).write_value(False)
                except Exception as exc:
                    logger.exception("Failed to clear manual movement node %s", node_id)
                    failures.append(exc)
        if failures:
            raise failures[0]


async def set_home() -> None:
    await _execute_until_stopped(HOME_NODE_ID, "home")


async def _execute_until_stopped(node_id: str, name: str) -> None:
    logger.info("Starting %s command on OPC UA node %s", name, node_id)
    try:
        async with connection() as client:
            node = client.get_node(node_id)
            x_velocity_node = client.get_node(X_VELOCITY_NODE_ID)
            z_velocity_node = client.get_node(Z_VELOCITY_NODE_ID)
            await node.write_value(True)
            logger.info("Set %s execute node to true", name)
            stable_readings = 0
            while True:
                x_velocity = await x_velocity_node.read_value()
                z_velocity = await z_velocity_node.read_value()
                if (
                    abs(x_velocity) <= VELOCITY_TOLERANCE
                    and abs(z_velocity) <= VELOCITY_TOLERANCE
                ):
                    stable_readings += 1
                    if stable_readings >= VELOCITY_STABLE_READINGS:
                        break
                else:
                    stable_readings = 0
                await asyncio.sleep(VELOCITY_POLL_INTERVAL_SECONDS)
    except Exception:
        logger.exception("Failed to execute the %s command", name)
        raise
    finally:
        try:
            async with connection() as client:
                node = client.get_node(node_id)
                await node.write_value(False)
                logger.info("Set %s execute node to false", name)
        except Exception:
            logger.exception("Failed to reset the %s execute node to false", name)
            raise

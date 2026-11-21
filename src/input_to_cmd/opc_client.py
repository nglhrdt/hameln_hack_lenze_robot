import asyncio
import logging
from operator import ge, le
from typing import Callable

from asyncua import Client


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
MOVE_DURATION_SECONDS = 5
MOVE_POLL_INTERVAL_SECONDS = 0.1
MOVEMENT_COMMANDS: dict[str, tuple[str, str, float, Callable[[float, float], bool]]] = {
    "move_left": (X_JOG_NEG_NODE_ID, X_POSITION_NODE_ID, 16.0, le),
    "move_right": (X_JOG_POS_NODE_ID, X_POSITION_NODE_ID, 319.0, ge),
    "move_down": (Z_JOG_NEG_NODE_ID, Z_POSITION_NODE_ID, -11.0, le),
    "move_up": (Z_JOG_POS_NODE_ID, Z_POSITION_NODE_ID, 155.0, ge),
}
VELOCITY_TOLERANCE = 0.0001
VELOCITY_POLL_INTERVAL_SECONDS = 0.1
VELOCITY_STABLE_READINGS = 3
SESSION_TIMEOUT_MS = 900_000
logger = logging.getLogger(__name__)


def create_client() -> Client:
    client = Client(url=OPC_URL)
    client.session_timeout = SESSION_TIMEOUT_MS
    return client


async def move(command: str) -> None:
    if command not in MOVEMENT_COMMANDS:
        raise ValueError(f"Unknown movement command: {command}")

    jog_node_id, position_node_id, limit, reached_limit = MOVEMENT_COMMANDS[command]
    logger.info("Starting movement command %s on OPC UA node %s", command, jog_node_id)
    async with create_client() as client:
        jog_node = client.get_node(jog_node_id)
        position_node = client.get_node(position_node_id)
        try:
            position = await position_node.read_value()
            if reached_limit(position, limit):
                logger.info("%s limit already reached at position %s", command, position)
            else:
                await jog_node.write_value(True)
                logger.info("Set movement node to true")
                loop = asyncio.get_running_loop()
                deadline = loop.time() + MOVE_DURATION_SECONDS
                while True:
                    position = await position_node.read_value()
                    if reached_limit(position, limit):
                        logger.info("%s limit reached at position %s", command, position)
                        break
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        break
                    await asyncio.sleep(min(MOVE_POLL_INTERVAL_SECONDS, remaining))
        except Exception:
            logger.exception("Failed to execute movement command %s", command)
            raise
        finally:
            try:
                await jog_node.write_value(False)
                logger.info("Set movement node to false")
            except Exception:
                logger.exception("Failed to reset movement node to false")
                raise


async def stop() -> None:
    logger.info("Stopping manual movement on OPC UA")
    async with create_client() as client:
        failures = []
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
    logger.info("Starting home command on OPC UA node %s", HOME_NODE_ID)
    try:
        async with create_client() as client:
            node = client.get_node(HOME_NODE_ID)
            x_velocity_node = client.get_node(X_VELOCITY_NODE_ID)
            z_velocity_node = client.get_node(Z_VELOCITY_NODE_ID)
            await node.write_value(True)
            logger.info("Set home execute node to true")
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
        logger.exception("Failed to execute the home command")
        raise
    finally:
        try:
            async with create_client() as client:
                node = client.get_node(HOME_NODE_ID)
                await node.write_value(False)
                logger.info("Set home execute node to false")
        except Exception:
            logger.exception("Failed to reset the home execute node to false")
            raise

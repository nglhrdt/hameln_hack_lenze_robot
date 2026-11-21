import asyncio
import logging

from asyncua import Client


OPC_URL = "opc.tcp://192.168.5.99:4840"
HOME_NODE_ID = "ns=4;s=|var|c500.Application.GVL_Exchange.g_xHomeExecute"
X_VELOCITY_NODE_ID = "ns=4;s=|var|c500.Application.GVL_Exchange.g_lrActXVel"
Z_VELOCITY_NODE_ID = "ns=4;s=|var|c500.Application.GVL_Exchange.g_lrActZVel"
VELOCITY_TOLERANCE = 0.0001
VELOCITY_POLL_INTERVAL_SECONDS = 0.1
VELOCITY_STABLE_READINGS = 3
SESSION_TIMEOUT_MS = 900_000
logger = logging.getLogger(__name__)


def create_client() -> Client:
    client = Client(url=OPC_URL)
    client.session_timeout = SESSION_TIMEOUT_MS
    return client


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

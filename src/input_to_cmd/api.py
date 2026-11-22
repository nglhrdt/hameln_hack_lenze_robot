import asyncio
import logging
import math
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
from pydantic import BaseModel, Field, StrictStr, model_validator
from starlette.concurrency import run_in_threadpool

from .command_dispatcher import UnsupportedCommandError, execute_command, get_running_command
from .command_history import CommandHistory
from .config_store import Configuration, ConfigurationStore, ConfigurationUpdate
from .opc_client import X_MAX, X_MIN, Z_MAX, Z_MIN, get_position, shutdown_connection

load_dotenv(".env")

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        yield
    finally:
        await shutdown_connection()


app = FastAPI(lifespan=lifespan)


def _json_safe_validation_value(value: object) -> object:
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, list):
        return [_json_safe_validation_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_safe_validation_value(item) for key, item in value.items()}
    return value


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    errors = _json_safe_validation_value(jsonable_encoder(exc.errors()))
    return JSONResponse(status_code=422, content={"detail": errors})


HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))
MODEL = os.getenv("MODEL", "smart-city-fast")
AI_API_URL = os.getenv("AI_API_URL", "https://ki.mwl.merona.de/v1")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
CORS_ALLOWED_ORIGINS = [
    origin.strip() for origin in os.getenv("CORS_ALLOWED_ORIGINS", "*").split(",") if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOWED_ORIGINS,
    allow_methods=["GET", "PATCH", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)

web_dir = Path(__file__).resolve().parents[2] / "web"
app.mount("/web", StaticFiles(directory=web_dir, html=True), name="web")
configuration_store = ConfigurationStore(
    Path(os.getenv("CONFIG_DB_PATH", str(web_dir.parent / "data" / "config.json")))
)
command_history = CommandHistory(
    Path(os.getenv("COMMAND_HISTORY_DB_PATH", str(web_dir.parent / "data" / "command_history.sqlite3")))
)

logger = logging.getLogger("input_to_cmd")
logger.setLevel(LOG_LEVEL)

STATUS_POLL_INTERVAL_SECONDS = 1.0
POSITION_TIMEOUT_SECONDS = 2.0
COMMANDS = {"home", "move_right", "move_up", "move_left", "move_down", "stop"}
EXTRACTION_INSTRUCTIONS = (
    "Extract exactly one command from the user's input. "
    "The only valid commands are: home, move_right, move_up, move_left, move_down, stop. "
    "Return only the exact command in lowercase, with no other text. "
    "If the input does not clearly specify exactly one command, return NONE."
)


Percentage = Annotated[float, Field(strict=True, ge=0, le=100, allow_inf_nan=False)]


class PointInput(BaseModel):
    x: Percentage
    z: Percentage


class CommandInput(BaseModel):
    inputType: Literal["text", "instantCommand", "point"]
    data: StrictStr | None = None
    points: list[PointInput] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_input(self) -> "CommandInput":
        if self.inputType == "point":
            if self.points is None:
                raise ValueError("Points are required for point input")
        elif self.data is None:
            raise ValueError("Data is required for text and instant command input")
        return self


@app.get("/configuration")
def read_configuration() -> Configuration:
    try:
        return configuration_store.read()
    except (OSError, ValueError):
        logger.exception("Could not read configuration")
        raise HTTPException(status_code=500, detail="Could not read configuration.") from None


@app.patch("/configuration")
def update_configuration(changes: ConfigurationUpdate) -> Configuration:
    try:
        return configuration_store.update(changes)
    except (OSError, ValueError):
        logger.exception("Could not save configuration")
        raise HTTPException(status_code=500, detail="Could not save configuration.") from None


@app.get("/command-history")
def read_command_history() -> list[dict]:
    try:
        return command_history.read()
    except (OSError, sqlite3.Error, ValueError):
        logger.exception("Could not read command history")
        raise HTTPException(status_code=500, detail="Could not read command history.") from None


def extract_command(content: CommandInput) -> str:
    assert content.data is not None
    with OpenAI(
        base_url=AI_API_URL,
        api_key=os.environ["TEST_KEY"],
    ) as client:
        chat = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": EXTRACTION_INSTRUCTIONS},
                {"role": "user", "content": content.data},
            ],
            max_tokens=1024,
        )

    if not chat.choices or chat.choices[0].message.content is None:
        logger.error("Model returned no text response")
        raise RuntimeError("The model returned no text response.")
    extracted = chat.choices[0].message.content.strip()
    if extracted == "NONE":
        logger.debug("Model found no unambiguous command")
        raise HTTPException(status_code=422, detail="No unambiguous command found.")
    if extracted not in COMMANDS:
        logger.warning("Model returned an invalid command")
        raise HTTPException(status_code=502, detail="The model returned an invalid command.")
    return extracted


@app.websocket("/ws/status")
async def status_socket(websocket: WebSocket) -> None:
    origin = websocket.headers.get("origin")
    if origin and "*" not in CORS_ALLOWED_ORIGINS and origin not in CORS_ALLOWED_ORIGINS:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    try:
        while True:
            try:
                x, z = await asyncio.wait_for(get_position(), timeout=POSITION_TIMEOUT_SECONDS)
                if not math.isfinite(x) or not math.isfinite(z):
                    raise ValueError("OPC position is not finite")
                position = {"x": x, "z": z}
                error = None
            except Exception:
                logger.exception("Could not read OPC position for status socket")
                position = None
                error = "Position unavailable"

            await websocket.send_json({
                "command": get_running_command(),
                "position": position,
                "error": error,
            })
            try:
                message = await asyncio.wait_for(
                    websocket.receive(), timeout=STATUS_POLL_INTERVAL_SECONDS
                )
                if message["type"] == "websocket.disconnect":
                    break
            except asyncio.TimeoutError:
                pass
    except (WebSocketDisconnect, OSError):
        logger.debug("Status socket disconnected")


@app.post("/command")
async def command(content: CommandInput) -> dict[str, str]:
    logger.debug("Received %s input", content.inputType)

    points = None
    if content.inputType == "point":
        assert content.points is not None
        points = [
            (
                X_MIN + point.x * (X_MAX - X_MIN) / 100,
                Z_MIN + point.z * (Z_MAX - Z_MIN) / 100,
            )
            for point in content.points
        ]
        extracted = "point"
    elif content.inputType == "instantCommand":
        assert content.data is not None
        extracted = content.data.strip().lower()
        if extracted not in COMMANDS:
            logger.warning("Invalid instant command: %s", extracted)
            raise HTTPException(status_code=422, detail=f"Invalid command. Valid commands are: {', '.join(COMMANDS)}")
    else:
        logger.debug("Requesting command extraction with model %s", MODEL)
        extracted = await run_in_threadpool(extract_command, content)
    
    logger.debug("Extracted command: %s", extracted)
    print(extracted, flush=True)
    source = content.model_dump(mode="json", exclude_none=True)
    started_at = datetime.now(timezone.utc)
    task = asyncio.create_task(
        execute_command(extracted) if points is None else execute_command(extracted, points)
    )
    event_id = None
    history_failed = False
    outcome = "interrupted"
    try:
        try:
            event_id = await run_in_threadpool(
                command_history.start, extracted, source, points, started_at
            )
        except (OSError, sqlite3.Error):
            logger.exception("Could not save command history")
            history_failed = True
        try:
            await task
        except UnsupportedCommandError as exc:
            outcome = "failed"
            logger.warning("Unsupported command: %s", extracted)
            raise HTTPException(status_code=501, detail=str(exc)) from exc
        except Exception:
            outcome = "failed"
            raise
        else:
            outcome = "succeeded"
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if event_id is not None:
            try:
                await run_in_threadpool(command_history.finish, event_id, outcome)
            except (OSError, sqlite3.Error):
                logger.exception("Could not update command history")
                history_failed = True

    logger.debug("Command execution finished: %s", extracted)

    result = {"result": "command exectued"}
    if history_failed:
        result["history_warning"] = "Could not save command history."
    return result

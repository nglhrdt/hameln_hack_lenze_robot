import asyncio
import logging
import math
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

import anyio
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError, model_validator
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
HISTORY_SNAPSHOT_SIZE = 10
_history_subscribers: dict[asyncio.Queue[dict[str, Any]], asyncio.Event] = {}
COMMANDS = {"home", "move_right", "move_up", "move_left", "move_down", "stop"}
EXTRACTION_INSTRUCTIONS = (
    "Extract exactly one command and its optional movement distance from the user's input. "
    "The only valid commands are: home, move_right, move_up, move_left, move_down, stop. "
    'Return only a JSON object with exactly two fields: "command" and "stepSize". '
    '"command" must be an exact lowercase command or null. '
    '"stepSize" must be a non-negative integer explicitly specified as the movement distance, '
    "or null when no distance is specified. Do not treat unrelated numbers as distances, "
    "invent a distance, or convert units. Understand both German and English. "
    "If the command or an explicitly specified distance is ambiguous, negative, fractional, "
    'or otherwise invalid, return {"command": null, "stepSize": null}. '
    'Examples: "fahre 10 nach links" -> {"command": "move_left", "stepSize": 10}; '
    '"move right" -> {"command": "move_right", "stepSize": null}.'
)


Percentage = Annotated[float, Field(strict=True, ge=0, le=1, allow_inf_nan=False)]


class PointInput(BaseModel):
    x: Percentage
    z: Percentage


class CommandInput(BaseModel):
    inputType: Literal["text", "instantCommand", "point"]
    data: StrictStr | None = None
    points: list[PointInput] | None = Field(default=None, min_length=1)
    stepSize: int | None = Field(default=None, strict=True, ge=0)

    @model_validator(mode="after")
    def validate_input(self) -> "CommandInput":
        if self.inputType == "point":
            if self.points is None:
                raise ValueError("Points are required for point input")
        elif self.data is None:
            raise ValueError("Data is required for text and instant command input")
        return self


class ExtractedCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: Literal["home", "move_right", "move_up", "move_left", "move_down", "stop"] | None
    stepSize: int | None = Field(strict=True, ge=0)


class SequenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_ids: list[Annotated[int, Field(strict=True, ge=1)]] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def reject_duplicate_events(self) -> "SequenceInput":
        if len(set(self.event_ids)) != len(self.event_ids):
            raise ValueError("History entries must be unique")
        return self


class SavedSequencePoints(BaseModel):
    points: list[PointInput] = Field(min_length=2)


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


@app.get("/sequences")
def read_sequences() -> list[dict[str, Any]]:
    try:
        return command_history.read_sequences()
    except (OSError, sqlite3.Error, ValueError):
        logger.exception("Could not read sequences")
        raise HTTPException(status_code=500, detail="Could not read sequences.") from None


@app.post("/sequences", status_code=201)
def save_sequence(selection: SequenceInput) -> dict[str, Any]:
    try:
        events = {event["id"]: event for event in command_history.read()}
        points: list[dict[str, float]] = []
        for event_id in selection.event_ids:
            if event_id not in events:
                raise HTTPException(status_code=409, detail="A selected history entry is no longer available.")
            source = events[event_id]["source"]
            if not isinstance(source, dict) or source.get("inputType") != "point":
                raise HTTPException(status_code=422, detail="Only point-command entries can be selected.")
            command_input = CommandInput.model_validate(source)
            assert command_input.points is not None
            points.extend(point.model_dump() for point in command_input.points)
        if len(points) < 2:
            raise HTTPException(status_code=422, detail="Select at least two points.")
        return command_history.save_sequence(selection.event_ids, points)
    except (OSError, sqlite3.Error, ValueError):
        logger.exception("Could not save sequence")
        raise HTTPException(status_code=500, detail="Could not save sequence.") from None


def extract_command(content: CommandInput) -> tuple[str, int | None]:
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
    try:
        extracted = ExtractedCommand.model_validate_json(chat.choices[0].message.content)
    except ValidationError:
        logger.warning("Model returned an invalid command or step size")
        raise HTTPException(status_code=502, detail="The model returned an invalid command or step size.") from None
    if extracted.command is None:
        logger.debug("Model found no unambiguous command or step size")
        raise HTTPException(status_code=422, detail="No unambiguous command or step size found.")
    return extracted.command, extracted.stepSize


def _publish_history(event: dict[str, Any]) -> None:
    for queue, overflow in _history_subscribers.items():
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            overflow.set()


@app.websocket("/ws/status")
async def status_socket(websocket: WebSocket) -> None:
    origin = websocket.headers.get("origin")
    if origin and "*" not in CORS_ALLOWED_ORIGINS and origin not in CORS_ALLOWED_ORIGINS:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    send_lock = asyncio.Lock()
    queue: asyncio.Queue[dict[str, Any]] | None = None
    overflow = asyncio.Event()

    async def send(payload: dict[str, Any]) -> None:
        async with send_lock:
            await websocket.send_json(payload)

    async def send_status() -> None:
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

            await send({
                "command": get_running_command(),
                "position": position,
                "error": error,
            })
            await asyncio.sleep(STATUS_POLL_INTERVAL_SECONDS)

    async def send_history(events: asyncio.Queue[dict[str, Any]]) -> None:
        try:
            snapshot = await run_in_threadpool(command_history.read, HISTORY_SNAPSHOT_SIZE)
        except (OSError, sqlite3.Error, ValueError):
            logger.exception("Could not read command history")
            await send({"type": "history_error", "error": "History unavailable"})
        else:
            await send({"type": "history_snapshot", "events": snapshot})
        while True:
            if overflow.is_set():
                async with send_lock:
                    await websocket.close(code=1013)
                return
            event = await events.get()
            await send({"type": "history_event", "event": event})

    async def receive_disconnect() -> None:
        while True:
            if (await websocket.receive())["type"] == "websocket.disconnect":
                return

    if websocket.query_params.get("history") == "1":
        queue = asyncio.Queue(maxsize=256)
        _history_subscribers[queue] = overflow
    tasks = [asyncio.create_task(send_status()), asyncio.create_task(receive_disconnect())]
    if queue is not None:
        tasks.append(asyncio.create_task(send_history(queue)))
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except (WebSocketDisconnect, OSError):
        logger.debug("Status socket disconnected")
    finally:
        if queue is not None:
            del _history_subscribers[queue]
        for task in tasks:
            task.cancel()
        with anyio.CancelScope(shield=True):
            await asyncio.gather(*tasks, return_exceptions=True)


@app.post("/command")
async def command(content: CommandInput) -> dict[str, str]:
    return await _run_command(content)


@app.post("/sequences/{sequence_id}/execute")
async def execute_sequence(sequence_id: int) -> dict[str, str]:
    try:
        sequence = await run_in_threadpool(command_history.read_sequence, sequence_id)
        if sequence is None:
            raise HTTPException(status_code=404, detail="Sequence not found.")
        saved = SavedSequencePoints.model_validate(sequence)
    except (OSError, sqlite3.Error, ValueError):
        logger.exception("Could not read sequence for execution")
        raise HTTPException(status_code=500, detail="Could not read sequence for execution.") from None
    return await _run_command(
        CommandInput(inputType="point", points=saved.points), sequence_id=sequence_id
    )


async def _run_command(content: CommandInput, sequence_id: int | None = None) -> dict[str, str]:
    logger.debug("Received %s input", content.inputType)

    step_size = content.stepSize
    points = None
    if content.inputType == "point":
        assert content.points is not None
        points = [
            (
                X_MIN + point.x * (X_MAX - X_MIN),
                Z_MAX - point.z * (Z_MAX - Z_MIN),
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
        extracted, extracted_step_size = await run_in_threadpool(extract_command, content)
        if extracted_step_size is not None:
            step_size = extracted_step_size

    logger.debug("Extracted command: %s", extracted)
    print(extracted, flush=True)
    source = content.model_dump(mode="json", exclude_none=True)
    if sequence_id is not None:
        source["sequence_id"] = sequence_id
    started_at = datetime.now(timezone.utc)
    task = asyncio.create_task(
        execute_command(extracted, points=points, step_size=step_size)
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
        if event_id is not None:
            event = {
                "id": event_id, "timestamp": started_at.isoformat(), "command": extracted,
                "source": source, "points": points, "outcome": "pending",
            }
            _publish_history(event)
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
            else:
                _publish_history({**event, "outcome": outcome})

    logger.debug("Command execution finished: %s", extracted)

    result = {"result": "command exectued"}
    if history_failed:
        result["history_warning"] = "Could not save command history."
    return result

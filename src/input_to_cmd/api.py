import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
from pydantic import BaseModel, StrictStr
from starlette.concurrency import run_in_threadpool

from .command_dispatcher import UnsupportedCommandError, execute_command
from .opc_client import shutdown_movement

load_dotenv(".env")

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        yield
    finally:
        await shutdown_movement()


app = FastAPI(lifespan=lifespan)

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
    allow_methods=["POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)

web_dir = Path(__file__).resolve().parents[2] / "web"
app.mount("/web", StaticFiles(directory=web_dir, html=True), name="web")

logger = logging.getLogger("input_to_cmd")
logger.setLevel(LOG_LEVEL)

COMMANDS = {"home", "move_right", "move_up", "move_left", "move_down", "stop"}
EXTRACTION_INSTRUCTIONS = (
    "Extract exactly one command from the user's input. "
    "The only valid commands are: home, move_right, move_up, move_left, move_down, stop. "
    "Return only the exact command in lowercase, with no other text. "
    "If the input does not clearly specify exactly one command, return NONE."
)


class CommandInput(BaseModel):
    inputType: Literal["text", "instantCommand"]
    data: StrictStr


def extract_command(content: CommandInput) -> str:
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


@app.post("/command")
async def command(content: CommandInput) -> dict[str, str]:
    logger.debug("Received %s input (%d characters)", content.inputType, len(content.data))
    
    if content.inputType == "instantCommand":
        extracted = content.data.strip().lower()
        if extracted not in COMMANDS:
            logger.warning("Invalid instant command: %s", extracted)
            raise HTTPException(status_code=422, detail=f"Invalid command. Valid commands are: {', '.join(COMMANDS)}")
    else:
        logger.debug("Requesting command extraction with model %s", MODEL)
        extracted = await run_in_threadpool(extract_command, content)
    
    logger.debug("Extracted command: %s", extracted)
    print(extracted, flush=True)
    try:
        await execute_command(extracted)
    except UnsupportedCommandError as exc:
        logger.warning("Unsupported command: %s", extracted)
        raise HTTPException(status_code=501, detail=str(exc)) from exc

    logger.debug("Command execution finished: %s", extracted)

    return {"result": "command exectued"}

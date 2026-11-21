import http.client
import json
import logging
import os
from typing import Literal
from urllib.parse import urlsplit

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from openai import OpenAI
from pydantic import BaseModel, StrictStr

load_dotenv(".env")

app = FastAPI()

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))
MODEL = os.getenv("MODEL", "smart-city-fast")
AI_API_URL = os.getenv("AI_API_URL", "https://ki.mwl.merona.de/v1")
COMMAND_API_URL = os.getenv("COMMAND_API_URL", "http://192.168.5.202:8000/command")
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

logger = logging.getLogger("input_to_cmd")
logger.setLevel(LOG_LEVEL)

COMMANDS = {"home", "move right", "move up", "move left", "stop"}
EXTRACTION_INSTRUCTIONS = (
    "Extract exactly one command from the user's input. "
    "The only valid commands are: home, move right, move up, move left, stop. "
    "Return only the exact command in lowercase, with no other text. "
    "If the input does not clearly specify exactly one command, return NONE."
)


class CommandInput(BaseModel):
    inputType: Literal["text"]
    data: StrictStr

@app.post("/command")
def command(content: CommandInput) -> dict[str, str]:
    logger.debug("Received %s input (%d characters)", content.inputType, len(content.data))
    logger.debug("Requesting command extraction with model %s", MODEL)
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

    logger.debug("Extracted command: %s", extracted)
    print(extracted, flush=True)
    command_url = urlsplit(COMMAND_API_URL)
    if command_url.scheme != "http" or not command_url.hostname or command_url.username is not None:
        raise ValueError("COMMAND_API_URL must be an HTTP URL without credentials.")
    path = command_url.path or "/"
    if command_url.query:
        path += f"?{command_url.query}"
    logger.debug("Sending command to %s:%s", command_url.hostname, command_url.port or 80)
    connection = http.client.HTTPConnection(command_url.hostname, command_url.port, timeout=10)
    try:
        connection.request(
            "POST",
            path,
            body=json.dumps({"command": extracted}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        logger.debug("Command server responded with HTTP %s", response.status)
        if not 200 <= response.status < 300:
            logger.warning("Command server returned HTTP %s", response.status)
            raise HTTPException(
                status_code=502,
                detail=f"Command server returned HTTP {response.status}.",
            )
    except (OSError, http.client.HTTPException) as exc:
        logger.error("Command server request failed (%s): %s", type(exc).__name__, exc)
        raise HTTPException(
            status_code=502,
            detail="Could not confirm whether the command server accepted the request.",
        ) from exc
    finally:
        connection.close()

    return {"result": "command exectued"}


def main() -> None:
    logging.basicConfig(format="%(levelname)s: %(name)s: %(message)s")
    os.environ["TEST_KEY"]
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()

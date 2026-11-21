import http.client
import json
import os
from typing import Literal

import uvicorn
from fastapi import FastAPI, HTTPException
from openai import OpenAI
from pydantic import BaseModel, StrictStr

app = FastAPI()

COMMANDS = {"home", "move right", "move up", "move left"}
EXTRACTION_INSTRUCTIONS = (
    "Extract exactly one command from the user's input. "
    "The only valid commands are: home, move right, move up, move left. "
    "Return only the exact command in lowercase, with no other text. "
    "If the input does not clearly specify exactly one command, return NONE."
)


class CommandInput(BaseModel):
    inputType: Literal["text"]
    data: StrictStr


@app.post("/command")
def command(content: CommandInput) -> dict[str, str]:
    with OpenAI(
        base_url="https://ki.mwl.merona.de/v1",
        api_key=os.environ["TEST_KEY"],
    ) as client:
        chat = client.chat.completions.create(
            model="smart-city-fast",
            messages=[
                {"role": "system", "content": EXTRACTION_INSTRUCTIONS},
                {"role": "user", "content": content.data},
            ],
            max_tokens=1024,
        )

    if not chat.choices or chat.choices[0].message.content is None:
        raise RuntimeError("The model returned no text response.")
    extracted = chat.choices[0].message.content.strip()
    if extracted == "NONE":
        raise HTTPException(status_code=422, detail="No unambiguous command found.")
    if extracted not in COMMANDS:
        raise HTTPException(status_code=502, detail="The model returned an invalid command.")

    print(extracted, flush=True)
    connection = http.client.HTTPConnection("192.168.5.202", 8000, timeout=10)
    try:
        connection.request(
            "POST",
            "/command",
            body=json.dumps({"command": extracted}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        if not 200 <= response.status < 300:
            raise HTTPException(
                status_code=502,
                detail=f"Command server returned HTTP {response.status}.",
            )
    except (OSError, http.client.HTTPException) as exc:
        raise HTTPException(
            status_code=502,
            detail="Could not confirm whether the command server accepted the request.",
        ) from exc
    finally:
        connection.close()

    return {"result": "command exectued"}


def main() -> None:
    os.environ["TEST_KEY"]
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()

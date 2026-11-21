# input_to_cmd

A Python project for `input_to_cmd`.

## Development

Install the project in editable mode:

```bash
make install
```

Copy `.env.example` to `.env` if needed and configure the settings. Add
`TEST_KEY` to `.env` or set it in the environment. The `.env` file is ignored
by Git; keep your key private. The application reads `.env` from the current
working directory, and existing environment variables take precedence.

Start the API:

```bash
make run
```

Alternatively, start it with a prompt for the key without displaying it:

```bash
make run-with-key
```

By default, the API listens on `0.0.0.0:8000` (all network interfaces). Send a JSON object
containing a plain-text command:

```bash
curl -X POST http://127.0.0.1:8000/command \
  -H 'Content-Type: application/json' \
  --data '{"inputType":"text","data":"Please go home."}'
```

Currently only `inputType: "text"` is supported. Image, audio, and video
inputs are not yet supported and are rejected.

## Web Interface

The web interface files (HTML, CSS, JavaScript) are served from the `/web` endpoint. Access them at:

- **Main Interface**: `http://localhost:8000/web/`
- **Assets**: 
  - `http://localhost:8000/web/css.css`
  - `http://localhost:8000/web/webSpeechAPI.js`

From another device, replace `localhost` with the API machine's IP address.
Microphone access requires HTTPS or localhost in most browsers. The page's
command API URL is configurable in its settings.

Browser CORS preflight requests are supported. All origins are allowed by
default (`CORS_ALLOWED_ORIGINS=*`). To restrict them, set
`CORS_ALLOWED_ORIGINS` in `.env` to a comma-separated list of exact frontend
origins (including scheme and port), then restart the API. Allowing all origins
lets any website send commands from a browser that can reach the API. CORS is
not authentication; protect access to the API separately.

The `HOST`, `PORT`, `MODEL`, and `AI_API_URL` settings control the listening
address, model, and AI endpoint.

Set `LOG_LEVEL=DEBUG` in `.env` to log command-processing steps. Logs do not
include the API key or raw input text.

The model extracts one of `home`, `move_right`, `move_up`, `move_left`,
`move_down`, or `stop`. Commands are printed and dispatched directly to the
OPC UA client at `opc.tcp://192.168.5.99:4840`. The home command sets the home
node, waits for three consecutive low-velocity readings, and resets the node.
Each movement command sets its manual jog node for up to five seconds, polling
the corresponding position and stopping at its configured limit (X: 16.0 to
319.0; Z: -11.0 to 155.0). The `stop` command clears all four manual jog nodes.
HTTP 200 returns
`{"result":"command exectued"}` after command execution finishes; low velocity
alone does not confirm physical homing. Ambiguous input and invalid model
responses are not executed. If execution fails, check the device before retrying.

To test the dispatcher directly, run `python -m input_to_cmd.command_dispatcher`
and enter a command such as `home`, `move_left`, or `stop`. This sends commands
directly to the configured OPC UA device.

Model interpretation can be incorrect even when the returned command is valid.
Use appropriate independent safety controls before connecting a physical device.
Use an independent emergency stop; the `stop` command is not a substitute for
a physical emergency stop.

## Hurl test

Install [Hurl](https://hurl.dev/docs/installation.html), then start the API as
described above. In another terminal, run:

```bash
make test-hurl
```

The test sends a real request to the model and can home the connected device.
To test a different API address, set `HURL_BASE_URL`, for
example `make test-hurl HURL_BASE_URL=http://127.0.0.1:8001`.

The FastAPI application is in `src/input_to_cmd/api.py`; the
`src/input_to_cmd/__main__.py` module starts the server.

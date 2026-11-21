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

Use the arrow keys for movement, `H` for home, and `S` for stop. Releasing a
movement key does not stop the robot; use the stop control.
The header selects a Kasten or Räder interface; Räder hides the home control
and its keyboard shortcut. The arrow controls are available for both. This only
changes the interface and does not stop movement or restrict commands from
other clients.

The text-command card sends its input to `/command` for AI interpretation;
use the send button or Ctrl+Enter.

The selected device type and Geräteansicht URL are shared settings. Change
the URL using the card's dialog; only HTTP and HTTPS URLs without embedded
credentials are accepted. Settings are loaded when the page opens, so other
open pages need a reload to see changes. The default URL is
`http://192.168.5.203:5000`. The browser must be able to reach the configured
address, and the device must permit embedding. Browsers may block an HTTP
frame if the control interface is served over HTTPS.

Settings are stored in the TinyDB document database at `data/config.json`
under the project root. Set `CONFIG_DB_PATH` to use another file; relative
paths are resolved from the server's working directory. Keep this file and
its backups protected: URLs may contain sensitive query parameters. The
`GET /configuration` and `PATCH /configuration` endpoints read and update
the shared settings. TinyDB access is synchronized within one process; run
only one API worker, as also required for robot control. Configuration changes
are not authenticated, so protect access to the API.

The page connects to `/ws/status` on the same host. The server samples X and Z
positions approximately once per second per connected client and sends JSON
such as `{"command":"move_left","position":{"x":123.4,"z":56.7},"error":null}`.
The command is `null` when no command is running; an in-progress home or stop
takes precedence over movement. Failed position reads are logged and sent as
`"position":null` with an error message. The UI clears stale status on
disconnect and reconnects automatically. The displayed command reflects this
process's command tracking, not an independent hardware safety indication.
WebSocket browser origins follow `CORS_ALLOWED_ORIGINS`; as with HTTP, this
is not authentication.

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
Each movement command starts its manual jog node and returns while the client
polls the corresponding position. Movement continues until `stop`, another
movement command, shutdown, an error, or its configured limit (X: 16.0 to
319.0; Z: -11.0 to 155.0). A new movement command stops the previous movement
first. The `stop` command clears all four manual jog nodes. Run only one API
process controlling the device so all commands share the same movement monitor.
Position polling, movement, home, and stop share one lazily opened OPC UA
connection per process. Shutdown stops active movement before closing that
connection; use a single server worker to avoid opening additional sessions.
HTTP 200 returns `{"result":"command exectued"}` after movement starts (or
immediately if its limit has already been reached); monitoring failures after
the response are logged. Other commands return after execution finishes; low velocity
alone does not confirm physical homing. Ambiguous input and invalid model
responses are not executed. If execution fails, check the device before retrying.

To test the dispatcher directly, run `python -m input_to_cmd.command_dispatcher`
and enter commands such as `home`, `move_left`, or `stop`. Enter `quit` or
EOF to exit and stop any active movement. This sends commands directly to the
configured OPC UA device.

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

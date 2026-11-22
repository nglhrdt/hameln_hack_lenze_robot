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

`inputType` may be `"text"` or `"instantCommand"` (an exact command such as
`"move_left"`). Both accept an optional non-negative integer `stepSize`,
which applies to movement commands; home and stop ignore it. For text commands,
the model also extracts an explicitly stated movement distance, which takes
precedence over `stepSize`. If no distance is stated, `stepSize` is the fallback.
Ambiguous or invalid distances are rejected rather than used as continuous movement.
Image, audio, and video inputs are not supported and are rejected.

To execute a path, use `inputType: "point"` and send an ordered, non-empty
array of objects with `x` and `z` percentages:

```bash
curl -X POST http://127.0.0.1:8000/command \
  -H 'Content-Type: application/json' \
  --data '{"inputType":"point","points":[{"x":0,"z":0},{"x":0.5,"z":0.5},{"x":1,"z":1}]}'
```

Each coordinate must be a normalized number from 0 to 1, with (0, 0) at the
upper left. X maps linearly from 16.0 to 319.0, while Z maps from 155.0 at the
top to -8.0 at the bottom. Point commands write the profile and start the robot's path;
the number of points cannot exceed the device's profile buffer capacity.

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
use the send button or Ctrl+Enter. The optional Schrittweite field applies to
both text and directional commands. Leave it empty for continuous movement
unless the text specifies a distance; zero does not start a jog. Stop remains
available even if the field is invalid.

The Befehlsverlauf card loads the latest 10 API commands on connection, then
receives new commands and outcome updates over the status WebSocket. It retains
up to 100 entries while connected, newest first; reconnecting reloads the latest
10. The card scrolls when it contains more than 10 entries. Entries include
failed and in-progress attempts. Each retains its source (including the original text
or normalized points), the dispatched command, and, for point commands, the
mapped device coordinates in `GET /command-history`. A successful movement
entry means the start request completed, not that the robot moved or
subsequently reached its destination.
Commands rejected before dispatch and direct Python or command-line calls are
not recorded. An interrupted server may leave an entry marked in progress.

Point-command entries can be selected in the history, including failed or
in-progress attempts. Once the selection contains at least two points, use
**Sequenz speichern** in the card header. Each entry contributes all its points
in their original order; entries are combined in the order selected. The
sequence stores copies of the original normalized coordinates, not OPC device
coordinates, and does not execute the robot. Saved sequences survive history
eviction and server restarts. Selections removed from the visible history must
be selected again; an entry evicted before saving is reported as unavailable.

The **Sequenzen** card lists saved sequences and their points. **Ausführen**
sends the saved points through the same validated point-command path as
`POST /command` and moves the robot. The execution appears in command history
with its sequence ID. The card loads on startup and refreshes after saving;
use **Aktualisieren** to load sequences saved by other clients. If an execution
response fails, check the robot status before retrying: the robot may already
have received the command. The existing stop control remains available.

`POST /sequences` accepts `{"event_ids":[2,1]}` in selection order and returns
the saved sequence with its ID, timestamp, source event IDs and points.
`GET /sequences` lists saved sequences, newest first. Protect both endpoints as
well as the history endpoints: they are unauthenticated.
`POST /sequences/{id}/execute` executes the saved sequence and is also
unauthenticated.

History is stored in `data/command_history.sqlite3`; set
`COMMAND_HISTORY_DB_PATH` to choose another file. Newly created history files
are owner-readable and writable only; check permissions on existing files and
protect this file, its backups,
and access to `GET /command-history`: original text may contain sensitive
information and the API has no authentication. Storage failures are logged and
reported in the successful command response as `history_warning`; they do not
prevent a robot command, including stop, from being dispatched.

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
Clients opting in with `/ws/status?history=1` also receive
`{"type":"history_snapshot","events":[...]}` followed by
`{"type":"history_event","event":{...}}` for new entries and outcome changes.
The snapshot is resent on reconnect; a `history_error` message reports a failed
snapshot read. Clients without the query parameter receive only status messages.
The command is `null` when no command is running; an in-progress home, stop, or point
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
319.0; Z: -8.0 to 155.0). A new movement command stops the previous movement
first. The `stop` command clears all four manual jog nodes. Run only one API
process controlling the device so all commands share the same movement monitor.
Position polling, movement, home, and stop share one lazily opened OPC UA
connection per process. Shutdown stops active movement before closing that
connection; use a single server worker to avoid opening additional sessions.

For direct Python use, `await move("move_left", step_size=10.0)` optionally
stops the jog when the axis position differs from its starting position by at
least the given distance. The size must be a finite non-negative number; zero
does not start a jog. Without `step_size`, the existing continuous movement
behavior applies. Position is polled approximately every 0.1 seconds, so the
robot can travel beyond the requested distance before the jog is stopped.

The OPC client also provides `await write_points(points)` for writing to the
`g_ascProfilePar` array. The points are written in order to the first array
elements using an OPC UA index range (for example `0:5`); remaining elements
are filled with `ZERO_PROFILE_POINT` (zero numeric fields and `halt=False`).
The device must support index-range writes on this array; otherwise the write
fails and the path is not executed. More points than the array holds raise
`ValueError`.
X and Z of every supplied point must be within `X_MIN`..`X_MAX` and
`Z_MIN`..`Z_MAX` (the same limits as manual movement):

```python
from input_to_cmd.opc_client import ProfilePoint, write_points

points = [
    ProfilePoint(
        x_pos=50, z_pos=30, path_vel=350, path_acc=1000, path_dec=1000,
        blending_radius=0, halt=False,
    ),
]
await write_points(points)
```

`await write_xz_points([(50, 30), (110, 140)])` takes only X/Z coordinates and
uses `DEFAULT_PATH_VEL`, `DEFAULT_PATH_ACC`, `DEFAULT_PATH_DEC`,
`DEFAULT_BLENDING_RADIUS`, and `halt=False`.

The wire layout follows `scProfilePar`: six LREAL fields in the order shown
above, followed by a BOOL. The server must expose the expected data type and
its Default Binary encoding. After a successful write, the method sets
`g_xPathExecute` to true, which starts the path motion. Like home, it waits
for three consecutive low-velocity readings and then resets the node to
false, also on errors. If the write fails, the path is not executed. The
example is illustrative, not a safety recommendation.

To test against the real device, run `make test-write-points`. This manually
invoked test is not included in the regular test suite because it needs a real
device connection. It prints the current profile, asks for confirmation, writes
the test points from `TEST_PROFILE_POINTS`, executes the path (the robot
moves), and prints the profile read back from the device.

`make test-step-size` runs a separate real-device check. After confirmation,
it jogs right by a requested distance of 10.0, waits up to 30 seconds for the
movement monitor, and prints and checks the X displacement. Ensure there is
enough clear space for the movement and possible overshoot. The check stops
the jog on timeout and closes the connection; it is not an emergency stop.
Run it only when no other process is controlling the device.

HTTP 200 returns `{"result":"command exectued"}` after movement starts (or
immediately if its limit has already been reached); monitoring failures after
the response are logged. Other commands return after execution finishes; low velocity
alone does not confirm physical homing. Ambiguous input and invalid model
responses are not executed. If execution fails, check the device before retrying.

To test the dispatcher directly, run `python -m input_to_cmd.command_dispatcher`
and enter commands such as `home`, `move_left`, or `stop`. Enter `quit` or
EOF to exit and stop any active movement. This sends commands directly to the
configured OPC UA device.

Model interpretation can be incorrect even when the returned command and distance are valid.
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

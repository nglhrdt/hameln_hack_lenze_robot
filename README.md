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

The model extracts one of `home`, `move right`, `move up`, `move left`, or `stop`.
Only `home` is currently supported for execution. It is printed and dispatched
directly to the OPC UA client at `opc.tcp://192.168.5.99:4840`. The client
sets the home node, waits for three consecutive low-velocity readings, and
resets the node. HTTP 200 returns `{"result":"command exectued"}` after this
process finishes; low velocity alone does not confirm physical homing.
Movement commands and `stop` return HTTP 501 and are not executed. Ambiguous input and invalid model responses
are not executed. If execution fails, check the device before retrying.

Model interpretation can be incorrect even when the returned command is valid.
Use appropriate independent safety controls before connecting a physical device.
**The `stop` command is not implemented and will not stop the robot.** Use an
independent emergency stop.

## Hurl test

Install [Hurl](https://hurl.dev/docs/installation.html), then start the API as
described above. In another terminal, run:

```bash
make test-hurl
```

The test sends a real request to the model and can home the connected device.
To test a different API address, set `HURL_BASE_URL`, for
example `make test-hurl HURL_BASE_URL=http://127.0.0.1:8001`.

The package source is in `src/input_to_cmd`.

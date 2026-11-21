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
  --data '{"inputType":"text","data":"Bewege den Roboter 15 Zentimeter nach rechts."}'
```

Currently only `inputType: "text"` is supported. Image, audio, and video
inputs are not yet supported and are rejected.

The `HOST`, `PORT`, `MODEL`, `AI_API_URL`, and `COMMAND_API_URL` settings
control the listening address, model, and API endpoints. `COMMAND_API_URL` must
be an HTTP URL without credentials.

Set `LOG_LEVEL=DEBUG` in `.env` to log command-processing steps and the
execution server's response status. Connection failures are logged with their
underlying error. Logs do not include the API key or raw input text.

The model extracts one of `home`, `move right`, `move up`, or `move left`.
The command is printed and sent as JSON to
`http://192.168.5.202:8000/command` by default. A successful response from
that server returns HTTP 200 with `{"result":"command exectued"}`; this confirms
acceptance, not physical completion. Input the model identifies as ambiguous
and invalid model responses are not forwarded.
If the request to the command server times out or the connection fails, its
execution status may be unknown; check the device before retrying.

Model interpretation can be incorrect even when the returned command is valid.
Use appropriate independent safety controls before connecting a physical device.

## Hurl test

Install [Hurl](https://hurl.dev/docs/installation.html), then start the API as
described above. In another terminal, run:

```bash
make test-hurl
```

The test sends a real request to the model and can move the device connected to
the command server. To test a different API address, set `HURL_BASE_URL`, for
example `make test-hurl HURL_BASE_URL=http://127.0.0.1:8001`.

The package source is in `src/input_to_cmd`.

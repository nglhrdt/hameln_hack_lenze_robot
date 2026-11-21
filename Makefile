.PHONY: all install run run-with-key test-dispatcher test-hurl

HURL_BASE_URL ?= http://127.0.0.1:8000

all: run

install:
	python -m pip install -e .

run: install
	python -m input_to_cmd

run-with-key: install
	@bash -c 'read -rs -p "API key: " TEST_KEY || { echo "Failed to read API key." >&2; exit 1; }; echo; export TEST_KEY; exec python -m input_to_cmd'

test-dispatcher: install
	python -m input_to_cmd.command_dispatcher

test-hurl:
	hurl --test --variable base_url=$(HURL_BASE_URL) tests/command.hurl

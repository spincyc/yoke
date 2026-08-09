PYTHON ?= python3
VENV := .venv
BIN := $(VENV)/bin

.PHONY: venv lint type test verify verify-net media-send media-recv clean

# Two-host media verification. `make verify` (offline + loopback) is the
# single-host gate that must pass first. Then, one command on each host:
#   host A (display):  make media-recv
#   host B (source):   make media-send PEER=<host-A-ip>
# or the all-in-one net check that expects a peer already receiving:
#   make verify-net PEER=<peer-ip> ROLE=send|recv
ROLE ?= recv
PEER ?= 127.0.0.1
FRAMES ?= 600
ENCODING ?= raw
FEC ?= none

venv: $(VENV)/.stamp
$(VENV)/.stamp: pyproject.toml
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --quiet -e '.[dev]'
	touch $@

lint: venv
	$(BIN)/ruff format --check src tests
	$(BIN)/ruff check src tests

type: venv
	$(BIN)/mypy

test: venv
	$(BIN)/pytest

verify: lint type test

# One command per host for real two-host verification over the network.
media-recv: venv
	$(BIN)/yoke-media run --role receiver --encoding $(ENCODING) --fec $(FEC) --snoop

media-send: venv
	$(BIN)/yoke-media run --role sender --peer $(PEER) \
		--encoding $(ENCODING) --fec $(FEC) --frames $(FRAMES)

verify-net: venv
	@if [ "$(ROLE)" = "send" ]; then $(MAKE) media-send; else $(MAKE) media-recv; fi

clean:
	rm -rf $(VENV) .mypy_cache .ruff_cache .pytest_cache dist

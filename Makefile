PYTHON ?= python3
VENV ?= .scratch/venv
BIN := $(VENV)/bin

# Keep all disposable tools, caches, and generated artifacts in one place.
export XDG_CACHE_HOME := $(abspath .scratch/cache)
export PIP_CACHE_DIR := $(abspath .scratch/cache/pip)
export MYPY_CACHE_DIR := $(abspath .scratch/cache/mypy)
export RUFF_CACHE_DIR := $(abspath .scratch/cache/ruff)
export PYTHONPYCACHEPREFIX := $(abspath .scratch/cache/pycache)
export TMPDIR := $(abspath .scratch/tmp)
export PLAYWRIGHT_BROWSERS_PATH := $(abspath .scratch/cache/playwright)

.PHONY: venv lint type test verify docs docs-serve browser-install verify-browser simulate \
	benchmark-control benchmark-media benchmark-telemetry benchmark-usb verify-firmware verify-net \
	media-send media-recv clean

ROLE ?= recv
PEER ?= 127.0.0.1
GROUP ?= 239.255.42.42
PORT ?= 5004
INTERFACE ?= 0.0.0.0
TTL ?= 1
UNICAST ?= 0
FRAMES ?= 600
ENCODING ?= raw
FEC ?= none
BENCH_FRAMES ?= 10000
REPEAT ?= 5
NODES ?= 12
INTENTS ?= 100

MEDIA_OPTIONS = --group $(GROUP) --port $(PORT) --interface $(INTERFACE) \
	--ttl $(TTL) --encoding $(ENCODING) --fec $(FEC)
ifeq ($(UNICAST),1)
SEND_TRANSPORT = --unicast --peer $(PEER)
RECV_TRANSPORT = --unicast
endif

venv: $(VENV)/.stamp
	@mkdir -p .scratch/tmp
$(VENV)/.stamp: pyproject.toml
	mkdir -p .scratch/tmp
	$(PYTHON) -m venv $(VENV)
	$(BIN)/python -m pip install --quiet -e '.[dev]'
	touch $@

lint: venv
	$(BIN)/ruff format --check src tests scripts benchmarks
	$(BIN)/ruff check src tests scripts benchmarks

type: venv
	$(BIN)/mypy

test: venv
	$(BIN)/python -m pytest -ra

docs: venv
	$(BIN)/python scripts/build_docs.py

docs-serve: docs
	$(BIN)/python -m http.server 8000 --bind 127.0.0.1 --directory .scratch/site

# Browser installation is explicit; make verify needs no installed browser.
# CI adds --with-deps when provisioning its disposable Ubuntu runner.
BROWSER_INSTALL_OPTIONS ?=
browser-install: venv
	$(BIN)/python -m playwright install --only-shell $(BROWSER_INSTALL_OPTIONS) chromium

verify-browser: docs
	$(BIN)/python -m scripts.verify_browser --site .scratch/site

verify: lint type test docs

verify-firmware:
	$(PYTHON) firmware/esp32/verify.py

simulate: venv
	$(BIN)/python -m yoke.simulation --json

benchmark-control: venv
	$(BIN)/python benchmarks/control.py --nodes $(NODES) --intents $(INTENTS) --repeat $(REPEAT)

benchmark-media: venv
	$(BIN)/python benchmarks/media.py --frames $(BENCH_FRAMES) --repeat $(REPEAT)

benchmark-telemetry: venv
	$(BIN)/python benchmarks/telemetry.py --nodes $(NODES) --repeat $(REPEAT)

# Simulated, software-only USB data-path numbers (ADR 0022); add
# USB_BENCH_OPTIONS=--markdown for a table or --quick for a smoke run.
USB_BENCH_OPTIONS ?=
benchmark-usb: venv
	$(BIN)/python benchmarks/usb_datapath.py $(USB_BENCH_OPTIONS)

# First pass make verify, then use one command on each host. Multicast sends
# to GROUP; set INTERFACE to each host's local IPv4 address. For unicast use
# UNICAST=1 on both ends and PEER=<receiver-ip> on the sender.
media-recv: venv
	$(BIN)/yoke-media run --role receiver $(MEDIA_OPTIONS) $(RECV_TRANSPORT) --snoop

media-send: venv
	$(BIN)/yoke-media run --role sender $(MEDIA_OPTIONS) $(SEND_TRANSPORT) --frames $(FRAMES)

verify-net: venv
	@if [ "$(ROLE)" = "send" ]; then $(MAKE) media-send; else $(MAKE) media-recv; fi

clean:
	rm -rf .scratch

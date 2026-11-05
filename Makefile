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
	benchmark-control benchmark-media benchmark-telemetry benchmark-usb verify-usb-kernel verify-firmware firmware-bench firmware-usb-bench ywp-bench ywp-fuzz verify-net \
	native-usb fuzz-usb tsan-usb benchmark-usbtrace \
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
	$(PYTHON) firmware/verify.py

# Host-only cost of the IR endpoint core (no SDK, no device): JSON on stdout.
IR_CORE := firmware/esp32/components/yoke_ir
firmware-bench:
	mkdir -p .scratch/firmware-bench
	cc -std=gnu17 -O2 -I $(IR_CORE)/include tests/firmware_ir/host_ir_bench.c \
		$(IR_CORE)/ir_core.c $(IR_CORE)/ir_program.c $(IR_CORE)/ir_encode.c \
		-o .scratch/firmware-bench/host-ir-bench
	.scratch/firmware-bench/host-ir-bench

# Native YWP/1 library (native/ywp, ADR 0027). Host-only cost as JSON on
# stdout, and longer libFuzzer runs than `make verify` does (FUZZ_SECONDS
# per target); the grown decode corpus is then checked against Python.
YWP := native/ywp
FUZZ_SECONDS ?= 300
ywp-bench:
	mkdir -p .scratch/ywp-bench
	cc -std=c11 -O2 -D_DEFAULT_SOURCE -I $(YWP)/include tests/native_ywp/c/bench.c \
		$(wildcard $(YWP)/src/*.c) -o .scratch/ywp-bench/bench
	.scratch/ywp-bench/bench

ywp-fuzz: venv
	YWP_FUZZ_SECONDS=$(FUZZ_SECONDS) $(BIN)/python -m pytest -q tests/native_ywp/test_ywp_fuzz.py

# Host-only cost of the USB box's data path: both boxes' native cores and
# glue over TCP loopback, without sanitizers (tests/firmware_usb/host_bridge.c;
# YOKE_NATIVE_YUSB may point at another native/yusb).
firmware-usb-bench: venv
	$(BIN)/python tests/firmware_usb/usb_build.py .scratch/firmware-bench/host-usb-bridge
	for mode in bulk-in bulk-out report; do .scratch/firmware-bench/host-usb-bridge $$mode; done

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

# Opt-in real-kernel loopback of the gadget worker under QEMU/KVM (no root):
# dummy_hcd + usbip-host + Raw Gadget + usb-storage. Skips cleanly without
# KVM, QEMU, the running kernel's image/modules or busybox.
verify-usb-kernel: venv native-usb
	$(BIN)/python tests/usbsim/kernel_vm/launch.py

# Native USB core (ADR 0027, native/README.md): the tier-2 gadget worker, its
# simulation build (Raw Gadget calls served by tests/usbsim/rpc.py) and the
# export server with a simulated disk. Host compiler; see native/README.md for
# a native build on the ZERO 3E.
NATIVE_OUT := .scratch/native
NATIVE_CC ?= cc
NATIVE_CFLAGS ?= -std=c11 -O2 -g -Wall -Wextra -Werror -Wshadow -Wstrict-prototypes
NATIVE_INC := -Inative/yusb/include -Inative/yusb/linux -Inative/yusb/sim -Inative/third_party/lz4
NATIVE_CORE := $(wildcard native/yusb/src/*.c) native/third_party/lz4/lz4.c
NATIVE_LINUX := $(filter-out %/sys_linux.c,$(wildcard native/yusb/linux/*.c))
NATIVE_HEADERS := $(wildcard native/yusb/*/*.h) native/third_party/lz4/lz4.h

native-usb: $(NATIVE_OUT)/yoke-gadget-worker $(NATIVE_OUT)/yoke-gadget-worker-sim \
	$(NATIVE_OUT)/yusb-export-sim
$(NATIVE_OUT)/yoke-gadget-worker: $(NATIVE_CORE) $(NATIVE_LINUX) native/yusb/linux/sys_linux.c $(NATIVE_HEADERS)
	mkdir -p $(NATIVE_OUT)
	$(NATIVE_CC) $(NATIVE_CFLAGS) $(NATIVE_INC) $(filter %.c,$^) -o $@ -lpthread
$(NATIVE_OUT)/yoke-gadget-worker-sim: $(NATIVE_CORE) $(NATIVE_LINUX) native/yusb/sim/sys_rpc.c $(NATIVE_HEADERS)
	mkdir -p $(NATIVE_OUT)
	$(NATIVE_CC) $(NATIVE_CFLAGS) $(NATIVE_INC) $(filter %.c,$^) -o $@ -lpthread
$(NATIVE_OUT)/yusb-export-sim: $(NATIVE_CORE) native/yusb/sim/simdev.c native/yusb/sim/export_server.c $(NATIVE_HEADERS)
	mkdir -p $(NATIVE_OUT)
	$(NATIVE_CC) $(NATIVE_CFLAGS) $(NATIVE_INC) $(filter %.c,$^) -o $@

# libFuzzer over every network-facing parser (USB/IP codec, descriptors, the
# proxy and export streams, YX framing) and the control socket. Bounded:
# FUZZ_SECONDS per target; seeds in native/yusb/fuzz/corpus/<target>/, working
# corpora under .scratch/fuzz. Exits non-zero on the first crash.
FUZZ_SECONDS ?= 60
FUZZ_TARGETS := usbip desc proxy proxyyx export yx control
fuzz-usb:
	mkdir -p .scratch/fuzz
	set -e; for t in $(FUZZ_TARGETS); do \
		clang -g -O1 -std=c11 -fsanitize=fuzzer,address,undefined $(NATIVE_INC) \
			native/yusb/fuzz/fuzz_$$t.c $(NATIVE_CORE) native/yusb/sim/simdev.c \
			native/yusb/linux/control.c -o .scratch/fuzz/fuzz_$$t; \
		mkdir -p .scratch/fuzz/corpus_$$t native/yusb/fuzz/corpus/$$t; \
		.scratch/fuzz/fuzz_$$t .scratch/fuzz/corpus_$$t native/yusb/fuzz/corpus/$$t \
			-max_total_time=$(FUZZ_SECONDS) -rss_limit_mb=2048 -max_len=20000 \
			-artifact_prefix=.scratch/fuzz/crash_$$t- 2>.scratch/fuzz/$$t.log; \
		tail -1 .scratch/fuzz/$$t.log; \
	done

# ThreadSanitizer over the native gadget worker in the simulator: the usbsim
# scenarios and the control-socket tests against a -fsanitize=thread build
# (tests/usbsim/native.py honours YOKE_NATIVE_SANITIZER). Not part of verify.
TSAN_CC ?= clang
tsan-usb: venv
	YOKE_NATIVE_SANITIZER=thread CC=$(TSAN_CC) $(BIN)/python -m pytest -q \
		tests/native_usb/test_native_usbsim.py tests/native_usb/test_native_control.py

# Replay usbmon traces (synthetic now, real captures later) per ADR 0028 lever.
USBTRACE_OPTIONS ?= --markdown
benchmark-usbtrace: venv
	$(BIN)/python tools/usbtrace/synth.py --out-dir .scratch/usbtrace
	$(BIN)/python tools/usbtrace/replay.py .scratch/usbtrace --json-out .scratch/usbtrace/replay.json $(USBTRACE_OPTIONS)

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

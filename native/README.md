# Native cores

Portable C11 for the tiered USB product line ([ADR 0027](../doc/decisions/0027-tiered-usb-product-line-and-native-c-data-path.md)).
The Python implementation in `src/yoke/usb` is the executable reference.
Divergence from it is a bug unless it is listed under
[Differences from the reference](#differences-from-the-reference).

| Directory | What |
|---|---|
| `yusb/include/` | Public headers (the backend contract lane C builds against) |
| `yusb/src/` | The sans-I/O cores: USB/IP codec (`usbip.c`), descriptors and builder (`desc.c`), control routing (`policy.c`), computer-side presentation core (`proxy.c`, `client.c`, `pump.c`), device-side export core (`export.c`), YX extension (`yx.c`, [ADR 0028](../doc/decisions/0028-negotiated-usbip-payload-extension.md)), route fencing and target validation (`fence.c`, `target.c`) |
| `yusb/linux/` | Tier-2 `yoke-gadget-worker`: Raw Gadget backend (`rawgadget.c`), session threads (`session.c`), ADR 0022 control socket (`control.c`), CLI (`worker_main.c`); the only OS seam is `sys.h` |
| `yusb/sim/` | Host-only: the RPC seam to the Python fake kernel (`sys_rpc.c`), a simulated BOT disk (`simdev.c`) and its USB/IP server (`export_server.c`) |
| `yusb/fuzz/` | libFuzzer targets and small seed corpora |
| `third_party/lz4/` | LZ4 1.10.0 (BSD-2-Clause), vendored unmodified; see `VENDORED` |
| `ywp/` | YWP/1 (lane E) |

## Design in one paragraph

Every core object is sans-I/O and single-threaded:

- **Receive.** The platform asks for a receive buffer (`*_rx_space`): small messages are parsed from a staging buffer, and payloads land directly in their final URB buffer.
- **Transmit.** It writes the pending segments (`*_tx_iov`) with one `sendmsg`.
- **Time and backends.** It passes a millisecond clock, and implements small operation tables (`yusb_gadget_ops_t`, `yusb_hc_ops_t`) whose operations complete later through `*_done` or `*_complete`.

The reference blocks one thread per endpoint. Here, each blocking step is a named state:

- the control flow `E0_*` in `proxy.c`;
- the pump wait states `W_*` in `pump.c`: stall, park with backoff, host clear-halt, unlink.

The Linux worker supplies the threads Raw Gadget needs (one per endpoint and ep0, plus event and receive threads) around one mutex. It cancels a blocked ioctl with `pthread_kill(SIGUSR1)` and EINTR, never retried. SIGTERM and SIGINT are blocked in every thread except the main one, so an EINTR elsewhere is always that cancellation. ESP-IDF plugs TinyUSB and the USB Host Library into the same tables.

Memory is allocated only at create and on configuration changes. `yusb_proxy_footprint()` and `yusb_export_footprint()` report it in advance. `YUSB_*` bounds in the headers are `#ifndef`-overridable for MCU builds:

| Configuration | Proxy at create | Pumps, worst case | Export per connection |
|---|---|---|---|
| Linux defaults | 186 KB | 4.6 MB | 1.09 MB (32 URBs, automatic pool, 128 held) |
| Linux, YX FILL+LZ4+CACHE (64 blocks) | 801 KB | 5.0 MB | 1.64 MB |
| ESP32-S3 (MCU bounds) | 51.9 KB | 66 KB | 49.8 KB (16 URBs, 4 KiB max, 64 held) |
| ESP32-S3, YX FILL / FILL+LZ4 / +CACHE 16 | 60.5 / 77.0 / 208 KB | 82 KB | — / 69.9 / 202 KB |

All figures are exact: `test_native_footprint.py` (proxy) and `test_native_export_core.py` (export) count every heap byte by linker wrapping and check the footprint against it; YX figures include what an accepted offer allocates at import. On the ESP32-S3 a YX cache does not fit. The cores need about 2.3 KB of task stack plus the platform's `vsnprintf`.

## Build

```sh
make native-usb      # .scratch/native/{yoke-gadget-worker,yoke-gadget-worker-sim,yusb-export-sim}
cmake -S native/yusb -B build && cmake --build build    # libyusb.a + yoke-gadget-worker
```

Both build optimized: `make native-usb` with `-O2 -g` (`NATIVE_CFLAGS`), CMake as `RelWithDebInfo` unless `CMAKE_BUILD_TYPE` is set.

- **ESP-IDF.** `native/yusb` is the component `yusb`: add it to `EXTRA_COMPONENT_DIRS`.
- **Directory name.** It is not called `usb`, so it cannot shadow ESP-IDF's USB Host Library.

### On a Radxa ZERO 3E (computer-side box, Radxa OS)

No cross toolchain is needed. The worker builds natively in a few seconds:

```sh
sudo apt install gcc make
git clone <this repository> && cd yoke
make native-usb NATIVE_CC=gcc            # or: cmake -S native/yusb -B build && cmake --build build
sudo packaging/zero3e/install.sh computer --native-worker .scratch/native/yoke-gadget-worker
sudo systemctl daemon-reload && sudo systemctl restart yoke-gadget
journalctl -u yoke-gadget -f             # "native worker on UDC fcc00000.usb ..."
```

**Without the agent** (bench step 4 in [USB-DATAPATH.md](../doc/design/USB-DATAPATH.md)):

```sh
sudo .scratch/native/yoke-gadget-worker --udc fcc00000.usb \
    --target <device-box-ip>:3240/<busid> --source <computer-box-vlan-ip>
```

**Selecting the worker.** The command line is the Python worker's own. `python -m yoke.usb.worker --implementation native` (or `YOKE_GADGET_WORKER=native`) executes `/usr/local/libexec/yoke/yoke-gadget-worker` with the same options.

- `install.sh computer --native-worker PATH` installs the binary there and the drop-in `yoke-gadget.service.d/20-native-worker.conf`, which only sets `Environment=YOKE_GADGET_WORKER=native`. The unit's `ExecStart`, options and sandbox are unchanged.
- Re-running `install.sh computer` without the option removes the drop-in, so the Python worker runs again. The binary stays.
- `--log-level` takes the names Python's `logging` takes: `DEBUG`, `INFO`, `WARN`/`WARNING`, `ERROR`, `CRITICAL`/`FATAL`, `NOTSET`. Unlike Python, case is ignored.

**Native-only options:**

| Option | Purpose |
|---|---|
| `--bot-write-behind N` | Write-behind window (ADR 0028 §7, off by default) |
| `--yx fill,lz4,cache` and `--yx-cache N` | Offer the extension |
| `--max-transfer N` | BOT URB size |
| `--udc-sysfs DIR` | Override the UDC sysfs directory |

## Test

Pytest compiles everything with the host compiler, `-Wall -Wextra -Werror` and ASan/UBSan:

| Command | What it runs |
|---|---|
| `make verify` | Includes `tests/native_usb` (described below) |
| `make verify-usb-kernel` | The real-kernel QEMU/KVM loopback: both workers through the same steps, then the export core imported by the guest kernel through `vhci-hcd` |
| `make tsan-usb` | `test_native_usbsim.py` and `test_native_control.py` against a ThreadSanitizer build of the worker (`YOKE_NATIVE_SANITIZER=thread`, clang). Not part of `make verify` |
| `make fuzz-usb FUZZ_SECONDS=60` | Every network-facing parser for a bounded time: USB/IP codec, descriptors and builder, proxy and export streams, YX framing, control socket |
| `make benchmark-usbtrace` | The trace replay (`tools/usbtrace/README.md`) |

The `tests/native_usb` suites:

- `test_native_usbsim.py`: the scenarios `test_usb_proxy.py`/`test_usb_faults.py` run against the Python worker, against the native worker in the simulator. The fake Raw Gadget kernel is served over `tests/usbsim/rpc.py`.
- `test_native_diff.py`: differential against `usbip.py`, `descriptors.py` and `policy.py`.
- `test_native_export.py`: the export core with the Python reference client and proxy, and native-to-native with each YX lever.
- `test_native_core.py`: deterministic unit tests, at Linux and ESP32-S3 sizing.
- `test_native_control.py`: the control socket. Request decisions are differential against `control.py`, replies are byte for byte. It also covers the worker process: slow clients, signals, descriptor exhaustion, a second instance, unconfirmed teardown, thread-creation failure and the YX fallback.
- `test_native_yx.py`, `test_native_usbtrace.py`.

## Differences from the reference

Recorded deliberately; tests pin the behaviors both share.

1. **Control requests.** Both workers answer SETUPs in order and abandon a forwarded request on a bus reset or disconnect, releasing a SETUP still owed with an ep0 STALL (Raw Gadget keeps it pending otherwise). A new SETUP abandons the current request while it is still owed (SET_CONFIGURATION and SET_INTERFACE still complete, unanswered); a request whose status stage the UDC completed (OUT with data, `YUSB_EV_CONTROL_ACKED`) always finishes. Every ep0 answer belongs to the newest SETUP delivered. A forwarded request that times out is STALLed at once and unlinked; the reference first waits for the unlink.
2. **Teardown.** Neither worker unlinks outstanding URBs one by one at teardown: the transport closes and the exporter cancels everything on close (the Python worker does the same since 2026-11-05). Unlinks are still sent and awaited for SET_CONFIGURATION, SET_INTERFACE and bus resets.
3. **Ordering.** The CBW goes out before the IN URBs it sizes, deterministically. The reference does this only through thread scheduling.
4. **Bounds** (the reference is unbounded):
   - tombstones: as many as URB table entries (4096 in the reference);
   - forwarded-descriptor cache: 16 entries, 4 KiB, FIFO;
   - cached strings: 96 and HID report descriptors: 4 per alternate setting; any more are forwarded;
   - interface numbers: at most 31;
   - BOT IN pipelining: `bot_in_slots` (8) buffers of `max_transfer`;
   - control data stages: at most `policy.ep0_max` (65535). A longer forwarded request is STALLed locally.
5. **Robustness.** An empty LANGID table caches no strings; the reference raises IndexError.
6. **Control socket** (`linux/control.c`, `linux/worker_main.c`):
   - **Parsing.** Requests are parsed strictly, by the schema. Anything accepted is accepted by `control.py` with the same route. These are refused although `control.py` accepts them, and the agent never sends them:
     - non-ASCII text, including a UTF-8 BOM and UTF-16/32;
     - a `schema` that is not the integer `1` (`true`, `1.0`);
     - duplicate keys;
     - scoped IPv6 hosts.
   - **Errors.** A refusal names a category (`"bad host"`, `"unknown control request"`), not the reference's exact text.
   - **Concurrency.** Up to 8 connections are served at once. Each has one 2 s deadline for its request line; the reference allows 2 s per `recv`, one connection at a time. When all 8 are busy, a new connection replaces the oldest.
   - **Descriptor exhaustion.** `EMFILE` on accept is logged and retried every 100 ms.
   - **A live socket is kept.** A second worker on the same path exits with status 1 and leaves the socket alone; the reference unlinks it. A stale socket file is replaced.
   - **Unconfirmed teardown** is retried with a backoff from 100 ms to 2 s. Each retry waits only 100 ms, so route requests are answered at once (with the held generation).
7. **YX unavailable.** When the agreed YX codec cannot be allocated (`YUSB_END_YX_UNAVAILABLE`), the worker retries at the normal delay, offering no YX. It offers the configured levers again after the next route change.
8. **Export core** (no reference exists; it mirrors the stock stub):
   - A URB that completes while being unlinked yields its RET_SUBMIT, then RET_UNLINK(0). The stub drops the data in that race.
   - A URB to an endpoint outside the configuration closes the connection; the stub desynchronizes.
   - URB buffers come from a fixed pool (`pool_bytes`, automatic by default) sized per URB, so a parked 64-byte interrupt read does not cost `max_urb_bytes`; one slot and a 1 KiB buffer are reserved for ep0. A URB larger than the pool can ever hold completes with -ENOMEM: an MCU exporter with 4 KiB URBs cannot serve stock Linux usb-storage, whose 120 KiB URBs fail that way.
   - When slots or pool bytes run out, IN and zero-length OUT submits are held and parsing continues, so a CMD_UNLINK still reaches them (a held URB is answered RET_UNLINK(-ECONNRESET), no RET_SUBMIT); an OUT submit with data stops the stream until room frees. Every RET_UNLINK has its own tracked buffer; when none is free the stream waits.
9. **Status counters** add `late_ret_submits`, `unlinks` and `yx_*`.

## Licensing

- No GPL code is used, and no Linux kernel code was copied.
- Raw Gadget request numbers and layouts are ABI values from the UAPI header (`include/uapi/linux/usb/raw_gadget.h`, syscall note). `test_native_core.py` checks them against the installed header.
- LZ4 is BSD-2-Clause; its licence ships in `third_party/lz4/LICENSE`.

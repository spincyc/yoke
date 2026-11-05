# USB box firmware: tier 1 (ESP32-S3) and tier 1.5 (ESP32-P4)

Status: implemented in `firmware/esp32-usb` and verified in host simulation
and target builds only, 2026-11-05. No hardware has been touched. Items
marked ⚠ are bench-unverified hardware or controller claims. Decisions:
[ADR 0027](../decisions/0027-tiered-usb-product-line-and-native-c-data-path.md)
(product tiers) and
[ADR 0030](../decisions/0030-usb-box-firmware-on-esp-idf.md) (this firmware's
shape). Operator steps: [`firmware/esp32-usb`](../../firmware/esp32-usb/) (its README).

```text
peripheral ─ USB-A ─ device box:   USB Host Library ─ yoke_usbh ─ box_host ─ export core ─┐
                                                                                         USB/IP over TCP 3240
computer ─ native USB ─ computer box: TinyUSB ─ yoke_usbd ─ box_gadget ─ presentation core┘
                     YWP/1 (native/ywp, box_ywp) pairs them; ADMISSION gates TCP 3240
```

## Components

| Component | Role | Host-tested |
|---|---|---|
| `firmware/components/yoke_ota` | Mutual-TLS admin and update server, journal, health policy (shared with the IR endpoint) | yes (`tests/firmware_ota`) |
| `firmware/components/yoke_eth` | W5500 or P4 EMAC bring-up, link and IPv4 state, board pin profiles ⚠ | target build |
| `firmware/components/yoke_node` | Boot lifecycle: fail-closed trust, Ethernet retries, restart backoff, health gate, clock floor | target build |
| `yoke_usbdesc` | Bounds-checked configuration walk, descriptor log lines | yes |
| `yoke_usbd` | Computer-role backend: TinyUSB application class driver presenting arbitrary descriptors | `usbd_plan.c`, `usbd_core.c` yes; `usbd_tinyusb.c` target |
| `yoke_usbh` | Device-role backend over the ESP-IDF USB Host Library | yes, over a model of the library |
| `yoke_box` | Configuration (Kconfig default, NVS, admin API), status, the two self-tests, and the bridge: `box_gadget`/`box_host` (cores onto the backends), `box_pump` (socket transport), `box_media`, `box_limit` (flood throttle), `box_clock` (SNTP), `box_ywp` and `box_net` (YWP/1 shell, network task), `box_computer`/`box_device` (role sessions) | all but `box.c`, `box_clock.c` and `box_computer.c`; `box_ywp.c`, `box_net.c` and `box_device.c` over ESP-IDF stand-ins (`tests/firmware_usb/idf`) |

The IR firmware uses the same `yoke_ota`, `yoke_eth` and `yoke_node`; its
behaviour is unchanged (same Kconfig symbols and values, same partition
table, all 132 of its host tests).

## Backend interfaces

The backends are the narrow seams the native cores of ADR 0027 plug into.
Both use the Linux Raw Gadget / host-controller operation model of the
Python reference, so the platform glue is a table of one-line adapters.

**`yoke_usbd.h` (computer role, presentation core's gadget operations).**
`present(descriptors, sink)` validates the rewritten descriptors against
this controller and connects; `withdraw()` disconnects. Asynchronous
`ep0_write`, `ep0_read` (0 = status), `ep0_stall`, `ep_write`, `ep_read`,
`ep_cancel`, each ending in exactly one `ep0_done`/`ep_done`; synchronous
`ep_enable`, `ep_disable`, `ep_set_halt`, `link_speed`. Events: CONNECT,
CONTROL, SUSPEND, RESUME, RESET, DISCONNECT. Results are byte counts or
INTERRUPTED, HALT_CLEARED, SHUTDOWN, ERROR.

**`yoke_usbh.h` (device role, export core's host-controller operations).**
`claim`, `release`, `endpoint_type`, asynchronous `submit` and `cancel`
with one `complete(urb, status, actual)` each; statuses are Linux wire
errnos, as USB/IP carries them. `attached`/`detached` frame one device.

**Threading.** Each backend runs in one task (TinyUSB's task, or the USB
host client task); every sink call happens there and never from inside an
operation (completions are posted). Other tasks enter with `*_post()`. A
sans-I/O core therefore needs no lock when it runs in that task.

## Control requests: what TinyUSB 0.21 and DWC2 permit

Investigated in esp_tinyusb 3.0.0's bundled TinyUSB 0.21.0 (`usbd.c`,
`dcd_dwc2.c`); `usbd_tinyusb.c` refuses to build against another TinyUSB.

| Request | Who answers | Can the host be held off? |
|---|---|---|
| Interface-recipient (any type, incl. GET_DESCRIPTOR HID report, SET/GET_INTERFACE) | sink | **Yes.** The driver returns from SETUP without arming EP0; DWC2 NAKs the data or status IN token until `ep0_write`/`ep0_read(0)`/`ep0_stall` |
| Endpoint-recipient class, device-recipient class (routed by `wIndex` interface) | sink | Yes, as above |
| Vendor (any recipient) | sink, via `tud_vendor_control_xfer_cb` | Yes, as above |
| OUT with data | sink | **Only until the data stage.** The host is held off before `ep0_read(buf)`; once the data arrives TinyUSB queues the status ZLP as soon as the driver's DATA callback returns. Refusing then needs a synchronous STALL decision, so a remote STALL after the data is late (as with Raw Gadget; the core counts it) |
| GET_DESCRIPTOR device, configuration, string, BOS, qualifier, other-speed | backend, from the presented descriptors | No: synchronous callbacks. An uncached string or descriptor type STALLs |
| SET_ADDRESS, GET_STATUS, GET_CONFIGURATION, SET/CLEAR_FEATURE(device) | TinyUSB | No |
| SET_CONFIGURATION | TinyUSB acknowledges at once; the sink then receives it as CONTROL_ACKED (native/yusb's `YUSB_EV_CONTROL_ACKED`): the core applies it and answers nothing on ep0, and holds the computer's next request until it is applied. A repeat of the active value is acknowledged and dropped (`usbd.c`: no endpoint reset, no event; the remote device never sees it ⚠) | No: forwarded after the fact; a remote refusal is late |
| CLEAR/SET_FEATURE(ENDPOINT_HALT), GET_STATUS(endpoint) | TinyUSB; the backend turns a clear into HALT_CLEARED on a started operation (the dwc3 `-EPIPE` the core expects) | No |

Details that shape the backend:
- **Stale replies.** A late reply must not answer a newer request. The
  backend keeps the SETUP and checks it against usbd's own request copy
  before replying; a superseded request completes as INTERRUPTED. So does
  an armed reply the host abandoned for a request TinyUSB answers itself
  (no DATA or status callback follows; checked every 250 ms).
- **SETUP reception.** In buffer-DMA mode DWC2 receives a SETUP only while
  EP0 OUT is armed, and a deferred IN request leaves it unarmed. An
  unanswered request is therefore stalled after 4 s (below the 5 s host
  timeouts), which re-arms SETUP reception.
- **Endpoints.** DWC2 cannot free one TX FIFO, so the backend allocates one
  FIFO per endpoint address (largest packet across alternate settings) at
  SET_CONFIGURATION and switches alternates by re-activation. Alternate 0
  is live at once (NAK, not silence) because the configuration was already
  acknowledged. `dcd_dwc2` gives IN endpoint n TX FIFO n, so IN endpoint
  numbers stop at the FIFO count. `present` refuses what the controller
  cannot carry: S3 has endpoint numbers 1–6 (IN 1–4) and 242 FIFO words;
  P4 high speed has 1–15 (IN 1–7) and 992 words ⚠ (TinyUSB's
  `dwc2_esp32.h`); isochronous, high-bandwidth and hub interfaces;
  `bConfigurationValue` other than index + 1 (TinyUSB's mapping). The
  presentation core is offered the same IN numbers, so it reports "UDC
  lacks endpoints" for a device it cannot place.
- **Speed.** TinyUSB 0.21 runs a high-speed PHY at high speed only (and
  esp_tinyusb fixes the port's speed), so the P4 presents high-speed
  devices only; low- and full-speed devices need an S3 computer box (or a
  patched `dcd_dwc2` forcing full speed ⚠).
- **Cancel.** DWC2 aborts an armed transfer by re-activating the endpoint,
  which also resets DATA0. The core cancels only before a toggle reset
  (stall recovery, SET_INTERFACE, teardown). A completion the controller
  had already queued still wins over the cancel (a posted fence decides;
  one fence post at a time covers every abort before it).
- **Transfers.** `dcd_dwc2` programs a transfer without splitting it, and
  the packet counter has 7 bits on the S3 (8 on the P4's high-speed port),
  so the backend moves at most 127 packets per chunk.
- **Task queue.** TinyUSB's queue carries both the controller's interrupt
  events and the backend's posts; when full it drops interrupt events (a
  lost SETUP leaves EP0 deaf until reboot) and blocks posters. The build
  raises `CFG_TUD_TASK_QUEUE_SZ` from 16 to 64 ⚠.
- **Descriptors.** esp_tinyusb implements the device, configuration and
  string callbacks for its static model (one configuration, ASCII strings
  of at most 31 characters, one language); the link wraps those three so
  TinyUSB serves the remote bytes verbatim. `firmware/verify.py` checks the
  link map. The qualifier and other-speed callbacks cannot be wrapped
  (`usbd.c` calls its own weak defaults); on a high-speed port esp_tinyusb
  answers them from the buffers it was installed with, which the backend
  owns and fills from each presentation (the presented descriptors, or the
  device's own fields and no other-speed configuration).
- **Attach.** TinyUSB connects at install; the backend disconnects at once.
  esp_tinyusb logs in between, so the computer sees a pulse of a few
  milliseconds, below its 100 ms connect debounce ⚠.
- **VBUS.** The S3 has no VBUS sense: an unplugged computer leaves the bus
  idle, which reads as suspended, and the session is reported `active`
  with "suspended or unplugged" ⚠.

## USB Host Library limits (device role)

- No per-transfer cancel: `cancel` halts and flushes the endpoint, so every
  URB queued on it ends with `-ECONNRESET`; control transfers cannot be
  cancelled.
- IN transfers are whole packets; a URB is rounded up and a device sending
  more than the URB asked for ends it with `-EOVERFLOW`. URBs above 4 KiB
  move in whole-packet chunks and end at the first short chunk.
- The library enumerates and selects the configuration itself; one device,
  no hubs. It enumerates through a control buffer the build raises from
  256 B to 1 KiB (`CONFIG_USB_HOST_CONTROL_TRANSFER_MAX_SIZE`), the largest
  configuration descriptor the box accepts.
- Clearing a halt keeps the pipe's data toggle (`usb_host_endpoint_clear`),
  while CLEAR_FEATURE(HALT) reset the device's to DATA0: one packet would be
  lost (BOT recovery). After a successful clear the backend releases and
  claims the interface again for fresh pipes; with another transfer of the
  interface in flight it can only clear the halt ⚠.
- A device that leaves and returns before it was freed is attached once
  the previous one is.
- The library counts a transfer in flight until its callback returns and
  refuses to release an interface or close the device meanwhile, so the
  backend queues completions in the callback and delivers them after
  `usb_host_client_handle_events` returns; a refused release or close is
  retried each second.

## Bridge

A box with a YWP/1 node id and a PSK (at least 32 bytes, used byte for byte
like the agents' PSK file) or an explicit open mode bridges; otherwise it
runs its self-test. The device box is a YWP/1 producer with admission, the
computer box a consumer.

**Tasks.** A sans-I/O core is touched by one task only:
- the role's USB task (TinyUSB's task, or the USB host client task) runs the
  USB/IP core, its backend and the sockets of its session, which are
  nonblocking and moved by `box_pump` straight into and out of the core's
  buffers;
- the network task (`yoke-net`, 8 KiB stack) runs the YWP/1 node, its
  multicast socket, NVS persistence and mbedTLS hashing, and a `select()`
  that reports the USB/IP sockets' readiness to the USB task (posted once,
  then re-armed by the USB task: readable only while the core has room,
  writable only with bytes pending); other tasks wake it through an
  eventfd.
- Route commands, health polls and admission sets cross from the network
  task as posts; answers cross back through a queue.
- Sockets exist only from the box's first address on (lwIP is initialised
  with Ethernet, after the USB role started): the network task starts then,
  and so does the device box's listener.

**Wall clock.** YWP/1 sequences are Unix time and peers drop frames more
than an hour ahead as stale, while the box's clock is the provisioned floor
plus saved uptime: off-time is lost, and a rebase on a lagging clock could
go below what peers recorded. A bridging box therefore runs SNTP
(`esp_netif_sntp`, initialised before Ethernet so the first DHCP request
asks for option 42; `CONFIG_YOKE_USB_NTP_SERVER`, default `pool.ntp.org`,
as the second server) or takes a forward-only reading over the admin API
(`PUT /v1/usb/clock`; at most a year ahead unless `"force": true`, since a
typo would advance the clock floor and then the sequence floor for good).
Until one of them, the node is not created (`ywp.state`
`waiting-for-clock`) and a rebase is refused. A synchronized reading is
kept in RTC memory each second and restored after software, panic and
watchdog resets (the restored floor can be an hour old after a watchdog
reset), when it is plausible against that floor; a power-on clears it.

**Flooding.** Every forged datagram costs the node a parse and two HMACs.
The shell handles at most 16 datagrams per wake, feeding the watchdog, and
yields a tick after a full batch so lower-priority tasks and idle run.
Only under load (the previous wake filled its batch) is a source whose
datagrams keep failing authentication ignored unparsed (token bucket: 8
failures, then 1 per second; 8 sources tracked; `ywp.throttled` counts):
sources are spoofable, so a forged trickle from a peer's address must not
mute that peer, and outside a flood a failure costs only a bounded parse.
Rotating source addresses defeat the per-source bucket but not the batch
bound ⚠.

**Threat limits.** As ADR 0024 accepts for admission (a LAN host can spoof
an admitted IPv4 address), the box trusts its LAN in two more places: SNTP
is unauthenticated, so a rogue NTP reply can step the wall clock (back,
so peers' frames look far-future and pairing stalls; or forward, which
advances the clock floor and the YWP/1 sequence floor); and the flood
throttle keys on the spoofable source address, so during a flood a forger
can make a peer's frames be dropped unparsed until the flood ends. The
mutual-TLS admin API remains the authenticated way to set the clock.

**Control socket.** A failed (re)open of the multicast socket, at start or
after an address change, leaves the node running and is retried every
second.

**Routes.** ATTACH/DETACH go through `ywp_route` (one command at a time, a
detach barrier plus the latest attach). The computer box parses the
producer's media (IPv4 literal host only, first busid), follows the route
with native/yusb's generation fence (a newer generation is adopted only
after the running session stopped and a pending connect was abandoned;
readiness posts carry the socket's generation, so a closed socket's late
post is ignored), retries with its backoff, and reports
`idle`/`pending`/`active` (configured or suspended)/`error` with detail.
TinyUSB is installed at boot, disconnected; a session's READY phase
presents the core's descriptor cache and this controller's endpoints (1–6,
IN 1–4 on the S3; 1–15, IN 1–7 on the P4) so the core refuses what it
cannot carry.

**Admission.** The device box accepts TCP 3240 only from the IPv4 addresses
of the latest ADMISSION set (ADR 0024); it reports `pending` until the node
has published one, `error` without a device on the USB-A socket, otherwise
`active`. Established sessions are not torn down by a new set.

**Policy.** S3: budget 4 IN / 4 OUT / numbers 1–6, max transfer and ep0
4 KiB, two-deep IN queues, two BOT IN slots (native/yusb's measured S3
sizing). Full speed needs no deeper queue for tier 1's classes; generic
bulk IN then moves two packets per network round trip. P4: 7 / 15 / 15,
16 KiB, four-deep bulk IN (two-deep interrupt), four slots and
`bot_out_window` 8 (write-behind only inside a BOT data-out phase, the CSW
still reports the outcome).
`out_window` stays 1 on both: a device's OUT STALL reaches the computer on
its next transaction, and tier 1's OUT traffic (HID output reports,
serial, MIDI) needs no pipelining; the cost is one packet per round trip
for generic bulk OUT. `control_timeout_ms` 3500 (below the backend's 4 s
stall), `prefetch_os_string` on (TinyUSB serves device-recipient
descriptors from the cache only).

**URB slots.** A device box runs 16 URBs at the USB Host Library per
import (`YOKE_USBH_URBS`; each owns a DMA buffer of 2 KiB on the S3, 4 KiB
on the P4; longer URBs move in chunks, control data up to that size).
native/yusb's export core keeps one of them for control and cuts every
URB's buffer from a pool to its own length (automatic: room for 8
full-size URBs, 4 KiB on the S3, 16 KiB on the P4 for a tier 1.5
consumer's BOT transfers, plus a 1 KiB control buffer). IN reads beyond
that are held while the stream is still read, so a consumer that parks
reads on a quiet device (Linux cdc-acm parks 16) still gets its control
requests and unlinks through; only an OUT submit carrying data stops the
stream until room frees. A URB larger than the pool can hold fails with
`-ENOMEM`: stock Linux usb-storage (120 KiB URBs) cannot use a device box,
while the boxes' own consumers split transfers (`max_transfer`).

**YX (ADR 0028).** S3: plain USB/IP both ways. Full speed (about 1.2 MB/s)
is far below the 100 Mbit link and tier 1's transfers are mostly below
YX's 512-byte minimum, so FILL (+9 KB) or LZ4 (+21 to 25 KB) would buy
nothing; the block cache (about 200 KB) does not fit. P4: FILL and LZ4
offered by the consumer and accepted by the producer: high-speed mass
storage outruns the 100 Mbit EMAC, so zero-block elimination and
compression raise throughput (+100 KB consumer, +22 KB producer); no block
cache (another 131 KB for 16 blocks) and no read-ahead until the bench
measures it ⚠. A session whose agreed codec cannot be allocated ends
`YX_UNAVAILABLE`, and the consumer reconnects plain.

**RAM.** Each session is budgeted before it is created, conservatively:
its native/yusb footprint must fit in internal RAM as if all of it came
from there while leaving a 32 KiB reserve for TLS, lwIP and the Ethernet
driver, and in the largest free block of the malloc heap as if it were one
allocation. A short heap refuses the session with a reason instead of
half-creating it. Endpoint buffers are allocated at SET_CONFIGURATION; if
they do not fit, the core refuses the configuration ("UDC could not
configure") and the host model checks that this stays clean.

The S3 image enables the N16R8 module's octal PSRAM: malloc places
allocations of 16 KiB and more there (the TLS input buffers, the YWP/1
node, the presentation core's state block, the export core's pool), while DMA buffers
and smaller pieces stay internal. Host measures in bytes against
native/yusb's exact footprints (the 32-bit target
is slightly smaller); the runtime base is an estimate ⚠:

| | S3 computer | S3 device | P4 computer | P4 device |
|---|---|---|---|---|
| Internal heap after static use | 238,257 | 238,257 | 546,096 | 546,096 |
| ESP-IDF runtime (task stacks, two TLS sessions, lwIP, Ethernet) ⚠ | ~130,000 | ~130,000 | ~130,000 | ~130,000 |
| YWP/1 node (16 peers, 64 origins) | 33,840 | 31,440 | 33,840 | 31,440 |
| Presentation core at create (P4: YX FILL+LZ4) | 51,928 | — | 273,583 | — |
| Its endpoints: a HID keyboard / the worst case | ~2,000 / 65,904 | — | small / 2,552,400 | — |
| Export core (16 URBs, automatic pool; P4: YX FILL+LZ4) + USB Host transfers | — | 45,816 + 32,896 | — | 178,704 + 65,664 |
| **Left, internal RAM only** | ~21,000 | ~0 | ~107,000 | ~140,000 |
| **Left on the S3 with PSRAM** (TLS input buffers ~33,000, the node, the proxy's 34,384-byte block and the export's ~34 KB pool moved out) | ~122,000 | ~97,000 | — | — |

Without PSRAM the S3's margins are below the reserve, so the guard refuses
sessions there (cleanly, with the reason in status) unless the bench
measures a smaller runtime base ⚠; with it, both S3 roles keep a wide
margin. The worst-case endpoint set does not fit either S3 role and is
refused as above. `GET /v1/status` reports `usb.heap_internal_free` and
`usb.heap_internal_min_free` for the bench to confirm ⚠. The device box
serves one import at a time.

The YWP/1 node is sized for the S3: 16 peers, the reference's 64 replay
origins (admission needs origins ≥ peers), 8 intents and rejections, 16
effects in 4 KiB; a consumer keeps 2 × 1200 bytes of producer media. The
network task keeps 8 KiB of stack (native/ywp's worst path is now about
2.8 KiB on x86-64; `ywp.cost.task_stack_free` measures it on silicon ⚠).

**Host model** (`tests/firmware_usb/host_bridge.c`, `make
firmware-usb-bench`): the export core, `box_host`, TCP loopback, the
presentation core, `box_gadget` and `usbd_core` in one thread at the S3
policy, with models of the USB Host Library, DWC2 and the computer that
complete at once. On an x86-64 workstation without sanitizers: bulk IN
25.2 MB/s (two-deep queues), bulk OUT 11.4 MB/s and 5.1 µs per interrupt
report through both boxes. These are software costs only: full speed
carries about 1.2 MB/s, the LAN round trip bounds the queues, and an ESP32
runs this code tens of times slower (inference) ⚠.

## Self-tests

- **Computer role:** a CDC-ACM echo device (pid.codes test ID 1209:0001)
  presented through `yoke_usbd` exactly as a mirrored device is: synthesized
  SET_CONFIGURATION, endpoint enable, bulk echo, and class requests answered
  20 ms later to prove the NAK hold-off on silicon.
- **Device role:** enumerate whatever is on the USB-A socket, log its
  descriptors and strings, claim it and log every changed report of its
  first interrupt IN endpoint.
- **Both:** Ethernet link and DHCP, logged with the address.

## Verification

`tests/firmware_usb` (pytest, host C compiler, `-Wall -Wextra -Werror`,
ASan and UBSan) runs the firmware's C unchanged:

- the pure modules: the descriptor rules against TinyUSB's limits (IN
  endpoint numbers, port speed, the qualifier and other-speed buffers), the
  backend state machine against a simulated usbd/DWC2 port (deferral,
  superseded, expired and abandoned requests, OUT data, reset ordering,
  chunking within the packet counter, ZLPs, cancel fences, halts, contract
  misuse, P4 limits), both self-tests, the configuration (PSK, open-mode
  and name rules), the media description, the socket transport (close,
  refusal, backpressure, no read interest while full) and the
  authentication-failure throttle;
- `usbh_esp.c` against a model of the USB Host Library (completions after
  its bookkeeping, CLEAR_HALT's fresh pipes, unplug and quick replug);
- `box_ywp.c`, `box_net.c` and `box_device.c` over ESP-IDF stand-ins
  (`tests/firmware_usb/idf`): no socket before the network, the clock
  gate, the control socket's retry, the receive bound under a flood, and
  watch wake-ups;
- the bridge model above (import, fetch, presentation, SET_CONFIGURATION
  with a class request right behind it, a relayed vendor STALL, bulk both
  ways, reports in order, a configuration refused for want of RAM) and a
  cdc-acm-like USB/IP client whose 16 parked reads must not hold up a
  control request and two unlinks.

`firmware/verify.py` adds the target builds. None of it proves DWC2 timing,
PHY behaviour, VBUS, cables, lwIP multicast, SNTP or task scheduling; those
are bench items. `box.c`, `box_clock.c` and `box_computer.c` run on target
only.

| Image | App size | Slot use | Static internal RAM |
|---|---|---|---|
| ESP32-S3 | 786,064 B | 12.5 % of 6 MiB | 103,503 B of 341,760 B DIRAM (8 KB of it PSRAM support) |
| ESP32-P4 | 876,784 B | 13.9 % of 6 MiB | 109,264 B of 655,360 B DIRAM |

The YWP/1 node's cost on silicon is reported in `GET /v1/status`
(`ywp.cost`: node bytes, the network task's free stack, datagram and poll
time) for the bench to record ⚠.

## First bench procedure (two ESP32-S3 boxes)

1. Wire per the README; provision and flash a computer-role image on board
   A and a device-role image on board B. Record the board revision, USB
   bridge and W5500 module, the boot log's PSRAM line (8 MB octal) and
   `usb.heap_internal_free` from `GET /v1/status`.
2. **Ethernet:** cable both to a DHCP switch; record the logged addresses
   and `GET /v1/status`. Repeat once without a cable (health confirms
   after 10 s, "link observed down").
3. **Computer role:** connect A's native port to a Linux computer. Record
   `lsusb -v -d 1209:0001`, run the README's echo script, then `stty -F
   <port> 9600`; check the log shows the line coding and DTR lines and no
   `control request ended` errors. Repeat on Windows (GET_LINE_CODING path).
4. **Device role:** on B, plug in a USB keyboard, then a mouse, then a
   flash drive; record each enumeration log, the reports, unplug/replug
   behaviour and the VBUS voltage at the socket under load.
5. **Role switch:** `PUT /v1/usb/config {"role": "device"}` on A; check it
   restarts in the device role, then switch it back.
6. **Bridge:** configure both boxes for
   YWP/1 (README), `yoke status` lists both; `yoke pair` the keyboard box to
   the computer box. Record the time to `active`, typing and Caps Lock (the
   SET_REPORT path), unplug/replug at each end, Ethernet pull, a box reboot,
   `ywp.cost` and heap from `GET /v1/status`, and a stock Linux `usbip
   list -r` from an unadmitted address (refused) and from the paired box.
   Repeat once on a network without NTP: `ywp.state` stays
   `waiting-for-clock` until `PUT /v1/usb/clock`. Unplug A's computer and
   record what the status reports (no VBUS sense).
7. Record every result in `doc/research/HARDWARE.md` before clearing a ⚠.

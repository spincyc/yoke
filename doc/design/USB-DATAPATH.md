# USB data path: computer-side gadget proxy

Status: implemented in software and verified in simulation only, 2026-11-04.
No hardware has been touched. Items marked ⚠ are bench-unverified hardware
or kernel claims. Decisions: [ADR 0021](../decisions/0021-userspace-usbip-client-and-raw-gadget-presentation.md)
(architecture) and [ADR 0022](../decisions/0022-gadget-worker-process-and-metadata-driver.md)
(process split, driver, simulation).

```text
USB device ─ device-side box: stock usbipd/usbip-host (TCP 3240)
               ⇅ USB/IP 1.1.1 over Ethernet
computer-side box: yoke.usb.worker ── Raw Gadget ── DWC3 OTG port ─ computer
                   ↑ metadata-only control socket
                   yoke agent: usb-gadget driver
```

## Components

| Module | Role | I/O |
|---|---|---|
| `yoke.usb.usbip` | USB/IP codec (OP_REQ/REP_IMPORT, DEVLIST, CMD/RET_SUBMIT, CMD/RET_UNLINK) | none |
| `yoke.usb.descriptors` | Parse, validate, reject, rewrite descriptors; build the cached presentation | none |
| `yoke.usb.policy` | Control routing, IN coalescing plan, BOT CBW sizing, tunables | none |
| `yoke.usb.client` | Import handshake; URB multiplexer (one reader thread) | socket |
| `yoke.usb.gadget` | Backend-neutral presentation interface | none |
| `yoke.usb.rawgadget` | Raw Gadget ioctls ⚠ | `/dev/raw-gadget`, sysfs |
| `yoke.usb.proxy` | Event thread, endpoint pumps, reset/disconnect/suspend handling | threads |
| `yoke.usb.session` | Import, fetch descriptors, start presenting | — |
| `yoke.usb.worker` | Route following, reconnect backoff, control server, CLI | process |
| `yoke.usb.control` | Route/status schema shared with the agent | Unix socket |
| `yoke.drivers.gadget` | `usb-gadget` agent driver; never imports payload modules | Unix socket, sysfs |

## Request handling

| Request | Handling |
|---|---|
| GET_DESCRIPTOR device, configuration, string, qualifier, other-speed, BOS | Served from the import-time cache, truncated to wLength; a short reply on a 64-byte boundary carries a ZLP |
| GET_DESCRIPTOR HID report (interface) | Cached at import from the HID descriptor's length |
| Other GET_DESCRIPTOR | Forwarded once; data or STALL cached |
| SET_ADDRESS | Local (normally the UDC answers it); never forwarded |
| SET_CONFIGURATION | Quiesce pumps, forward (stub calls `usb_set_configuration`), enable endpoints, VBUS_DRAW, CONFIGURE, acknowledge |
| SET_INTERFACE | Quiesce that interface, forward, enable the new alternate setting; a remote refusal restores the old pumps and STALLs |
| GET_CONFIGURATION / GET_INTERFACE / GET_STATUS | Local; status reports bus power and no remote wakeup |
| CLEAR/SET_FEATURE(ENDPOINT_HALT) | Normally answered by the UDC ⚠; if delegated, forwarded |
| SET_FEATURE(REMOTE_WAKEUP), SYNCH_FRAME, SET_DESCRIPTOR | STALL (not advertised / no isochronous) |
| Class and vendor requests | Forwarded. IN data or STALL is returned. For OUT requests *with data* the UDC completes the status stage when the data arrives, so a remote STALL reaches the computer as success and is only counted (`late_out_stalls`; see the class table under Limits) |

## Data transfers

- **IN (device to computer).** Each pump keeps `in_depth_bulk` (default 4)
  or `in_depth_interrupt` (2) URBs outstanding. Each URB is one packet
  (`wMaxPacketSize × (1 + mult)`). Consecutive full completions coalesce
  into one gadget write of at most 64 KiB. A short final completion sets
  the zero flag only when the device ended with a zero-length packet, so
  the computer sees exactly the device's packets.
- **OUT (computer to device).** Each read is one packet and each packet is
  one URB, so a zero-length packet becomes a zero-length URB. At most
  `out_window` (default 1) OUT URBs are in flight. The UDC acknowledges a
  packet as soon as a read is queued, so the computer sees a device's OUT
  STALL on its *next* OUT transaction. A window above one pipelines OUT
  transfers but delays that STALL by up to the window, and drops data the
  UDC already acknowledged (`dropped_out`).
- **Bulk-Only Transport.** For BOT interfaces (class 8, protocol 0x50), the
  OUT pump parses each 31-byte CBW:
  - data-in commands queue exact IN URB sizes for the data and the CSW;
  - data-out commands size the next OUT reads to the data length.

  This keeps BOT fast while every other protocol stays packet-exact.
- **Stall (IN).**
  - The pump writes any partial data, unlinks later URBs and halts the
    gadget endpoint.
  - It then clears the remote halt *once* (the stub calls
    `usb_clear_halt`) and probes with a single URB.
  - A device that stalls the probe as well (for example BOT after an
    invalid CBW, until reset recovery) is parked. The pump retries only
    after host activity (a forwarded control request or OUT traffic) or an
    exponential backoff of up to 2 s. This replaces a 53k CMD_SUBMIT/s spin
    with about 10 per second (`tests/test_usb_faults.py`).
- **Stall (OUT).** The remote halt is cleared at once, and again before each
  later transfer until one succeeds. A device that keeps its halt until a
  class reset is therefore released right after the host performed that
  reset.
- **Host CLEAR_FEATURE(ENDPOINT_HALT).** The UDC answers it itself (⚠ dwc3,
  dummy_hcd). dwc3 gives requests already *started* on that endpoint back
  with `-EPIPE`; the pump treats that as the host's clear. It unlinks the
  endpoint's URBs, forwards the clear to the device and retries the same
  write or read (`host_clear_halts`). A clear on an endpoint without a
  started request is invisible to the proxy.
- **Transaction errors** (`-EPROTO`, `-EILSEQ`, `-ETIME`, `-EOVERFLOW`,
  `-EREMOTEIO`) are presented as STALL.
- **Cancel.** URBs are unlinked when pumps stop: SET_CONFIGURATION,
  SET_INTERFACE, bus reset, disconnect, stall recovery and teardown.
  - stub_tx sends every queued RET_SUBMIT before every queued RET_UNLINK
    on each pass, so RET_UNLINK(0) can precede the URB's own RET_SUBMIT.
  - An unlink that loses the race carries the URB status (0 or -EPIPE)
    with no RET_SUBMIT at all.
  - The client keeps tombstones for unlinked sequence numbers and gives an
    ambiguous unlink 200 ms for a late RET_SUBMIT, whose data it delivers.
    Afterwards it consumes stragglers instead of dropping the connection.

## Descriptor adjustments

| Field | Presented value | Reason |
|---|---|---|
| Device and qualifier `bMaxPacketSize0` | 64 | Raw Gadget control data is packetized by the UDC's endpoint zero ⚠ |
| Configuration `bmAttributes` | D7 set, D6 (self-powered) and D5 (remote wakeup) clear | The box is bus powered by the computer (owner, 2026-11-04); Raw Gadget has no wakeup ioctl |
| Configuration `bMaxPower` | `power_ma / 2` (default 250 = 500 mA) ⚠ | Declares the box's own draw; the peripheral is powered by the device-side box |
| BOS USB 2.0 Extension LPM bit | clear | LPM is a property of the local UDC |
| Low-speed devices | presented at full speed, endpoints unchanged | Every valid low-speed control/interrupt descriptor is valid at full speed with the same polling period |

Rejected with a clear status:
- super-speed devices (USB 2 first);
- hubs;
- isochronous or extra control endpoints;
- low-speed bulk;
- endpoint counts or numbers beyond the UDC budget (configured default 7
  IN and 7 OUT ⚠, refined by EPS_INFO after binding);
- a computer link slower than the device (for example a high-speed device
  behind a full-speed hub).

## Runtime requirements (⚠ all bench-unverified)

- **Kernel:**
  - `CONFIG_USB_RAW_GADGET` (module `raw_gadget`, creates `/dev/raw-gadget`);
  - `CONFIG_USB_DWC3` with gadget support, the OTG controller at `fcc00000`
    in peripheral or OTG device role (`dr_mode`, or the usb-role-switch
    sysfs `role` = `device`);
  - UDC name expected `fcc00000.usb` (check `ls /sys/class/udc`).
  - Raw Gadget's `usb_raw_init.driver_name` is the controller's *gadget*
    name, not the UDC name. dwc3 names every gadget `dwc3-gadget`,
    dummy_hcd `dummy_udc`; a wrong name makes RUN fail with EBUSY. The worker
    infers it from `/sys/class/udc/<udc>/device/driver` (dwc3 → `dwc3-gadget`,
    verified for dummy_udc in the VM test); `--gadget-driver` overrides it.
- **No other gadget on that UDC.** Radxa images may bind an ADB/NCM
  configfs gadget. Clear its `UDC` attribute first, otherwise RUN fails
  with "is another gadget bound?".
- **Device node:** `/dev/raw-gadget`, owned by the `yoke-gadget` user through
  a udev rule (see `examples/yoke-gadget.service`). Open must be blocking.
- **Signals:** the worker installs a SIGUSR1 handler. Cancelling a blocked
  EVENT_FETCH/EP_READ/EP_WRITE relies on EINTR from
  `wait_for_completion_interruptible`.
  - The ioctl must go through ctypes (`LibcIoctl`): CPython's `fcntl.ioctl`
    retries EINTR (PEP 475) because Python handlers only run on the main
    thread, so teardown could never be confirmed.
  - The VM test proves both sides: the production worker stops in about
    0.1 s, and an `fcntl` variant cannot stop.
- **Device side:** stock `usbip-host` and `usbipd` with the device bound
  (the existing `usbip` producer driver).
- **Data-plane admission (lane/ctl, ⚠).** The producer accepts TCP 3240 only
  from the IPv4 address its consumer's authenticated announcements come
  from, so the worker:
  - resolves the producer's `host` as IPv4 only;
  - binds the computer-side box's control-VLAN IPv4 before connecting.

  The driver passes that address in each route. It takes `driver.source`,
  or else the agent's `interface` (the control-VLAN address). With neither
  set, the kernel's default source must equal the announced address
  (single-homed box).
- **Python:** the project's ≥ 3.12 runtime, stdlib only.

Agent configuration on the computer-side box:

```toml
node_id = "desk-usb"
role = "consumer"
kind = "usb"
state_file = "/var/lib/yoke/state.json"

[driver]
type = "usb-gadget"
udc = "fcc00000.usb"
# socket = "/run/yoke-gadget/control.sock"
# busid = "1-1"   # required when the producer advertises several devices
# source = "10.20.30.2"   # defaults to the agent's interface address
```

Packaging needs for the computer role:

| Item | Value |
|---|---|
| Kernel modules | `raw_gadget` (`CONFIG_USB_RAW_GADGET`); `dwc3` built with gadget/dual-role support; no `usb_f_*`/`libcomposite` gadget bound to `fcc00000.usb` |
| Device node | `/dev/raw-gadget` (misc), udev `KERNEL=="raw-gadget", OWNER="yoke-gadget", MODE="0600"` |
| Users | system user `yoke-gadget`, primary group `yoke` (the agent's group) |
| Capabilities | none (`CapabilityBoundingSet=` empty); access through the device node owner only |
| Runtime directory | `/run/yoke-gadget` (0750, `yoke-gadget:yoke`), socket `control.sock` 0660 |
| Network | outbound TCP to the producer's 3240 from the control-VLAN IPv4 |
| Unit | `examples/yoke-gadget.service`; no change to `yoked.service` |
| Sysfs (read) | `/sys/class/udc/fcc00000.usb/{state,current_speed}` (agent and worker) |

## Simulation and evidence

`tests/usbsim` is the offline proof. The data flows:

```text
host model ↔ simulated UDC/Raw Gadget ↔ proxy ↔ USB/IP ↔ exporter ↔ device model
```

- The simulated kernel runs the real `RawGadgetBackend` dispatch against the
  ioctl surface and returns `-errno` (no exceptions, no retries), with:
  - Raw Gadget's EBUSY/EINVAL rules;
  - the dwc3 gadget name, so RUN fails on a wrong name;
  - signals that are lost unless the thread is blocked;
  - EPIPE when the host clears a halt on an endpoint with a started
    request;
  - dwc3 disconnect leaving requests queued;
  - a late ep0 reply blocking (and later SETUPs stalling) until bus reset.
- The exporter enforces the stock stub's checks:
  - a connection drop on a bad `devid`;
  - desynchronization on a bad endpoint, because the OUT payload is left
    unread;
  - EBADR and a drop on a control wLength mismatch;
  - a WARN on a control direction mismatch.

  It also offers both stub_tx unlink orderings.
- The host model enumerates like Linux, Windows and macOS (modelled
  sequences). It records §9.2.6.4 deadline misses as advisory metrics and
  fails only after the 5 s host-stack timeout.

The end-to-end tests are in `tests/test_usb_proxy.py`, and regressions for
the 2026-11-04 cold review are in `tests/test_usb_faults.py`.

**Real-kernel loopback (`make verify-usb-kernel`).** QEMU/KVM boots the
host's own kernel with the host root shared read-only. No root is needed on
the host. Inside the VM:

- A real configfs mass-storage gadget on `dummy_udc.1` is exported by the
  kernel `usbip-host` stub. A minimal `usbipd` performs the sysfs handoff,
  because the tools are not installed.
- The production worker presents it through `/dev/raw-gadget` on
  `dummy_udc.0`, driven by the real `usb-gadget` driver over the control
  socket.
- The guest's `usb-storage` reads and writes it.

Results on 2026-11-04 (kernel 7.2.14, 2 vCPUs):

| Check | Result |
|---|---|
| 8 MiB read / write via the proxied `/dev/sda`, checksums, data landed in the source file | pass (high-speed) |
| Throughput (dummy_hcd has no bus limit; direct source read 139 MB/s) | 93.7 MB/s read, 73.2 MB/s write |
| Driver evidence | ACTIVE: worker `configured`, UDC sysfs `configured`, link high-speed |
| Detach through the driver | confirmed; device gone; export released (FIN reached the stub) |
| Remote loss (stub `usbip_sockfd = -1`) | disconnect presented in 0.1 s |
| usbipd restart | re-imported and re-enumerated 1.6 s after restart |
| Worker SIGTERM | exit 0 in 0.11 s (blocked ioctls interrupted) |
| Negative control: same worker with `fcntl.ioctl` | teardown unconfirmed, exit 1 (as the review predicted) |
| `dmesg` WARN/BUG/BOGUS | none |

The first run measured 2.3 MB/s because the minimal usbipd lacked
`TCP_NODELAY`: Nagle plus delayed ACK added about 40 ms per command. Stock
usbipd sets it (`usbip_net_set_nodelay`), and so does the worker; any
replacement exporter must too.

Benchmarks
(`make benchmark-usb`) measure simulated, software-only latency and
throughput; see the results table below.

Results from `make benchmark-usb USB_BENCH_OPTIONS=--markdown`
(2026-11-04, Python 3.14, x86_64 workstation). These are **simulated,
software-only figures**. The model has no USB bus bandwidth limit, so values
above the real high-speed ceiling (about 40 MB/s) only show the
proxy's own overhead. "Baseline" turns off pipelining, coalescing, batched
submits, BOT sizing and the descriptor cache. "Optimized" is the default
policy. Payloads are 1 MiB.

| Scenario | RTT | Baseline | Optimized |
|---|---|---|---|
| Windows-style enumeration | 1 ms | 19.1 ms, 13 requests forwarded | 6.1 ms, 2 forwarded |
| HID report latency (device event → host) | 1 ms | median 1.23 ms | median 0.64 ms |
| Generic bulk IN (packet per URB) | 1 ms | 0.39 MB/s | 6.1 MB/s (depth 16) |
| Generic bulk OUT (exact STALL, window 1) | 1 ms | 0.39 MB/s | 0.40 MB/s; 1.7 MB/s with window 4 |
| Mass storage READ(10) 64 KiB | 1 ms | 0.40 MB/s | 46.7 MB/s |
| Mass storage WRITE(10) 64 KiB | 1 ms | 0.41 MB/s | 19.8 MB/s |
| Generic bulk IN | 0 | 6.7 MB/s, 2047 gadget writes | 22.0 MB/s, 127 writes |
| Enumeration deadline misses (all RTTs 0–4 ms) | — | 0 | 0 |

Generic bulk IN scales with queue depth (`depth × packet / RTT`). At a
1 ms RTT, depth 4 gives 1.6 MB/s, 16 gives 6.6 MB/s and 32 gives
12.4 MB/s, so the default is 16. The optimizations are:

- pipelined IN URBs;
- coalesced gadget writes (16× fewer write calls);
- one stream write per URB batch;
- reused receive buffers with header headroom (no copy between socket and
  ioctl);
- BOT sizing (one URB per SCSI data phase instead of per packet);
- the descriptor cache (2 forwarded requests instead of 13).

Forwarded standard no-data requests (SET_CONFIGURATION, SET_INTERFACE)
cost one RTT plus device time against the computer's 50 ms limit. The
simulation shows a 60 ms RTT failing SET_CONFIGURATION while cached
descriptor reads stay under 1 ms.

## First bench procedure (two ZERO 3Es)

1. **Device-side box.**
   - Plug in a USB 2 HID keyboard.
   - Bind it with `usbip bind -b <busid>` or the `usbip` producer driver
     and run `usbipd`.
   - Check `usbip list -r 127.0.0.1`.
2. **Computer-side kernel.**
   - `modprobe raw_gadget` and check that `/dev/raw-gadget` exists.
   - `ls /sys/class/udc` should list `fcc00000.usb`.
   - Clear the `UDC` file of any configfs gadget and put the DWC3 port in
     device role (`/sys/class/usb_role/*/role`, or a `dr_mode` overlay).
   - Record `uname -r`, `zcat /proc/config.gz | grep RAW_GADGET` and
     `cat /sys/class/udc/*/state`.
3. **Power.** Connect the computer-side box's OTG port to a separate
   computer through a USB power meter. Record the boot current before and
   after enumeration. This also checks that the board runs from that port
   alone.
4. **Standalone worker** (no agent): run
   `python -m yoke.usb.worker --udc fcc00000.usb --target <device-box-ip>:3240/<busid> --source <computer-box-vlan-ip>`.
   The worker logs the inferred gadget name; if RUN fails with EBUSY,
   pass `--gadget-driver dwc3-gadget`. Expected:
   - `/sys/class/udc/fcc00000.usb/state` reaches `configured` and
     `current_speed` is `full-speed` (low-speed keyboard presented at full
     speed);
   - the computer lists the keyboard (`lsusb -v` on Linux, Device Manager
     on Windows, System Information on macOS);
   - keys type, and Caps Lock toggles the keyboard LED (the SET_REPORT
     path).
5. **Faults.** With the worker running:
   - unplug and replug the keyboard (the computer sees a disconnect, then
     re-enumeration);
   - pull and restore Ethernet;
   - restart `usbipd`;
   - reboot the computer (bus reset and suspend);
   - `systemctl restart yoke-gadget`.

   Record recovery time and `journalctl -u yoke-gadget`.
6. **Second device.** Repeat with a USB 2 flash drive: high-speed, BOT.
   Copy 200 MB both ways, verify checksums, and record throughput and
   `current_speed`.
7. **Agent path.** Install `examples/yoke-gadget.service`, configure the
   `usb-gadget` driver, `yoke pair` the boxes, and check that `yoke status`
   shows the consumer active with UDC state `configured`. Unpair and check
   that the computer sees the device disappear.

Each step's pass/fail, kernel version and measurements belong in
`doc/research/HARDWARE.md` before any ⚠ above is cleared.

## Limits and open work

### STALLs that cannot reach the computer

Raw Gadget delays the status stage only for requests without data, so a
control OUT request *with data* is acknowledged when its data arrives. A
bulk/interrupt OUT packet is acknowledged when a read is queued, so a
device's refusal surfaces on the next transaction.

| Class | Affected request | Effect |
|---|---|---|
| HID | SET_REPORT (LEDs, output/feature reports) | a device STALL is reported as success |
| CDC-ACM | SET_LINE_CODING (7 bytes) | an unsupported line coding looks accepted; GET_LINE_CODING shows the truth |
| Mass storage (BOT) | bulk OUT data or CBW refused by STALL | seen one packet late; BOT reset recovery still converges |
| Vendor | any OUT request with data | a failure is visible only in the device's later state |
| Audio, DFU, printer | SET_CUR, DNLOAD, and similar | same as vendor; not tested |

### Other limits

- **Remote wakeup** is not supported (no Raw Gadget ioctl), so the presented
  device never advertises it.
- **Packet-per-URB forwarding** costs per-packet Python overhead for non-BOT
  bulk devices. A native worker behind `GadgetBackend` is the path to full
  high-speed throughput.
- **Host clear-halt visibility.** dwc3 reports a host CLEAR_FEATURE(HALT)
  only through started requests (EPIPE). A device that keeps a halt until
  it sees that specific request is released by the parked-pump retry, not
  by the host's request itself.
- **Bus power.** A RK3566 board drawing more than 100 mA before enumeration
  exceeds the USB 2 unconfigured-device budget. Many hosts tolerate it; it
  must be measured ⚠.
- **Isochronous, hubs and multiple devices** are future work.

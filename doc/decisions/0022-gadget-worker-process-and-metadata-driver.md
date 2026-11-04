# 0022 — Gadget worker process, metadata driver and simulation evidence

Date: 2026-11-04. Status: accepted.

## Context

ADR 0021 puts USB payload in a userspace proxy. ADRs 0002 and 0011 forbid
the agent and control plane from receiving or inspecting media payload.
ADR 0017 requires drivers to retain ownership until teardown is confirmed
and to describe their status evidence. The agent's systemd unit is
unprivileged and sandboxed (`ProtectSystem=strict`, AF_UNIX/AF_INET only).
The proxy needs `/dev/raw-gadget` and an outbound TCP connection.

## Decision

1. **Separate worker process.** `python -m yoke.usb.worker` owns the gadget
   and the USB/IP socket. It runs as its own system user under its own unit
   (`examples/yoke-gadget.service`). The agent never imports the payload
   modules; a test enforces that boundary.
2. **Metadata-only control channel.** The new `usb-gadget` consumer driver
   talks to the worker over a Unix stream socket (`yoke.usb.control`). Each
   message is one JSON line with a closed, size-bounded schema:
   - a *route* is a positive generation plus
     `{host, port, busid, source}` or `null`. `source` is the control-VLAN
     IPv4 that the producer's data-plane admission expects;
   - the reply is the worker's status: state, detail, generation, target,
     link speed, device IDs and counters.

   Connecting to a socket under a read-only `/run` works inside the agent's
   existing sandbox, so its unit needs no change.
3. **Generation fencing.** The driver chooses
   `max(worker generation, own generation) + 1`. The worker adopts a
   generation only after it has confirmed teardown of every older session:
   threads joined, URBs unlinked and the gadget closed. A failed teardown
   leaves the old generation in place and reports `error`. The driver then
   raises `DriverError` and retains its desired route, so no replacement can
   start while the old session might still own the UDC.
4. **Repeated attaches are idempotent.** A worker that already presents the
   requested target (and is not in `error`) is adopted without a new
   generation. A producer flapping back to active, or an agent restart,
   therefore never makes the computer re-enumerate. A repeated detach of
   an idle worker is likewise a no-op.
5. **The agent is the durable authority.** The worker keeps routes in
   memory. A restarted worker reports generation 0; the driver's poll
   reports a lost route as `error`, and the core's retry reapplies it.
6. **Status evidence.** The driver reports `active` only when the worker
   presents the requested device in state `configured` or `suspended` *and*
   the kernel's `/sys/class/udc/<udc>/state` agrees. It reports `pending`
   while the worker connects (it retries remote loss itself with capped
   backoff) or while it waits for the computer to enumerate. It reports
   `error` for rejection, stale or unreachable workers, and lost routes.
   `active` does not mean that an application uses the device.
7. **Simulation is the offline evidence.** `tests/usbsim` provides:
   - device models;
   - an exporter with stock-stub semantics over real sockets and in-memory
     streams;
   - a simulated Raw Gadget kernel that runs the real ioctl encodings;
   - a host enforcing §9.2.6.4 deadlines.

   End-to-end tests and `benchmarks/usb_datapath.py` use them. Their
   numbers are simulated, software-only evidence and never hardware claims.

## Consequences

- Deployment adds one unit, one system user and a udev rule granting it
  `/dev/raw-gadget`. Kernel requirements are documented in
  `doc/design/USB-DATAPATH.md`.
- A worker crash closes the Raw Gadget file descriptor, which unbinds the
  gadget. The computer sees a disconnect; systemd restarts the worker and
  the agent reapplies the route.
- Requests and status cost one short Unix-socket connection each, which is
  negligible beside the 2 s announce interval.

## Alternatives considered

- **Agent-spawned child process.** Simpler supervision, but the worker would
  inherit the agent's sandbox and lack device access unless the agent's unit
  is weakened. Rejected.
- **Route and status files in a shared directory.** Workable, but the
  agent's `ProtectSystem=strict` permits writes only under `/var/lib/yoke`.
  A privileged writer in a user-owned directory needs extra care against
  link attacks. Rejected in favor of a socket.

## Amendment (2026-11-04, cold review)

- **Builds outside the route lock.** The worker builds sessions without
  holding its route lock. A route change aborts a build in progress by
  shutting down its socket, which also aborts a connect, so `apply()` stays
  well inside the agent's 10 s acknowledgment timeout.
- **Fence check.** A build whose generation was superseded is stopped
  before the new generation is acknowledged, so the fence holds.
- **Exact generation.** The driver adopts a route only when the worker
  acknowledges exactly the generation it sent.
- **Reconnect backoff** is capped at 2 s, so a usbipd restart recovers in
  about 1.6 s in the VM test. An unsupported device is retried every 30 s.
- **Idempotent stop.** `Proxy.stop()` is idempotent and serialized. Each
  blocked thread gets its own join deadline.

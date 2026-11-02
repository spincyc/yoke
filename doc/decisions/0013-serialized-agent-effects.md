# 0013 — Serialized driver effects and explicit agent lifecycle

Date: 2026-11-02. Status: accepted.

The former asyncio lock around `to_thread` calls serialized tasks only
until cancellation. A cancelled task released the lock while its actual
thread could still change hardware. A queued poll could also report a
previous pairing's health after a replacement intent. Persistence errors
were logged while the shell continued to send successful acknowledgments.

Use a dedicated single-worker executor for every operation on an agent's
driver, including description, startup/export, attach, detach, health
polling, and shutdown. Queue operations synchronously in issue order.
Cancelling an awaiting coroutine does not cancel or release a running
operation; shutdown drains the actual worker before final teardown and
closes the executor. Drivers must bound their blocking I/O: Python cannot
safely interrupt an arbitrary running thread.

Keep one actual device call in flight and one bounded desired-route slot:
a required detach barrier plus the latest attach. A newer route replaces
superseded pending attaches before they reach hardware; an unpair clears
pending attaches and retains teardown. Use one drain task rather than a
task and executor entry for every control update. Shutdown drops obsolete
pending routes and runs its final teardown after the actual in-flight call.
At most one periodic poll can wait in the same executor; it runs before a
later route submission, so route churn cannot starve health polling.

Tag each scheduled attach/detach and each poll with the runner's current
media generation. Only the current generation may publish a status. Polls
are ordered with commands in the same worker. A poll is tagged with the
last successfully applied hardware generation and is withheld while route
work is pending or in flight. A command that raises, including validation
before touching the old route, reports failure against its requested
generation without promoting the physical generation. Subsequent healthy
polls of the old resource therefore cannot clear the replacement error or
cancel its retry. A retained detach barrier keeps its own command generation
rather than borrowing the generation of a later pending attach. A successful
command does promote the generation even if its immediate health poll fails;
later health recovery then belongs to the applied route. Driver failures are contained and reported as status;
shutdown failures are logged after the final protocol effects.

The shell serializes core inputs and their effect batches. Persistence
runs outside the event loop, but its result is an explicit core input:
`Persist(state, request_id)` completes through
`on_persist_result(request_id, success, now)`. A failed durable transition
must not attach media or send an applied acknowledgment. Startup cannot
advertise or drive media until its durable sequence reservation succeeds
(ADR 0012). Hold an exclusive local state-file lease from before load
through worker drain so two processes cannot reserve the same sequences
or race the same configured endpoint. A failure there stops startup with
an actionable state error.

Use a monotonic clock for all core deadlines and a separate wall clock for
wire timestamps and the initial replay-sequence floor. Wall-clock
corrections therefore cannot expire peers or suspend retries.

An ephemeral management CLI can exit without ending media: the endpoint
agent owns the local runtime. Orderly agent shutdown stops accepting
control inputs, emits its stopping/bye effects, drains outstanding device
operations, and detaches its owned consumer media or unbinds its producer
exports. The durable pairing survives and is restored on restart. A hard
agent crash has device-specific effects; continued media is not promised.
Independently supervised physical endpoint workers would need an explicit
future lifecycle contract. This change does not add such a supervisor or
move media payload into the control plane.

The optional service-readiness integration uses the standard systemd
[`NOTIFY_SOCKET`/`READY=1` protocol](https://www.freedesktop.org/software/systemd/man/latest/sd_notify.html),
with no added listener or health file. Capture and remove `NOTIFY_SOCKET`
from the process environment before driver description or external children,
including on startup failure. Send readiness once configuration, state lease,
control socket, driver description, and durable startup reservation are ready
and the control loops have been scheduled. A failed save or initialization
never signals readiness. Ordinary CLI execution without this environment
variable has no readiness side effect. Filesystem and Linux abstract Unix
datagram addresses are supported; a failed optional notification is logged.

Readiness describes the local control runtime, not media flow, peer presence,
or appliance/export health. Producer binding is therefore queued on the same
serialized driver worker after the durable barrier and reports its result
asynchronously while control traffic is handled. Stop drains that one bind
before unbinding. A slow or failed appliance does not prevent local control
readiness. The sample service uses `Type=notify` and `NotifyAccess=main`;
no watchdog, management lease, or independent media supervisor is added.

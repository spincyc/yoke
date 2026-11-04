# Offline control-plane simulation

The installed package includes a deterministic software harness. It opens no
sockets, reads no clock and requires no devices:

```sh
python -m yoke.simulation
python -m yoke.simulation --seed 42 --json
python -m yoke.simulation --scenario partition-conflict --trace
```

Exit status is zero only when every selected scenario passes. `--scenario`
can be repeated; its default is the full suite. JSON summaries carry
`schema_version`, the seed, each scenario's checks, final committed and saved
pairing, driver attachment, work counters and a transmission digest. `--trace`
adds one JSON line per transmitted control datagram, with virtual time,
origin, sequence, type and body. It contains control metadata only.

The suite covers clean pairing, packet loss, duplication, delay, reordering,
partition healing with conflicting equal-counter intents, producer loss and
return, driver retries, failed persistence, a crash after the durable write
but before completion feedback, sequence recovery after clock rollback and
passive observer noninterference. Fault scenarios include a clean recovery
period; loss also retries the intent after recovery. Passing establishes
eventual convergence for that finite schedule, not a guarantee under
permanent loss or arbitrary Byzantine behavior.

`Simulation` in `yoke.simulation` hosts real `Node` instances and runs the real
wire encoder/decoder with a simulation PSK. `SimDriver` supplies deterministic
device responses. Every successful save updates a separate in-memory durable
slot and feeds `Node.on_persist_result`; failed saves leave that slot intact.
The harness checks that outgoing sequences were durably reserved, applied
ACKs follow durable pairing and attachment follows persistence. Driver status
feedback follows the original effect batch, preserving sequence order and
discarding intermediate command completions as the real runner does.

```python
from yoke.core.types import Pairing, Role
from yoke.simulation import FaultPolicy, Simulation

net = Simulation(seed=7)
net.add_node("producer", Role.PRODUCER)
net.add_node("consumer", Role.CONSUMER)
net.run(3)
net.set_faults(FaultPolicy(drop=0.2, dup=0.3, jitter=0.05, reorder=0.2))
net.intent("consumer", "producer", version=(1, "operator"), origin="operator")
net.run(5)
net.clear_faults()
net.intent("consumer", "producer", version=(1, "operator"), origin="operator")
net.run(8)
net.assert_agreement("consumer", Pairing("producer", (1, "operator")))
```

Fault policies apply to an exact source/destination link, then a source, then
a destination, then the default. `drop`, `dup` and `reorder` are probabilities;
`latency` and `jitter` are seconds. Each duplicated copy receives an independent
delay. Jitter naturally reorders packets; `reorder` adds an additional delay
to selected copies. A partition takes disjoint groups including any ephemeral
intent origins; unlisted ids are isolated. It blocks packets both when sent
and when delivered, including traffic queued before the partition.

`fail_saves`, `crash_during_next_save`, `set_online` and `restart` expose field
failure boundaries. A restart retains the last successful durable slot and
can receive a backwards `wall_time`; virtual timer time remains monotonic.
`Endpoint.driver` exposes the existing `SimDriver` fault scripts. `inject`
accepts arbitrary bytes for authentication/replay regressions. An observer is
an ideal sender-side bus tap using the real `Snooper`; adding observers
consumes no random draws and generates no protocol traffic. Actual receiver
capture loss and switch multicast behavior belong in network tests.

The scheduler uses a packet heap and revision-tagged timer heap, avoiding a
scan of all nodes on every packet delivery. `record=False` avoids retaining
datagrams while preserving the digest, counters, protocol work and invariant
checks. An event budget stops a faulty schedule from spinning indefinitely.

## Repeatable benchmark

```sh
python benchmarks/control.py --nodes 12 --intents 1000 --repeat 5
python benchmarks/control.py --nodes 32 --intents 10000 --repeat 5 --observers 1
```

The workload uses two HDMI producers and the remaining nodes as consumers,
rotates pairings between producers, drains the virtual network and checks
every affected consumer's durable pairing and every peer's announced view.
Repeats must have identical state, work counters and transmission digest.
The JSON report records Python/host details, workload parameters, per-run
wall/CPU durations and medians. Timings are informational; there is no speed
threshold. Compare the same workload and environment before and after a
change. Encoding, authentication, state-machine work, driver commands and
save feedback are included; disk fsync, physical drivers and real networking
are simulated. This benchmark therefore measures control software, not
switch throughput, media payload performance or hardware latency.

The existing `tests/simnet.py` remains the test-focused harness for historical
convergence regressions and the field-failure scenarios added by ADRs 0023
and 0024. Its virtual clock is true time and every node's monotonic clock;
each node also has a wall-clock skew, set at boot or reboot and changed by
`step_clock` (an NTP step or a bad RTC), and receives `now + skew` with
every input. Each sender has a transport address, and `inject_raw` can
claim any address, so producer data-plane admission is simulated end to
end: `data_plane_admits` models the rendered firewall rules, including
replayed and forged claims. `driver_status` reports backend health from
outside a command, such as a producer's device being unplugged and
replugged. Scenarios cover booting before time sync, clock steps forward
and back on either side of an active route, a far-future reboot, a restart
far behind the durable bound, an immediate consumer retry on producer
replug, and admission of exactly the claiming consumer. The installed harness uses public core APIs and has
no dependency on the test package. `tests/test_simulation.py` pins seeded
scenario outcomes, replayable output, clock/socket independence, observer
noninterference, persistence crash boundaries and fault-policy behavior.

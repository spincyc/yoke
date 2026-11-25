"""Shared control-plane intent, inspection, and observation helpers.

Kept separate from :mod:`yoke.cli` so optional components such as
:mod:`yoke.telemetry` can execute the exact same signed YWP/1 control
operations and inspection formatters without importing the top-level CLI
shell (ADR 0031).
"""

from __future__ import annotations

import json
import math
import secrets
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from yoke.core import frames
from yoke.core.types import (
    CLI_DEADLINE,
    CLI_RETRIES,
    MAX_SEQUENCE,
    NODE_ID_RE,
    PEER_TIMEOUT,
    VERSION_ZERO,
    Body,
    FrameType,
    Role,
    Version,
)
from yoke.net.mcast import open_multicast_socket, validate_multicast
from yoke.snoop import NodeView, Snooper
from yoke.terminal import escape_terminal

COLLECT_WINDOW = 2.5
RETRY_SPACING_BASE = 0.5
STATUS_SORT_KEYS = ("id", "name", "type", "state", "age")
_DIAGNOSTIC_DROPS = ("bad-sig", "stale", "version", "malformed", "oversize")

# recv(deadline) -> (raw datagram, receive time) or None once the absolute
# deadline (in the injected clock's domain) has passed with nothing received.
RecvFn = Callable[[float], "tuple[bytes, float] | None"]


def _read(clock: Callable[[], float] | None) -> float | None:
    """Re-read an optional wall clock per datagram (ADR 0023)."""
    return clock() if clock is not None else None


def new_operation_author() -> str:
    """A fresh valid YWP/1 author independent of the reusable CLI identity."""
    return "op-" + secrets.token_hex(14)


def default_origin(hostname: str) -> str:
    """``cli-`` + sanitized hostname, guaranteed to satisfy NODE_ID_RE."""
    sanitized = "".join(
        ch if ch.isascii() and (ch.islower() or ch.isdigit() or ch == "-") else "-"
        for ch in hostname.lower()
    )
    candidate = ("cli-" + sanitized)[:32]
    return candidate if NODE_ID_RE.fullmatch(candidate) else "cli-unknown"


@dataclass(frozen=True)
class IntentIO:
    """Injectable I/O; ``operation_author`` must be fresh for each operation."""

    send: Callable[[bytes], None]
    recv: RecvFn
    clock: Callable[[], float]
    seq: Callable[[], int]
    psk: bytes
    origin: str
    collect_window: float = COLLECT_WINDOW
    retries: int = CLI_RETRIES
    retry_base: float = RETRY_SPACING_BASE
    deadline: float = CLI_DEADLINE
    wall_clock: Callable[[], float] | None = None
    operation_author: Callable[[], str] = new_operation_author


@dataclass(frozen=True)
class IntentOutcome:
    exit_code: int
    out: tuple[str, ...]
    err: tuple[str, ...]
    sent_version: Version | None
    sends: int
    consumer_announced: bool
    committed: bool = False
    activation: str | None = None


@dataclass(frozen=True)
class BatchIntentResult:
    """Outcome targeting zero or more consumers (e.g. unpair-producer)."""

    exit_code: int
    producer: str
    results: tuple[tuple[str, IntentOutcome], ...]
    out: tuple[str, ...]
    err: tuple[str, ...]


def id_errors(op: str, consumer: str, producer: str | None) -> list[str]:
    """Validate node ids before any I/O happens (PROTOCOL.md id syntax)."""
    errors = []
    if op not in ("pair", "unpair"):
        errors.append(f"invalid operation: {op!r}")
    if not NODE_ID_RE.fullmatch(consumer):
        errors.append(f"invalid consumer id: {consumer!r}")
    if op == "pair" and (producer is None or not NODE_ID_RE.fullmatch(producer)):
        errors.append(f"invalid producer id: {producer!r}")
    if op == "pair" and producer == consumer:
        errors.append("producer and consumer must differ (self-pair)")
    return errors


def fmt_version(version: Version) -> str:
    return f"[{version[0]},{version[1]}]"


def drop_summary(counts: dict[str, int] | None) -> str:
    if not counts:
        return ""
    parts = [f"{k}={counts[k]}" for k in _DIAGNOSTIC_DROPS if counts.get(k, 0) > 0]
    return ", ".join(parts)


def compact_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def driver_detail_text(driver_status: Body | None) -> str:
    if not isinstance(driver_status, dict) or not driver_status:
        return ""
    detail = driver_status.get("detail")
    if isinstance(detail, str):
        return escape_terminal(detail)
    return escape_terminal(compact_json(driver_status))


def _validate_io_bounds(io: IntentIO) -> list[str]:
    problems: list[str] = []
    if not NODE_ID_RE.fullmatch(io.origin):
        problems.append(f"invalid origin id: {io.origin!r}")
    for label, value in (
        ("collect_window", io.collect_window),
        ("retry_base", io.retry_base),
        ("deadline", io.deadline),
    ):
        if not math.isfinite(value) or value <= 0:
            problems.append(f"{label} must be finite and positive")
    if type(io.retries) is not int or not 1 <= io.retries <= CLI_RETRIES:
        problems.append(f"retries must be from 1 through {CLI_RETRIES}")
    return problems


def _send_and_wait_ack(
    io: IntentIO,
    snooper: Snooper,
    *,
    op: str,
    consumer: str,
    producer: str | None,
    version: Version,
    announced: bool,
    err: list[str],
) -> IntentOutcome:
    body: Body = {"op": op, "consumer": consumer, "version": [version[0], version[1]]}
    if op == "pair":
        body["producer"] = producer

    t0 = io.clock()
    hard_deadline = t0 + io.deadline
    offsets = [io.retry_base * (2**k - 1) for k in range(io.retries)]
    sends = 0
    while True:
        now = io.clock()
        if now >= hard_deadline:
            break
        if sends < len(offsets) and now >= t0 + offsets[sends]:
            stamp = io.wall_clock() if io.wall_clock is not None else now
            raw = frames.encode(FrameType.INT, io.origin, io.seq(), stamp, body, io.psk)
            io.send(raw)
            sends += 1
            continue
        if sends < len(offsets):
            next_wake = min(hard_deadline, t0 + offsets[sends])
        else:
            next_wake = hard_deadline
        item = io.recv(next_wake)
        if item is None:
            continue
        for record in snooper.observe(*item, wall=_read(io.wall_clock)):
            frame = record.frame
            if (
                record.verdict == "ok"
                and frame is not None
                and frame.type is FrameType.ACK
                and frame.origin == consumer
                and frame.body.get("version") == [version[0], version[1]]
            ):
                reason = frame.body.get("reason")
                reason_text = reason if isinstance(reason, str) else "?"
                if frame.body.get("applied") is True:
                    target = f" <- {producer}" if op == "pair" else ""
                    view = snooper.nodes.get(consumer)
                    activation = (
                        view.state
                        if view is not None
                        and view.pairing_version == version
                        and view.pairing_producer == producer
                        else "unconfirmed"
                    )
                    line = (
                        f"{op} {consumer}{target} committed at version "
                        f"{fmt_version(version)} ({reason_text}); "
                        f"activation {activation} "
                        "(yoke status shows current media state)"
                    )
                    return IntentOutcome(
                        0, (line,), tuple(err), version, sends, True, True, activation
                    )
                line = f"{op} rejected by {consumer}: {reason_text}"
                return IntentOutcome(2, (), (*err, line), version, sends, True)
        announced = announced or consumer in snooper.nodes

    diagnosis = (
        "consumer never announced" if not announced else "consumer announced but no ack"
    )
    drops = drop_summary(snooper.counts)
    drop_note = f"; dropped: {drops}" if drops else ""
    err.append(
        f"no ack from {consumer!r} within {io.deadline:g}s: "
        f"{diagnosis} ({sends} intents sent{drop_note})"
    )
    return IntentOutcome(1, (), tuple(err), version, sends, announced)


def run_intent(
    io: IntentIO, op: str, consumer: str, producer: str | None = None
) -> IntentOutcome:
    """Send a pair/unpair intent and resolve it against the ack stream.

    Phases per PROTOCOL.md: collect announces (learn the consumer's pairing
    version, sanity-check kinds), then send the intent with version
    ``[n+1, fresh_operation_author]`` on the CLI retry schedule until its
    matching ack arrives or the deadline passes. Retries retain this one
    version; a new operation never reuses the stable CLI identity as its
    version author.
    """
    problems = id_errors(op, consumer, producer)
    problems.extend(_validate_io_bounds(io))
    if NODE_ID_RE.fullmatch(io.origin) and io.origin in (consumer, producer):
        problems.append("CLI origin must differ from endpoint ids")
    if problems:
        return IntentOutcome(2, (), tuple(problems), None, 0, False)

    author = io.operation_author()
    if not isinstance(author, str) or not NODE_ID_RE.fullmatch(author):
        return IntentOutcome(2, (), ("invalid operation author",), None, 0, False)

    err: list[str] = []
    start = io.clock()
    snooper = Snooper(
        io.psk,
        now=start,
        wall_time=io.wall_clock() if io.wall_clock is not None else start,
    )
    window_end = start + io.collect_window
    while io.clock() < window_end and consumer not in snooper.nodes:
        item = io.recv(window_end)
        if item is None:
            break
        snooper.observe(*item, wall=_read(io.wall_clock))

    if io.origin in snooper.nodes:
        return IntentOutcome(
            2, (), ("CLI origin is already announced by an endpoint",), None, 0, False
        )

    consumer_view = snooper.nodes.get(consumer)
    announced = consumer_view is not None
    if consumer_view is None:
        n = 0
        drops = drop_summary(snooper.counts)
        drop_note = f" (dropped: {drops})" if drops else ""
        err.append(
            f"warning: consumer {consumer!r} has not announced yet; "
            f"proceeding with pairing version 0{drop_note}"
        )
    else:
        if consumer_view.role != Role.CONSUMER.value:
            err.append(
                f"{consumer!r} announces as a {consumer_view.role}, not a "
                "consumer; it will never ack an intent — refusing to send"
            )
            return IntentOutcome(2, (), tuple(err), None, 0, announced)
        n = (consumer_view.pairing_version or VERSION_ZERO)[0]
    if n >= MAX_SEQUENCE:
        return IntentOutcome(
            2, (), ("consumer pairing version is exhausted",), None, 0, announced
        )
    if op == "pair" and producer is not None:
        producer_view = snooper.nodes.get(producer)
        if producer_view is not None and producer_view.role != Role.PRODUCER.value:
            err.append(
                f"{producer!r} announces as a {producer_view.role}, not a "
                "producer; refusing to send"
            )
            return IntentOutcome(2, (), tuple(err), None, 0, announced)
        if (
            consumer_view is not None
            and producer_view is not None
            and consumer_view.kind != producer_view.kind
        ):
            err.append(
                f"kind mismatch: consumer {consumer} is {consumer_view.kind}, "
                f"producer {producer} is {producer_view.kind}; refusing to send"
            )
            return IntentOutcome(2, (), tuple(err), None, 0, announced)

    version: Version = (n + 1, author)
    return _send_and_wait_ack(
        io,
        snooper,
        op=op,
        consumer=consumer,
        producer=producer,
        version=version,
        announced=announced,
        err=err,
    )


def run_reapply(io: IntentIO, consumer: str) -> tuple[str | None, IntentOutcome]:
    """Re-assert a consumer's currently announced pairing at version ``n + 1``.

    Returns ``(resolved_producer, outcome)`` where ``resolved_producer`` is the
    producer id for a ``pair`` re-assertion or ``None`` for an ``unpair``
    re-assertion (or when collection failed before sending).
    """
    problems: list[str] = []
    if not NODE_ID_RE.fullmatch(consumer):
        problems.append(f"invalid consumer id: {consumer!r}")
    problems.extend(_validate_io_bounds(io))
    if NODE_ID_RE.fullmatch(io.origin) and io.origin == consumer:
        problems.append("CLI origin must differ from endpoint ids")
    if problems:
        return None, IntentOutcome(2, (), tuple(problems), None, 0, False)

    author = io.operation_author()
    if not isinstance(author, str) or not NODE_ID_RE.fullmatch(author):
        return None, IntentOutcome(2, (), ("invalid operation author",), None, 0, False)

    start = io.clock()
    snooper = Snooper(
        io.psk,
        now=start,
        wall_time=io.wall_clock() if io.wall_clock is not None else start,
    )
    window_end = start + io.collect_window
    while io.clock() < window_end and consumer not in snooper.nodes:
        item = io.recv(window_end)
        if item is None:
            break
        snooper.observe(*item, wall=_read(io.wall_clock))

    if io.origin in snooper.nodes:
        return None, IntentOutcome(
            2, (), ("CLI origin is already announced by an endpoint",), None, 0, False
        )

    view = snooper.nodes.get(consumer)
    if view is None:
        drops = drop_summary(snooper.counts)
        drop_note = f" (dropped: {drops})" if drops else ""
        return None, IntentOutcome(
            1,
            (),
            (
                f"consumer {consumer!r} did not announce within "
                f"{io.collect_window:g}s; cannot determine pairing to reapply"
                f"{drop_note}",
            ),
            None,
            0,
            False,
        )
    if view.role != Role.CONSUMER.value:
        return None, IntentOutcome(
            2,
            (),
            (
                f"{consumer!r} announces as a {view.role}, not a consumer; "
                "refusing to reapply",
            ),
            None,
            0,
            True,
        )
    n = (view.pairing_version or VERSION_ZERO)[0]
    if n >= MAX_SEQUENCE:
        return None, IntentOutcome(
            2, (), ("consumer pairing version is exhausted",), None, 0, True
        )
    producer = view.pairing_producer
    op = "pair" if producer is not None else "unpair"
    version: Version = (n + 1, author)
    outcome = _send_and_wait_ack(
        io,
        snooper,
        op=op,
        consumer=consumer,
        producer=producer,
        version=version,
        announced=True,
        err=[],
    )
    return producer, outcome


def run_unpair_producer(io: IntentIO, producer: str) -> BatchIntentResult:
    """Unpair every observed consumer currently paired to ``producer``."""
    problems: list[str] = []
    if not NODE_ID_RE.fullmatch(producer):
        problems.append(f"invalid producer id: {producer!r}")
    problems.extend(_validate_io_bounds(io))
    if NODE_ID_RE.fullmatch(io.origin) and io.origin == producer:
        problems.append("CLI origin must differ from endpoint ids")
    if problems:
        return BatchIntentResult(2, producer, (), (), tuple(problems))

    start = io.clock()
    snooper = Snooper(
        io.psk,
        now=start,
        wall_time=io.wall_clock() if io.wall_clock is not None else start,
    )
    window_end = start + io.collect_window
    while io.clock() < window_end:
        item = io.recv(window_end)
        if item is None:
            break
        snooper.observe(*item, wall=_read(io.wall_clock))

    if io.origin in snooper.nodes:
        return BatchIntentResult(
            2, producer, (), (), ("CLI origin is already announced by an endpoint",)
        )

    producer_view = snooper.nodes.get(producer)
    if producer_view is not None and producer_view.role != Role.PRODUCER.value:
        return BatchIntentResult(
            2,
            producer,
            (),
            (),
            (
                f"{producer!r} announces as a {producer_view.role}, not a "
                "producer; refusing to unpair",
            ),
        )

    targets = [
        (node_id, view)
        for node_id, view in sorted(snooper.nodes.items())
        if view.role == Role.CONSUMER.value and view.pairing_producer == producer
    ]
    if not targets:
        return BatchIntentResult(
            0, producer, (), (f"no consumers currently paired to {producer}",), ()
        )

    results: list[tuple[str, IntentOutcome]] = []
    out_lines: list[str] = []
    err_lines: list[str] = []
    worst_code = 0
    for consumer_id, view in targets:
        n = (view.pairing_version or VERSION_ZERO)[0]
        if n >= MAX_SEQUENCE:
            outcome = IntentOutcome(
                2, (), ("consumer pairing version is exhausted",), None, 0, True
            )
        else:
            author = io.operation_author()
            if not isinstance(author, str) or not NODE_ID_RE.fullmatch(author):
                outcome = IntentOutcome(
                    2, (), ("invalid operation author",), None, 0, True
                )
            else:
                outcome = _send_and_wait_ack(
                    io,
                    snooper,
                    op="unpair",
                    consumer=consumer_id,
                    producer=None,
                    version=(n + 1, author),
                    announced=True,
                    err=[],
                )
        results.append((consumer_id, outcome))
        out_lines.extend(outcome.out)
        err_lines.extend(outcome.err)
        if outcome.exit_code > worst_code:
            worst_code = outcome.exit_code
    return BatchIntentResult(
        worst_code,
        producer,
        tuple(results),
        tuple(out_lines),
        tuple(err_lines),
    )


def collect(
    recv: RecvFn,
    clock: Callable[[], float],
    psk: bytes,
    window: float,
    *,
    wall_clock: Callable[[], float] | None = None,
) -> Snooper:
    """Feed everything received within ``window`` seconds into a Snooper."""
    if not math.isfinite(window) or window <= 0:
        raise ValueError("collection window must be finite and positive")
    start = clock()
    snooper = Snooper(
        psk, now=start, wall_time=wall_clock() if wall_clock is not None else start
    )
    end = start + window
    while clock() < end:
        item = recv(end)
        if item is None:
            break
        snooper.observe(*item, wall=_read(wall_clock))
    return snooper


def sort_nodes(
    nodes: dict[str, NodeView], now: float, sort_key: str = "id"
) -> list[tuple[str, NodeView]]:
    """Order observed nodes using the shared CLI/TUI sort vocabulary."""
    items = list(nodes.items())
    if sort_key == "name":
        return sorted(items, key=lambda pair: (pair[1].name.lower(), pair[0]))
    if sort_key == "type":
        return sorted(items, key=lambda pair: (pair[1].kind, pair[0]))
    if sort_key == "state":
        return sorted(items, key=lambda pair: (pair[1].state, pair[0]))
    if sort_key == "age":
        return sorted(
            items, key=lambda pair: (max(0.0, now - pair[1].last_seen), pair[0])
        )
    return sorted(items, key=lambda pair: pair[0])


def inspect_node_data(
    snooper: Snooper, node_id: str, now: float, *, psk: bytes
) -> dict[str, Any] | None:
    """Build a complete, JSON-safe inspection record for one observed endpoint."""
    snooper.topology(now)
    view = snooper.nodes.get(node_id)
    if view is None:
        return None
    age = max(0.0, now - view.last_seen)
    live = age <= PEER_TIMEOUT
    intent = snooper.intents.get(node_id)
    paired_consumers = (
        sorted(
            peer_id
            for peer_id, peer in snooper.nodes.items()
            if peer.role == Role.CONSUMER.value and peer.pairing_producer == node_id
        )
        if view.role == Role.PRODUCER.value
        else []
    )
    return {
        "schema_version": 1,
        "authentication": "site-psk" if psk else "open-unauthenticated",
        "node": {
            "id": node_id,
            "role": view.role,
            "kind": view.kind,
            "name": view.name,
            "state": view.state,
            "live": live,
            "stale": not live,
            "age_seconds": round(age, 6),
            "err": view.err,
            "driver_status": deepcopy(view.driver_status),
            "pairing": (
                {
                    "producer": view.pairing_producer,
                    "version": list(view.pairing_version or VERSION_ZERO),
                }
                if view.role == Role.CONSUMER.value
                else None
            ),
            "media": deepcopy(view.media) if view.role == Role.PRODUCER.value else None,
            "paired_consumers": paired_consumers,
            "last_intent": (
                {
                    "op": intent.op,
                    "producer": intent.producer,
                    "version": list(intent.version),
                    "acked": intent.acked,
                    "applied": intent.applied,
                    "reason": intent.reason,
                    "ambiguous": intent.ambiguous,
                }
                if intent is not None
                else None
            ),
        },
        "counts": dict(snooper.counts),
    }


def render_inspect_text(data: dict[str, Any]) -> str:
    """Render an inspection dictionary into clean, terminal-safe text lines."""
    node = data["node"]
    lines = [
        f"ID:      {escape_terminal(str(node['id']))}",
        f"Name:    {escape_terminal(str(node['name']))}",
        f"Role:    {node['role']} ({node['kind']})",
        f"State:   {node['state']} ({'live' if node['live'] else 'stale'}, "
        f"age {node['age_seconds']:.1f}s)",
    ]
    if node["role"] == "consumer":
        pairing = node.get("pairing") or {}
        producer = pairing.get("producer") or "unpaired"
        version = pairing.get("version") or [0, ""]
        lines.append(f"Pairing: {producer} @ [{version[0]},{version[1]}]")
    else:
        consumers = node.get("paired_consumers") or []
        attached = ", ".join(consumers) if consumers else "none"
        lines.append(f"Paired:  {attached}")
        media = node.get("media")
        media_text = (
            escape_terminal(compact_json(media)) if media is not None else "none"
        )
        lines.append(f"Media:   {media_text}")
    if node.get("err"):
        lines.append(f"Error:   {escape_terminal(str(node['err']))}")
    detail = driver_detail_text(node.get("driver_status"))
    lines.append(f"Backend: {detail or '(none announced)'}")
    intent = node.get("last_intent")
    if isinstance(intent, dict):
        ver = intent.get("version") or [0, ""]
        target = f" -> {intent['producer']}" if intent.get("producer") else ""
        ack_str = (
            f"ack={intent['applied']} ({intent['reason']})"
            if intent.get("acked")
            else "unacked"
        )
        lines.append(f"Intent:  {intent['op']}{target} @ [{ver[0]},{ver[1]}] {ack_str}")
    drops = drop_summary(data.get("counts"))
    if drops:
        lines.append(f"Drops:   {drops}")
    return "\n".join(lines)


def intent_outcome_body(
    *,
    op: str,
    consumer: str,
    producer: str | None,
    psk: bytes,
    outcome: IntentOutcome,
) -> dict[str, Any]:
    """Canonical JSON result schema shared by pair, unpair, and reapply."""
    return {
        "schema_version": 1,
        "op": op,
        "consumer": consumer,
        "producer": producer,
        "authentication": "site-psk" if psk else "open-unauthenticated",
        "exit_code": outcome.exit_code,
        "version": list(outcome.sent_version) if outcome.sent_version else None,
        "committed": outcome.committed,
        "activation": outcome.activation,
        "sends": outcome.sends,
        "errors": list(outcome.err),
    }


def _open_live_io(
    *,
    group: str,
    port: int,
    interface: str,
    psk: bytes,
    origin: str,
    collect_window: float = COLLECT_WINDOW,
    retries: int = CLI_RETRIES,
    retry_base: float = RETRY_SPACING_BASE,
    deadline: float = CLI_DEADLINE,
) -> tuple[Any, IntentIO]:
    validate_multicast(group, port, interface=interface)
    sock = open_multicast_socket(group, port, interface=interface)
    last_seq = 0

    def send(raw: bytes) -> None:
        sock.sendto(raw, (group, port))

    def recv_until(abs_deadline: float) -> tuple[bytes, float] | None:
        remaining = abs_deadline - time.monotonic()
        if remaining <= 0:
            return None
        sock.settimeout(remaining)
        try:
            raw, _addr = sock.recvfrom(65535)
        except TimeoutError:
            return None
        return raw, time.monotonic()

    def next_seq() -> int:
        nonlocal last_seq
        last_seq = max(last_seq + 1, time.time_ns())
        return last_seq

    io = IntentIO(
        send=send,
        recv=recv_until,
        clock=time.monotonic,
        seq=next_seq,
        psk=psk,
        origin=origin,
        collect_window=collect_window,
        retries=retries,
        retry_base=retry_base,
        deadline=deadline,
        wall_clock=time.time,
    )
    return sock, io


def execute_multicast_intent(
    *,
    group: str,
    port: int,
    interface: str,
    psk: bytes,
    origin: str,
    op: str,
    consumer: str,
    producer: str | None = None,
    collect_window: float = COLLECT_WINDOW,
    deadline: float = CLI_DEADLINE,
) -> IntentOutcome:
    """Open a short-lived multicast socket and run one pair/unpair intent."""
    sock, io = _open_live_io(
        group=group,
        port=port,
        interface=interface,
        psk=psk,
        origin=origin,
        collect_window=collect_window,
        deadline=deadline,
    )
    try:
        return run_intent(io, op, consumer, producer)
    finally:
        sock.close()


def execute_multicast_reapply(
    *,
    group: str,
    port: int,
    interface: str,
    psk: bytes,
    origin: str,
    consumer: str,
    collect_window: float = COLLECT_WINDOW,
    deadline: float = CLI_DEADLINE,
) -> tuple[str | None, IntentOutcome]:
    """Open a short-lived multicast socket and re-assert ``consumer``'s pairing."""
    sock, io = _open_live_io(
        group=group,
        port=port,
        interface=interface,
        psk=psk,
        origin=origin,
        collect_window=collect_window,
        deadline=deadline,
    )
    try:
        return run_reapply(io, consumer)
    finally:
        sock.close()


def execute_multicast_unpair_producer(
    *,
    group: str,
    port: int,
    interface: str,
    psk: bytes,
    origin: str,
    producer: str,
    collect_window: float = COLLECT_WINDOW,
    deadline: float = CLI_DEADLINE,
) -> BatchIntentResult:
    """Open a short-lived multicast socket and unpair consumers of ``producer``."""
    sock, io = _open_live_io(
        group=group,
        port=port,
        interface=interface,
        psk=psk,
        origin=origin,
        collect_window=collect_window,
        deadline=deadline,
    )
    try:
        return run_unpair_producer(io, producer)
    finally:
        sock.close()

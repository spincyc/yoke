"""Sans-I/O agent state machine for the yoke control plane (YWP/1).

``Node`` is a pure state machine: inputs are decoded frames, driver
status reports, and a monotonic clock value passed into every call;
outputs are lists of :class:`~yoke.core.types.Effect` for the shell to
perform. Nothing here touches sockets, clocks, or global state. Wall time
is an input value: the shell reads it with each frame/poll (ADR 0023) so a
clock step changes the future-sequence admission bound on the next frame,
while every timer stays monotonic.
Normative reference: doc/design/PROTOCOL.md.

Spec notes:

- Rejections (self-pair, kind-mismatch, unknown-producer, superseded)
  surface only as the ``ack``, never as an event — PROTOCOL.md lists no
  ``pairing-rejected`` slug. Driver failures happen after the ack and
  surface as ``driver-error`` events, so there is no ``driver-error``
  ack reason either.
- ``AttachMedia`` at intent-apply time requires the producer peer to be
  live (known and not departed/lost); a departed peer's stale media is
  treated as unknown and attach happens on its return.
- Anti-entropy re-sends are RE-ISSUED, not verbatim (ADR 0009): a
  cached intent body is sent as a fresh ``int`` frame under this node's
  own origin and seq. Verbatim re-sends (the original design) livelock:
  once the target consumer has seen any higher-seq frame from the
  intent's author, replay defense stale-drops the verbatim copy
  forever. The version inside the body preserves authorship and total
  order; suppression observes peers' re-issues through the normal
  ``int`` path.
- Intents rejected by their consumer (observed via the multicast ack:
  self-pair, kind-mismatch, unknown-producer) are evicted from the
  anti-entropy cache and remembered per consumer, so a doomed
  highest-version intent is not re-offered forever.
- Peer timeout uses a 1 ms epsilon in ``next_deadline()`` so that
  polling exactly at the returned instant satisfies the strict
  ``now - last_seen > PEER_TIMEOUT`` test.
- Producer driver statuses mirror the consumer transition events
  (``media-up``/``media-down``/``media-pending``) but never schedule an
  ``AttachMedia`` retry; only consumers drive attachment.
- If a paired producer announces ``media: null`` the consumer does not
  attach (and does not detach an already-attached driver); it waits for
  a later announce carrying media.
"""

from __future__ import annotations

import ipaddress
import json
import math
import random
from dataclasses import dataclass

from yoke.core.frames import canonical_bytes, encode
from yoke.core.types import (
    ADMISSION_CONFIRMATIONS,
    ANN_INTERVAL,
    ANN_JITTER,
    INTENT_CACHE_TTL,
    MAX_FRAME_BYTES,
    MAX_SEQUENCE,
    MAX_TRACKED,
    NODE_ID_RE,
    NS_PER_S,
    PEER_FORGET,
    PEER_TIMEOUT,
    PROTOCOL_VERSION,
    RETRY_BACKOFF_BASE,
    RETRY_BACKOFF_CAP,
    SEQ_FLOOR_JUMP_S,
    SEQ_FUTURE_MARGIN_S,
    SEQUENCE_RESERVATION,
    SUPPRESSION_MAX,
    SUPPRESSION_MIN,
    Admission,
    AttachMedia,
    Body,
    DetachMedia,
    DriverStatus,
    DropReason,
    Effect,
    Frame,
    FrameType,
    Kind,
    Log,
    NodeConfig,
    NodeState,
    Pairing,
    Persist,
    PersistedState,
    Role,
    SendFrame,
    Version,
    version_key,
)

_TIMEOUT_EPS = 1e-3
_COUNTER_KEYS = ("malformed", "version", "auth", "stale", "oversize")
# Local diagnostics about clocks repeat at most this often (monotonic s).
_FUTURE_DROP_LOG_INTERVAL = 60.0
_OWN_SEQ_LOG_INTERVAL = 3600.0


def _bounded_detail(detail: str) -> str:
    encoded = detail.encode("utf-8", errors="replace")[:128]
    return encoded.decode("utf-8", errors="ignore")


def _same_json(a: object, b: object) -> bool:
    """Identity of opaque JSON values is their canonical bytes (ADR 0029).

    Python equality would also merge ``1``, ``1.0`` and ``true``; every
    implementation must agree on what counts as a changed value.
    """
    if a is None or b is None:
        return a is b
    return canonical_bytes({"v": a}) == canonical_bytes({"v": b})


def _err_suffix(err: str | None) -> str:
    return f", error {err!r}" if err else ""


@dataclass(frozen=True)
class Peer:
    """A remote node as reconstructed from its announces."""

    node_id: str
    role: Role
    kind: Kind
    name: str
    state: NodeState
    err: str | None
    pairing: Pairing | None  # consumers only
    media: Body | None  # producers only
    last_seen: float
    departed: bool  # True after bye


@dataclass
class _PeerRec:
    role: Role
    kind: Kind
    name: str
    state: NodeState
    err: str | None
    pairing: Pairing | None
    media: Body | None
    last_seen: float
    departed: bool = False  # saw bye
    lost: bool = False  # peer-lost(timeout) already emitted


@dataclass
class _Source:
    """Transport source of an origin's accepted frames (ADR 0024)."""

    admitted: str | None = None
    candidate: str | None = None
    count: int = 0


def _ipv4(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(ipaddress.IPv4Address(value))
    except ValueError:
        return None


def _admission_bound(wall: float) -> int:
    """Largest admissible sequence for a receiver at Unix time ``wall``."""
    return (int(wall) + SEQ_FUTURE_MARGIN_S) * NS_PER_S


def restart_floor(saved: int, wall_time: float) -> int:
    """Restart sequence floor (PROTOCOL.md Replay defense, ADR 0023).

    The durable bound alone prevents reuse; the wall clock may advance a
    nonzero bound by at most ``SEQ_FLOOR_JUMP_S``. A first commission
    (no durable bound) has only the wall clock.
    """
    wall_ns = int(wall_time) * NS_PER_S
    if saved == 0:
        return wall_ns
    return max(saved, min(wall_ns, saved + SEQ_FLOOR_JUMP_S * NS_PER_S))


def rebase_bound(wall_time: float) -> int:
    """Durable bound for the explicit future-reservation recovery.

    No peer whose clock agrees with ``wall_time`` can have admitted a
    sequence above it (ADR 0023).
    """
    return _admission_bound(wall_time)


@dataclass(frozen=True)
class _CachedIntent:
    version: Version
    body: Body
    cached_at: float


@dataclass(frozen=True)
class _PendingSave:
    request_id: int
    kind: str
    state: PersistedState
    effects: tuple[Effect, ...] = ()


def _parse_version(value: object, *, min_n: int = 0) -> Version | None:
    if not isinstance(value, list) or len(value) != 2:
        return None
    n, origin = value
    if type(n) is not int or not min_n <= n <= MAX_SEQUENCE:
        return None
    if not isinstance(origin, str) or (
        (n == 0 and origin != "") or (n > 0 and not NODE_ID_RE.fullmatch(origin))
    ):
        return None
    return (n, origin)


def _parse_intent(body: Body) -> tuple[str, str, object, Version] | None:
    """Defensively parse an ``int`` body; None means malformed."""
    op = body.get("op")
    if op not in ("pair", "unpair"):
        return None
    consumer = body.get("consumer")
    if not isinstance(consumer, str) or not NODE_ID_RE.fullmatch(consumer):
        return None
    version = _parse_version(body.get("version"), min_n=1)
    if version is None:
        return None
    return (op, consumer, body.get("producer"), version)


def _parse_ann(body: Body, now: float) -> _PeerRec | None:
    """Defensively parse an ``ann`` body into a peer record."""
    role_raw = body.get("role")
    kind_raw = body.get("kind")
    state_raw = body.get("state")
    if (
        not isinstance(role_raw, str)
        or not isinstance(kind_raw, str)
        or not isinstance(state_raw, str)
    ):
        return None
    try:
        role = Role(role_raw)
        kind = Kind(kind_raw)
        state = NodeState(state_raw)
    except ValueError:
        return None
    name = body.get("name")
    if not isinstance(name, str) or len(name) > 64:
        return None
    err = body.get("err")
    if err is not None and not isinstance(err, str):
        return None
    pairing: Pairing | None = None
    media: Body | None = None
    if role is Role.CONSUMER:
        raw_pairing = body.get("pairing")
        if not isinstance(raw_pairing, dict):
            return None
        producer = raw_pairing.get("producer")
        if producer is not None and (
            not isinstance(producer, str) or not NODE_ID_RE.fullmatch(producer)
        ):
            return None
        version = _parse_version(raw_pairing.get("version"))
        if version is None or (version[0] == 0 and producer is not None):
            return None
        pairing = Pairing(producer, version)
    else:
        raw_media = body.get("media")
        if raw_media is not None and not isinstance(raw_media, dict):
            return None
        media = raw_media
    return _PeerRec(
        role=role,
        kind=kind,
        name=name,
        state=state,
        err=err,
        pairing=pairing,
        media=media,
        last_seen=now,
    )


class Node:
    """Pure agent state machine for one endpoint (producer or consumer)."""

    def __init__(
        self,
        config: NodeConfig,
        *,
        state: PersistedState,
        now: float,
        media: Body | None = None,
        seed: int = 0,
        wall_time: float | None = None,
        admission: bool = False,
    ) -> None:
        wall_time = now if wall_time is None else wall_time
        if not math.isfinite(now) or not math.isfinite(wall_time) or wall_time < 0:
            raise ValueError(
                "node clocks must be finite; wall time must be nonnegative"
            )
        state = PersistedState.from_json(state.to_json())
        self._config = config
        self._pairing = state.pairing
        self._seq = restart_floor(state.seq, wall_time)
        if self._seq >= MAX_SEQUENCE:
            raise ValueError("outbound sequence space is exhausted")
        self._reserved_until = state.seq
        self._wall_offset = wall_time - now
        self._startup_logs = self._floor_diagnostics(state.seq, wall_time)
        self._future_log_at = float("-inf")
        self._own_seq_log_at = float("-inf")
        self._admission = admission and config.role is Role.PRODUCER
        self._sources: dict[str, _Source] = {}
        self._admitted: tuple[tuple[str, str], ...] = ()
        self._driver_logged: tuple[NodeState, str] | None = None
        self._save_serial = 0
        self._pending_save: _PendingSave | None = None
        self._deferred_effects: tuple[Effect, ...] | None = None
        self._reserve_retry_at: float | None = None
        self._reserve_backoff = RETRY_BACKOFF_BASE
        self._started = False
        self._media = media
        self._rng = random.Random(seed)
        self._err: str | None = None
        self._driver_detail = ""
        self._teardown_pending = (
            config.role is Role.CONSUMER and state.pairing.version[0] > 0
        )
        if config.role is Role.CONSUMER and (
            state.pairing.producer is not None or self._teardown_pending
        ):
            self._state = NodeState.PENDING
        else:
            self._state = NodeState.IDLE
        self._peers: dict[str, _PeerRec] = {}
        self._replay: dict[
            str, tuple[int, bytes]
        ] = {}  # origin -> (seq, canonical bytes)
        self._drops: dict[str, int] = dict.fromkeys(_COUNTER_KEYS, 0)
        self._dups = 0
        self._drops_reported: dict[str, int] = dict(self._drops)
        self._rx_drop_at = float("-inf")
        self._intent_cache: dict[str, _CachedIntent] = {}
        # consumer id -> (rejected version, the producer that was rejected).
        # Keyed on producer too so a corrected retry that reuses the
        # version number but names a valid producer is not suppressed
        # (ADR 0009 amendment; the CLI derives versions from the
        # consumer's announced pairing, which a rejection does not
        # advance, so version reuse is expected).
        self._rejected: dict[str, tuple[Version, object]] = {}
        self._resend_at: dict[str, float] = {}
        self._ann_due = now
        self._retry_at: float | None = None
        self._backoff = RETRY_BACKOFF_BASE
        self._attached_media: Body | None = None
        self._last_now = now
        self._validate_media_description(media)

    # -- public introspection ------------------------------------------------

    @property
    def node_id(self) -> str:
        return self._config.node_id

    @property
    def node_state(self) -> NodeState:
        return self._state

    @property
    def pairing(self) -> Pairing:
        return self._pairing

    @property
    def peers(self) -> dict[str, Peer]:
        return {
            pid: Peer(
                node_id=pid,
                role=rec.role,
                kind=rec.kind,
                name=rec.name,
                state=rec.state,
                err=rec.err,
                pairing=rec.pairing,
                media=rec.media,
                last_seen=rec.last_seen,
                departed=rec.departed,
            )
            for pid, rec in self._peers.items()
        }

    # -- lifecycle -----------------------------------------------------------

    def start(self, now: float) -> list[Effect]:
        self._touch(now)
        if self._started or self._pending_save is not None:
            return []
        # The durable start barrier stays the first effect; diagnostics follow.
        logs: list[Effect] = list(self._startup_logs)
        self._startup_logs = []
        return self._begin_save("start", self._pairing) + logs

    def on_persist_result(
        self, request_id: int, success: bool, now: float
    ) -> list[Effect]:
        """Release effects only after the shell confirms the exact save."""
        self._touch(now)
        pending = self._pending_save
        if pending is None or pending.request_id != request_id:
            return []
        self._pending_save = None
        if not success:
            if pending.kind == "effects":
                self._deferred_effects = pending.effects
                self._reserve_retry_at = now + self._reserve_backoff
                self._reserve_backoff = min(
                    self._reserve_backoff * 2, RETRY_BACKOFF_CAP
                )
            return [Log("durable save failed; pairing unchanged and success withheld")]
        self._reserved_until = pending.state.seq
        self._reserve_backoff = RETRY_BACKOFF_BASE
        if pending.kind == "start":
            self._started = True
            effects: list[Effect] = []
            if self._teardown_pending:
                effects.append(DetachMedia())
            effects.extend([self._evt("started", {}), self._ann(now)])
            if self._admission:
                # Fail closed: a restarted producer never inherits a
                # previous process's data-plane permission (ADR 0024).
                effects.append(Admission(()))
            return self._finish(effects)
        if pending.kind == "pairing":
            return self._finish(self._commit_pairing(pending.state.pairing, now))
        return list(pending.effects)

    def shutdown(self, now: float) -> list[Effect]:
        self._touch(now)
        fx: list[Effect] = []
        if self._config.role is Role.CONSUMER:
            fx.append(DetachMedia())
        if (
            not self._started
            or self._pending_save is not None
            or self._deferred_effects is not None
        ):
            return fx
        return fx + self._finish(
            [
                self._evt("stopping", {}),
                self._send(FrameType.BYE, {}),
            ]
        )

    # -- inputs --------------------------------------------------------------

    def on_frame(
        self,
        frame: Frame,
        raw: bytes,
        now: float,
        *,
        wall: float | None = None,
        source: str | None = None,
    ) -> list[Effect]:
        """Judge one authenticated frame received at monotonic ``now``.

        ``wall`` is the shell's wall-clock reading at receipt (ADR 0023);
        without it the last supplied reading is extrapolated monotonically.
        ``source`` is the datagram's transport source address, used only
        for producer data-plane admission (ADR 0024).
        """
        self._touch(now)
        current_wall = self._wall(now, wall)
        if (
            not self._started
            or self._pending_save is not None
            or self._deferred_effects is not None
        ):
            return []
        if frame.origin == self._config.node_id:
            return []  # own multicast loopback
        if frame.seq > _admission_bound(current_wall):
            # Implausibly-far-future seq (nanoseconds): a spoofed
            # high-water would otherwise stale-drop the real origin's
            # frames indefinitely (ADR 0010). Reject without advancing the
            # replay mark. Integer math keeps the ceiling exact.
            self._drops["stale"] += 1
            return self._future_drop_log(frame, current_wall, now)
        # Dedup on canonical bytes (PROTOCOL.md replay defense; same basis
        # as the snooper, so the oracle reproduces agent verdicts): a
        # re-encoded copy of the same content at the same seq is a dup,
        # not a distinct frame.
        canon = canonical_bytes(json.loads(raw.decode("utf-8")))
        last = self._replay.get(frame.origin)
        if last is not None:
            last_seq, last_canon = last
            if frame.seq < last_seq:
                self._drops["stale"] += 1
                return []
            if frame.seq == last_seq:
                if canon == last_canon:
                    self._dups += 1
                    return []
                self._drops["stale"] += 1
                return []
        self._replay[frame.origin] = (frame.seq, canon)
        if self._admission:
            self._note_source(frame.origin, source)
        fx: list[Effect] = []
        if frame.type is FrameType.ANN:
            fx = self._on_ann(frame, now)
        elif frame.type is FrameType.INT:
            fx = self._on_int(frame, now)
        elif frame.type is FrameType.ACK:
            fx = self._on_ack(frame, now)
        elif frame.type is FrameType.BYE:
            fx = self._on_bye(frame, now)
        self._bound_maps()
        fx.extend(self._admission_update())
        return self._finish(fx)

    def on_drop(self, reason: DropReason, now: float) -> list[Effect]:
        self._touch(now)
        if reason is DropReason.DUP:
            self._dups += 1
        else:
            self._drops[reason.value] += 1
        return []

    def on_driver_status(self, status: DriverStatus, now: float) -> list[Effect]:
        self._touch(now)
        if (
            not self._started
            or self._pending_save is not None
            or self._deferred_effects is not None
        ):
            return []
        fx: list[Effect] = []
        logged = (status.state, status.detail)
        if logged != self._driver_logged:
            # Full, untruncated backend detail stays local (ADR 0023); the
            # wire copy in events and announces is bounded to 128 bytes.
            self._driver_logged = logged
            fx.append(
                Log(
                    f"driver {status.state.value}: {status.detail or '(no detail)'}",
                    "warning" if status.state is NodeState.ERROR else "info",
                )
            )
        prev = self._state
        consumer = self._config.role is Role.CONSUMER
        if consumer and self._pairing.producer is None:
            if status.state is NodeState.IDLE:
                self._teardown_pending = False
                self._retry_at = None
                self._backoff = RETRY_BACKOFF_BASE
            else:
                self._teardown_pending = True
                self._schedule_retry(now)
                if status.state is NodeState.ACTIVE:
                    # It is evidence of unfinished cleanup, never evidence
                    # that the unpaired desired route became active again.
                    if prev not in (NodeState.PENDING, NodeState.ERROR):
                        self._state = NodeState.PENDING
                        fx.extend([self._evt("media-pending", {}), self._ann(now)])
                    return self._finish(fx)
        if status.state is NodeState.ACTIVE:
            if consumer and self._teardown_pending:
                self._schedule_retry(now)
                return self._finish(fx)
            if self._config.role is Role.CONSUMER:
                peer = self._peers.get(self._pairing.producer or "")
                if (
                    peer is None
                    or peer.lost
                    or peer.departed
                    or peer.role is not Role.PRODUCER
                    or peer.kind is not self._config.kind
                ):
                    return fx
            self._driver_detail = _bounded_detail(status.detail)
            self._teardown_pending = False
            self._err = None
            self._backoff = RETRY_BACKOFF_BASE
            self._retry_at = None
            if prev is not NodeState.ACTIVE:
                self._state = NodeState.ACTIVE
                detail: Body = {}
                if self._driver_detail:
                    detail["detail"] = self._driver_detail
                fx.append(self._evt("media-up", detail))
        elif status.state is NodeState.ERROR:
            self._driver_detail = _bounded_detail(status.detail)
            self._state = NodeState.ERROR
            self._err = self._driver_detail or None
            fx.append(self._evt("driver-error", {"detail": self._driver_detail}))
            self._schedule_retry(now)
        elif status.state is NodeState.PENDING:
            self._driver_detail = _bounded_detail(status.detail)
            if prev is not NodeState.PENDING:
                self._state = NodeState.PENDING
                self._err = None
                fx.append(self._evt("media-pending", {}))
            self._schedule_retry(now)
        elif self._config.role is Role.CONSUMER and self._pairing.producer is not None:
            self._teardown_pending = False
            self._driver_detail = _bounded_detail(status.detail)
            if prev is not NodeState.PENDING:
                self._state = NodeState.PENDING
                self._err = None
                fx.append(
                    self._evt("media-pending", {"producer": self._pairing.producer})
                )
            self._schedule_retry(now)
        elif prev is not NodeState.IDLE:  # IDLE
            self._driver_detail = ""
            self._state = NodeState.IDLE
            self._err = None
            fx.append(self._evt("media-down", {}))
        if self._state is not prev:
            fx.append(self._ann(now))
        return self._finish(fx)

    def set_media(self, media: Body, now: float) -> list[Effect]:
        self._touch(now)
        self._validate_media_description(media)
        self._media = media
        if (
            not self._started
            or self._pending_save is not None
            or self._deferred_effects is not None
        ):
            return []
        return self._finish([self._ann(now)])

    def poll(self, now: float, *, wall: float | None = None) -> list[Effect]:
        self._touch(now)
        current_wall = self._wall(now, wall)
        if self._pending_save is not None or not self._started:
            return []
        if self._deferred_effects is not None:
            if self._reserve_retry_at is not None and now >= self._reserve_retry_at:
                effects = self._deferred_effects
                self._deferred_effects = None
                self._reserve_retry_at = None
                return self._begin_save("effects", self._pairing, effects)
            return []
        fx: list[Effect] = self._own_seq_log(current_wall, now)
        for pid, rec in self._peers.items():
            if not rec.departed and not rec.lost and now - rec.last_seen > PEER_TIMEOUT:
                rec.lost = True
                fx.append(
                    Log(
                        f"peer {pid} lost: no announce for {now - rec.last_seen:.1f}s "
                        f"(role {rec.role.value}, kind {rec.kind.value}, last state "
                        f"{rec.state.value}{_err_suffix(rec.err)})",
                        "warning",
                    )
                )
                fx.append(self._evt("peer-lost", {"peer": pid, "reason": "timeout"}))
                fx.extend(self._paired_producer_gone(pid, now))
        self._prune(now)
        fx.extend(self._admission_update())
        if self._retry_at is not None and now >= self._retry_at:
            self._retry_at = None
            fx.extend(self._fire_retry())
        for cid in [c for c, at in self._resend_at.items() if at <= now]:
            del self._resend_at[cid]
            entry = self._cache_get(cid, now)
            if entry is not None:
                # Re-issue under our own origin/seq (ADR 0009): a verbatim
                # copy would be stale-dropped whenever the author has since
                # sent higher-seq traffic.
                fx.append(self._send(FrameType.INT, dict(entry.body)))
        if now >= self._ann_due:
            fx.append(self._ann(now))
        if (
            self._drops != self._drops_reported
            and now >= self._rx_drop_at + ANN_INTERVAL
        ):
            hostile = any(
                self._drops[key] > self._drops_reported[key]
                for key in ("auth", "stale")
            )
            self._drops_reported = dict(self._drops)
            self._rx_drop_at = now
            fx.append(
                Log(
                    "rx-drop counters "
                    + " ".join(f"{k}={v}" for k, v in self._drops.items())
                    + f" dup={self._dups}",
                    "warning" if hostile else "info",
                )
            )
            fx.append(self._evt("rx-drop", dict(self._drops)))
        return self._finish(fx)

    def next_deadline(self) -> float:
        if self._pending_save is not None or not self._started:
            return self._last_now + ANN_INTERVAL
        if self._reserve_retry_at is not None:
            return self._reserve_retry_at
        deadline = self._ann_due
        for rec in self._peers.values():
            if not rec.departed and not rec.lost:
                deadline = min(deadline, rec.last_seen + PEER_TIMEOUT + _TIMEOUT_EPS)
        if self._retry_at is not None:
            deadline = min(deadline, self._retry_at)
        if self._resend_at:
            deadline = min(deadline, min(self._resend_at.values()))
        if self._drops != self._drops_reported:
            deadline = min(deadline, self._rx_drop_at + ANN_INTERVAL)
        return max(deadline, self._last_now)

    # -- frame handlers ------------------------------------------------------

    def _on_ann(self, frame: Frame, now: float) -> list[Effect]:
        rec = _parse_ann(frame.body, now)
        if rec is None:
            self._drops["malformed"] += 1
            return [Log(f"malformed ann body from {frame.origin}")]
        origin = frame.origin
        prev = self._peers.get(origin)
        returned = prev is not None and (prev.departed or prev.lost)
        self._peers[origin] = rec
        fx: list[Effect] = []
        if returned:
            fx.append(
                Log(
                    f"peer {origin} returned (role {rec.role.value}, kind "
                    f"{rec.kind.value}, state {rec.state.value})"
                )
            )
            fx.append(self._evt("peer-returned", {"peer": origin}))
        if self._config.role is Role.CONSUMER and self._pairing.producer == origin:
            if rec.role is not Role.PRODUCER or rec.kind is not self._config.kind:
                # The node we are paired to is not (or is no longer) a
                # compatible producer: a role flip, or a wrong-kind
                # producer we pending-paired to before it announced. Do
                # not attach its media; fall back to pending so a later
                # compatible announce can re-attach cleanly.
                if self._attached_media is not None:
                    self._attached_media = None
                    self._teardown_pending = True
                    fx.append(DetachMedia())
                fx.extend(self._paired_producer_gone(origin, now))
            elif rec.media is not None and (
                returned or not _same_json(self._attached_media, rec.media)
            ):
                self._attached_media = rec.media
                self._teardown_pending = False
                self._retry_at = None
                self._backoff = RETRY_BACKOFF_BASE
                self._state = NodeState.PENDING
                self._err = None
                self._driver_detail = ""
                fx.extend(
                    [
                        self._evt("media-pending", {"producer": origin}),
                        self._ann(now),
                    ]
                )
                fx.append(AttachMedia(origin, rec.media))
            elif (
                rec.media is not None
                and rec.state is NodeState.ACTIVE
                and prev is not None
                and prev.state is not NodeState.ACTIVE
                and not self._teardown_pending
                and (self._state is NodeState.ERROR or self._retry_at is not None)
            ):
                # The producer reports readiness again (device replugged,
                # export or admission restored): reattach now instead of
                # waiting out a backoff of up to 30 s (ADR 0023).
                self._backoff = RETRY_BACKOFF_BASE
                self._retry_at = None
                fx.append(
                    Log(
                        f"producer {origin} became active after "
                        f"{prev.state.value}; retrying attachment now"
                    )
                )
                fx.extend(self._fire_retry())
        if rec.role is Role.CONSUMER and rec.pairing is not None:
            entry = self._cache_get(origin, now)
            if entry is not None:
                if version_key(rec.pairing.version) < version_key(entry.version):
                    if origin not in self._resend_at:
                        jitter = self._rng.uniform(SUPPRESSION_MIN, SUPPRESSION_MAX)
                        self._resend_at[origin] = now + jitter
                else:
                    self._resend_at.pop(origin, None)
        return fx

    def _on_int(self, frame: Frame, now: float) -> list[Effect]:
        parsed = _parse_intent(frame.body)
        if parsed is None:
            self._drops["malformed"] += 1
            return [Log(f"malformed intent body from {frame.origin}")]
        op, consumer, producer, version = parsed
        normalized: Body = {
            "op": op,
            "consumer": consumer,
            "version": list(version),
        }
        cacheable = True
        if op == "pair":
            if isinstance(producer, str) and NODE_ID_RE.fullmatch(producer):
                normalized["producer"] = producer
            else:
                # The target still emits its normative rejection ACK, but
                # bystanders must not retain values that cannot be reissued.
                cacheable = False
        rejected = self._rejected.get(consumer)
        is_rejected = (
            rejected is not None
            and version_key(version) == version_key(rejected[0])
            and _same_json(producer, rejected[1])
        )
        entry = self._cache_get(consumer, now)
        if (
            cacheable
            and (entry is None or version_key(version) > version_key(entry.version))
            and not is_rejected
        ):
            self._intent_cache[consumer] = _CachedIntent(version, normalized, now)
        cached = self._intent_cache.get(consumer)
        if (
            consumer in self._resend_at
            and cached is not None
            and version_key(version) >= version_key(cached.version)
        ):
            del self._resend_at[consumer]
        if self._config.role is Role.CONSUMER and consumer == self._config.node_id:
            return self._apply_intent(op, producer, version, now)
        return []

    def _on_bye(self, frame: Frame, now: float) -> list[Effect]:
        rec = self._peers.get(frame.origin)
        if rec is None or rec.departed or rec.lost:
            if rec is not None:
                rec.departed = True
            return []
        rec.departed = True
        fx: list[Effect] = [
            Log(
                f"peer {frame.origin} departed (bye; role {rec.role.value}, "
                f"last state {rec.state.value})"
            ),
            self._evt("peer-lost", {"peer": frame.origin, "reason": "bye"}),
        ]
        fx.extend(self._paired_producer_gone(frame.origin, now))
        return fx

    # -- consumer intent application -----------------------------------------

    def _apply_intent(
        self, op: str, producer: object, version: Version, now: float
    ) -> list[Effect]:
        current = self._pairing.version
        vk, ck = version_key(version), version_key(current)
        if vk < ck:
            return [self._ack(version, applied=False, reason="superseded")]
        if vk == ck:
            applied_op = "unpair" if self._pairing.producer is None else "pair"
            if op == applied_op and (
                op == "unpair" or producer == self._pairing.producer
            ):
                return [self._ack(version, applied=True, reason="applied")]
            return [self._ack(version, applied=False, reason="superseded")]
        if op == "pair":
            if not isinstance(producer, str) or not NODE_ID_RE.fullmatch(producer):
                self._note_rejection(self._config.node_id, version, producer)
                return [self._ack(version, applied=False, reason="unknown-producer")]
            if producer == self._config.node_id:
                self._note_rejection(self._config.node_id, version, producer)
                return [self._ack(version, applied=False, reason="self-pair")]
            peer = self._peers.get(producer)
            if peer is not None and (
                peer.kind is not self._config.kind or peer.role is not Role.PRODUCER
            ):
                self._note_rejection(self._config.node_id, version, producer)
                return [self._ack(version, applied=False, reason="kind-mismatch")]
            return self._apply_pair(producer, version, now)
        return self._apply_unpair(version, now)

    def _apply_pair(self, producer: str, version: Version, now: float) -> list[Effect]:
        return self._begin_save("pairing", Pairing(producer, version))

    def _apply_unpair(self, version: Version, now: float) -> list[Effect]:
        return self._begin_save("pairing", Pairing(None, version))

    def _commit_pairing(self, pairing: Pairing, now: float) -> list[Effect]:
        previous = self._pairing
        had_media = self._attached_media is not None
        self._pairing = pairing
        producer, version = pairing.producer, pairing.version
        committed = Log(
            f"pairing committed: producer={producer or '(none)'} version="
            f"[{version[0]},{version[1]}] (previous producer="
            f"{previous.producer or '(none)'} version="
            f"[{previous.version[0]},{previous.version[1]}])"
        )
        self._state = NodeState.PENDING
        self._err = None
        self._driver_detail = ""
        self._attached_media = None
        self._retry_at = None
        self._backoff = RETRY_BACKOFF_BASE
        fx: list[Effect] = [committed]
        self._teardown_pending = (
            producer is None or had_media or previous.producer is not None
        )
        if producer is None or had_media or previous.producer is not None:
            fx.append(DetachMedia())
        if producer is None:
            fx.extend(
                [
                    self._ack(version, applied=True, reason="applied"),
                    self._evt(
                        "pairing-applied", {"producer": None, "version": list(version)}
                    ),
                    self._ann(now),
                ]
            )
            return fx
        peer = self._peers.get(producer)
        media = (
            peer.media
            if peer is not None and not peer.departed and not peer.lost
            else None
        )
        if media is not None:
            self._attached_media = media
            self._teardown_pending = False
            fx.append(AttachMedia(producer, media))
        else:
            fx.append(self._evt("media-pending", {"producer": producer}))
        fx.append(self._ack(version, applied=True, reason="applied"))
        fx.append(
            self._evt(
                "pairing-applied",
                {"producer": producer, "version": [version[0], version[1]]},
            )
        )
        fx.append(self._ann(now))
        return fx

    # -- helpers -------------------------------------------------------------

    def _touch(self, now: float) -> None:
        self._last_now = max(self._last_now, now)

    def _wall(self, now: float, wall: float | None) -> float:
        """Current wall-clock estimate; a supplied reading always wins."""
        if wall is not None and math.isfinite(wall) and wall >= 0:
            self._wall_offset = wall - now
            return wall
        return now + self._wall_offset

    @staticmethod
    def _floor_diagnostics(saved: int, wall_time: float) -> list[Effect]:
        logs: list[Effect] = []
        wall_ns = int(wall_time) * NS_PER_S
        if saved == 0:
            logs.append(
                Log(
                    f"sequence floor from wall clock {int(wall_time)} "
                    "(no durable bound; commission only with synchronized time)"
                )
            )
        elif wall_ns > saved + SEQ_FLOOR_JUMP_S * NS_PER_S:
            logs.append(
                Log(
                    f"wall clock is {(wall_ns - saved) // NS_PER_S}s past the "
                    f"durable sequence bound; restart floor advanced by the "
                    f"{SEQ_FLOOR_JUMP_S}s cap (ADR 0023)"
                )
            )
        if saved > _admission_bound(wall_time):
            ahead = (saved - int(wall_time) * NS_PER_S) // NS_PER_S
            logs.append(
                Log(
                    f"durable sequence bound is {ahead}s ahead of this wall clock: "
                    "either this clock is behind (check time synchronization; "
                    "peers with correct clocks still admit this node) or an "
                    "earlier boot persisted a future reservation (peers drop "
                    "this node's frames as future). With the clock confirmed "
                    "correct, stop the agent and run `yoke agent --config FILE "
                    "--rebase-sequence` (ADR 0023)",
                    "warning",
                )
            )
        return logs

    def _future_drop_log(self, frame: Frame, wall: float, now: float) -> list[Effect]:
        if now < self._future_log_at + _FUTURE_DROP_LOG_INTERVAL:
            return []
        self._future_log_at = now
        lead = frame.seq // NS_PER_S - int(wall)
        return [
            Log(
                f"dropped {frame.type.value} from {frame.origin}: sequence is "
                f"{lead}s ahead of local wall clock {int(wall)} (margin "
                f"{SEQ_FUTURE_MARGIN_S}s); if peers are correct this clock is "
                "behind - check time synchronization (ADR 0023)",
                "warning",
            )
        ]

    def _own_seq_log(self, wall: float, now: float) -> list[Effect]:
        bound = _admission_bound(wall)
        if self._seq <= bound or now < self._own_seq_log_at + _OWN_SEQ_LOG_INTERVAL:
            return []
        self._own_seq_log_at = now
        return [
            Log(
                f"own sequence is {(self._seq - bound) // NS_PER_S}s beyond the "
                "future-admission bound of this wall clock; peers whose clocks "
                "agree drop this node's frames. If the clock is correct, stop "
                "the agent and run `yoke agent --config FILE --rebase-sequence` "
                "(ADR 0023)",
                "warning",
            )
        ]

    def _note_source(self, origin: str, source: str | None) -> None:
        """Confirm an origin's source over consecutive accepted frames."""
        address = _ipv4(source)
        rec = self._sources.get(origin)
        if rec is None:
            rec = self._sources[origin] = _Source()
        if address is None or address == rec.admitted:
            rec.candidate, rec.count = None, 0
            return
        if address == rec.candidate:
            rec.count += 1
        else:
            rec.candidate, rec.count = address, 1
        if rec.count >= ADMISSION_CONFIRMATIONS:
            rec.admitted, rec.candidate, rec.count = address, None, 0

    def _admission_set(self) -> tuple[tuple[str, str], ...]:
        admitted: list[tuple[str, str]] = []
        for pid, rec in self._peers.items():
            source = self._sources.get(pid)
            if (
                rec.role is Role.CONSUMER
                and rec.kind is self._config.kind
                and rec.pairing is not None
                and rec.pairing.producer == self._config.node_id
                and not rec.lost
                and not rec.departed
                and source is not None
                and source.admitted is not None
            ):
                admitted.append((pid, source.admitted))
        return tuple(sorted(admitted))

    def _admission_update(self) -> list[Effect]:
        if not self._admission:
            return []
        current = self._admission_set()
        if current == self._admitted:
            return []
        self._admitted = current
        described = ", ".join(f"{pid}@{addr}" for pid, addr in current) or "(none)"
        return [Log(f"data-plane admission set: {described}"), Admission(current)]

    def _next_seq(self) -> int:
        if self._seq >= MAX_SEQUENCE:
            raise ValueError("outbound sequence space is exhausted")
        self._seq += 1
        return self._seq

    def _send(self, frame_type: FrameType, body: Body) -> SendFrame:
        return SendFrame(type=frame_type, seq=self._next_seq(), body=body)

    def _evt(self, event: str, detail: Body) -> SendFrame:
        return self._send(FrameType.EVT, {"event": event, "detail": detail})

    def _ack(self, version: Version, *, applied: bool, reason: str) -> SendFrame:
        return self._send(
            FrameType.ACK,
            {
                "version": [version[0], version[1]],
                "applied": applied,
                "reason": reason,
            },
        )

    def _ann(self, now: float) -> SendFrame:
        """Build an announce and reschedule the periodic timer."""
        jitter = self._rng.uniform(-ANN_JITTER, ANN_JITTER)
        self._ann_due = now + ANN_INTERVAL * (1 + jitter)
        body = self._ann_body()
        # Optional evidence must not crowd an otherwise valid media
        # description out of the bounded wire frame. Budget for a PSK
        # even in open mode; the actual shell supplies the signature.
        if "driver_status" in body and self._announce_size(body) > MAX_FRAME_BYTES:
            del body["driver_status"]
        detail = body["err"]
        if isinstance(detail, str) and self._announce_size(body) > MAX_FRAME_BYTES:
            low, high = 0, len(detail)
            while low < high:
                middle = (low + high + 1) // 2
                body["err"] = detail[:middle]
                if self._announce_size(body) <= MAX_FRAME_BYTES:
                    low = middle
                else:
                    high = middle - 1
            body["err"] = detail[:low]
        return self._send(FrameType.ANN, body)

    def _announce_size(self, body: Body) -> int:
        return len(
            canonical_bytes(
                {
                    "v": PROTOCOL_VERSION,
                    "t": FrameType.ANN.value,
                    "id": self.node_id,
                    "seq": MAX_SEQUENCE,
                    "ts": -1.2345678901234567e308,
                    "body": body,
                    "sig": "0" * 64,
                }
            )
        )

    def _validate_media_description(self, media: Body | None) -> None:
        body = self._ann_body()
        if self._config.role is Role.PRODUCER:
            body["media"] = media
        body["state"] = NodeState.PENDING.value  # longest readiness state
        body["err"] = None
        body.pop("driver_status", None)
        # Reject unsupported JSON or an opaque description that cannot fit
        # even with all optional human status omitted. Never truncate media.
        encode(
            FrameType.ANN,
            self.node_id,
            MAX_SEQUENCE,
            -1.2345678901234567e308,
            body,
            b"wire-budget",
        )

    def _ann_body(self) -> Body:
        body: Body = {
            "role": self._config.role.value,
            "kind": self._config.kind.value,
            "name": self._config.name,
            "state": self._state.value,
            "err": self._err,
        }
        if self._config.role is Role.CONSUMER:
            version = self._pairing.version
            body["pairing"] = {
                "producer": self._pairing.producer,
                "version": [version[0], version[1]],
            }
            body["media"] = None
        else:
            body["pairing"] = None
            body["media"] = self._media
        if self._driver_detail:
            body["driver_status"] = {"detail": self._driver_detail}
        return body

    def _begin_save(
        self,
        kind: str,
        pairing: Pairing,
        effects: tuple[Effect, ...] = (),
    ) -> list[Effect]:
        if self._pending_save is not None:
            return []
        self._save_serial += 1
        state = PersistedState(
            seq=min(MAX_SEQUENCE, self._seq + SEQUENCE_RESERVATION),
            pairing=pairing,
        )
        self._pending_save = _PendingSave(self._save_serial, kind, state, effects)
        return [Persist(state, self._save_serial)]

    def _finish(self, effects: list[Effect]) -> list[Effect]:
        """No outbound frame can escape its durable sequence reservation."""
        if any(isinstance(effect, Persist) for effect in effects):
            return effects
        if any(
            isinstance(effect, SendFrame) and effect.seq > self._reserved_until
            for effect in effects
        ):
            return self._begin_save("effects", self._pairing, tuple(effects))
        return effects

    def _paired_producer_gone(self, peer_id: str, now: float) -> list[Effect]:
        """Consumer's paired producer departed or timed out: go pending."""
        if (
            self._config.role is not Role.CONSUMER
            or self._pairing.producer != peer_id
            or self._state is NodeState.PENDING
        ):
            return []
        if self._teardown_pending and self._state is NodeState.ERROR:
            return []
        self._state = NodeState.PENDING
        self._err = None
        if not self._teardown_pending:
            self._retry_at = None
        return [
            self._evt("media-pending", {"producer": peer_id}),
            self._ann(now),
        ]

    def _fire_retry(self) -> list[Effect]:
        if self._teardown_pending:
            self._backoff = min(self._backoff * 2, RETRY_BACKOFF_CAP)
            return [
                DetachMedia(),
                self._evt("driver-retry", {"operation": "detach"}),
            ]
        producer = self._pairing.producer
        if producer is None:
            return []
        media = self._retry_media()
        if media is None:
            return []
        self._attached_media = media
        self._backoff = min(self._backoff * 2, RETRY_BACKOFF_CAP)
        return [
            AttachMedia(producer, media),
            self._evt("driver-retry", {"producer": producer}),
        ]

    def _schedule_retry(self, now: float) -> None:
        if (
            self._config.role is Role.CONSUMER
            and self._retry_at is None
            and (self._teardown_pending or self._peer_ready())
        ):
            self._retry_at = now + self._backoff

    def _retry_media(self) -> Body | None:
        if self._attached_media is not None:
            return self._attached_media
        producer = self._pairing.producer
        if producer is None:
            return None
        rec = self._peers.get(producer)
        if rec is not None and not rec.departed and not rec.lost:
            return rec.media
        return None

    def _peer_ready(self) -> bool:
        peer = self._peers.get(self._pairing.producer or "")
        return (
            peer is not None
            and peer.role is Role.PRODUCER
            and peer.kind is self._config.kind
            and not peer.departed
            and not peer.lost
            and peer.media is not None
        )

    def _prune(self, now: float) -> None:
        """Bound per-origin bookkeeping against id-cycling exhaustion
        (ADR 0010). Never evict the currently paired producer."""
        keep = self._pairing.producer
        forgotten = [
            pid
            for pid, rec in self._peers.items()
            if pid != keep
            and (rec.departed or rec.lost)
            and now - rec.last_seen > PEER_FORGET
        ]
        for pid in forgotten:
            del self._peers[pid]
            self._replay.pop(pid, None)
            self._sources.pop(pid, None)
        self._bound_maps()

    def _bound_maps(self) -> None:
        keep = {self._pairing.producer}
        # A currently admitted consumer's replay and source rows are never
        # evicted (ADR 0029): a flood of other authentic origins could
        # otherwise let replays of its captured frames rebind admission. One
        # MAX_TRACKED for every map keeps origins >= peers, so the replay map
        # always has an unprotected row to evict.
        admitted = keep | {pid for pid, _ in self._admitted}
        for name in (
            "_replay",
            "_peers",
            "_rejected",
            "_intent_cache",
            "_resend_at",
            "_sources",
        ):
            protected = admitted if name in ("_replay", "_sources") else keep
            self._cap_fifo(getattr(self, name), protected)

    @staticmethod
    def _cap_fifo(mapping: dict[str, object], keep: set[str | None]) -> None:
        while len(mapping) > MAX_TRACKED:
            for oldest in mapping:
                if oldest not in keep:
                    del mapping[oldest]
                    break
            else:
                break

    def _cache_get(self, consumer: str, now: float) -> _CachedIntent | None:
        entry = self._intent_cache.get(consumer)
        if entry is None:
            return None
        if now - entry.cached_at > INTENT_CACHE_TTL:
            del self._intent_cache[consumer]
            self._resend_at.pop(consumer, None)
            return None
        return entry

    _REJECTION_REASONS = frozenset({"self-pair", "kind-mismatch", "unknown-producer"})

    def _note_rejection(
        self, consumer: str, version: Version, producer: object
    ) -> None:
        """A consumer refused (version, producer): stop offering exactly
        that intent, but leave a same-version different-producer retry
        free to circulate."""
        prior = self._rejected.get(consumer)
        if prior is None or version_key(version) >= version_key(prior[0]):
            self._rejected[consumer] = (version, producer)
        entry = self._intent_cache.get(consumer)
        if entry is not None and version_key(entry.version) == version_key(version):
            del self._intent_cache[consumer]
            self._resend_at.pop(consumer, None)

    def _on_ack(self, frame: Frame, now: float) -> list[Effect]:
        body = frame.body
        version = _parse_version(body.get("version"), min_n=1)
        if version is None or body.get("applied") is not False:
            return []
        reason = body.get("reason")
        if not isinstance(reason, str):
            self._drops["malformed"] += 1
            return []
        if reason not in self._REJECTION_REASONS:
            return []
        # The ack does not carry the producer; identify it from our own
        # cached copy of the rejected intent. Without one we cannot tell a
        # corrected retry apart, so we do not pin — self-healing (one more
        # reject cycle) covers that rarer case.
        entry = self._intent_cache.get(frame.origin)
        if entry is not None and version_key(entry.version) == version_key(version):
            self._note_rejection(frame.origin, version, entry.body.get("producer"))
        return []

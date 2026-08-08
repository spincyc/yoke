"""Sans-I/O agent state machine for the yoke control plane (YWP/1).

``Node`` is a pure state machine: inputs are decoded frames, driver
status reports, and a Unix-seconds clock value passed into every call;
outputs are lists of :class:`~yoke.core.types.Effect` for the shell to
perform. Nothing here touches sockets, wall time, or global state.
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

import json
import random
from dataclasses import dataclass

from yoke.core.frames import canonical_bytes
from yoke.core.types import (
    ANN_INTERVAL,
    ANN_JITTER,
    INTENT_CACHE_TTL,
    MAX_TRACKED,
    NODE_ID_RE,
    NS_PER_S,
    PEER_FORGET,
    PEER_TIMEOUT,
    RETRY_BACKOFF_BASE,
    RETRY_BACKOFF_CAP,
    SEQ_FUTURE_MARGIN_S,
    SUPPRESSION_MAX,
    SUPPRESSION_MIN,
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


@dataclass(frozen=True)
class _CachedIntent:
    version: Version
    body: Body
    cached_at: float


def _parse_version(value: object, *, min_n: int = 0) -> Version | None:
    if not isinstance(value, list) or len(value) != 2:
        return None
    n, origin = value
    if isinstance(n, bool) or not isinstance(n, int) or n < min_n:
        return None
    if not isinstance(origin, str):
        return None
    return (n, origin)


def _parse_intent(body: Body) -> tuple[str, str, object, Version] | None:
    """Defensively parse an ``int`` body; None means malformed."""
    op = body.get("op")
    if op not in ("pair", "unpair"):
        return None
    consumer = body.get("consumer")
    if not isinstance(consumer, str) or not NODE_ID_RE.match(consumer):
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
    if not isinstance(name, str):
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
        if producer is not None and not isinstance(producer, str):
            return None
        version = _parse_version(raw_pairing.get("version"))
        if version is None:
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
    ) -> None:
        self._config = config
        self._pairing = state.pairing
        self._seq = max(state.seq, int(now) * NS_PER_S)
        self._media = media
        self._rng = random.Random(seed)
        self._err: str | None = None
        if config.role is Role.CONSUMER and state.pairing.producer is not None:
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
        return [
            self._persist(),
            self._evt("started", {}),
            self._ann(now),
        ]

    def shutdown(self, now: float) -> list[Effect]:
        self._touch(now)
        return [
            self._evt("stopping", {}),
            self._send(FrameType.BYE, {}),
        ]

    # -- inputs --------------------------------------------------------------

    def on_frame(self, frame: Frame, raw: bytes, now: float) -> list[Effect]:
        self._touch(now)
        if frame.origin == self._config.node_id:
            return []  # own multicast loopback
        if frame.seq > (int(now) + SEQ_FUTURE_MARGIN_S) * NS_PER_S:
            # Implausibly-far-future seq (nanoseconds): a spoofed
            # high-water would otherwise stale-drop the real origin's
            # frames indefinitely (ADR 0010). Reject without advancing the
            # replay mark. Integer math keeps the ceiling exact.
            self._drops["stale"] += 1
            return []
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
        if frame.type is FrameType.ANN:
            return self._on_ann(frame, now)
        if frame.type is FrameType.INT:
            return self._on_int(frame, now)
        if frame.type is FrameType.ACK:
            return self._on_ack(frame, now)
        if frame.type is FrameType.BYE:
            return self._on_bye(frame, now)
        return []  # evt: observed, not acted on

    def on_drop(self, reason: DropReason, now: float) -> list[Effect]:
        self._touch(now)
        if reason is DropReason.DUP:
            self._dups += 1
        else:
            self._drops[reason.value] += 1
        return []

    def on_driver_status(self, status: DriverStatus, now: float) -> list[Effect]:
        self._touch(now)
        fx: list[Effect] = []
        prev = self._state
        # A consumer with no pairing has nothing to be active/pending/error
        # about: a status arriving from an attach that completed after an
        # unpair is stale and must not resurrect a torn-down pairing.
        if (
            self._config.role is Role.CONSUMER
            and self._pairing.producer is None
            and status.state is not NodeState.IDLE
        ):
            return []
        if status.state is NodeState.ACTIVE:
            self._err = None
            self._backoff = RETRY_BACKOFF_BASE
            self._retry_at = None
            if prev is not NodeState.ACTIVE:
                self._state = NodeState.ACTIVE
                fx.append(self._evt("media-up", {}))
        elif status.state is NodeState.ERROR:
            self._state = NodeState.ERROR
            self._err = status.detail or None
            fx.append(self._evt("driver-error", {"detail": status.detail}))
            if (
                self._config.role is Role.CONSUMER
                and self._pairing.producer is not None
            ):
                self._retry_at = now + self._backoff
        elif status.state is NodeState.PENDING:
            if prev is not NodeState.PENDING:
                self._state = NodeState.PENDING
                self._err = None
                fx.append(self._evt("media-pending", {}))
        elif prev is not NodeState.IDLE:  # IDLE
            self._state = NodeState.IDLE
            self._err = None
            fx.append(self._evt("media-down", {}))
        if self._state is not prev:
            fx.append(self._ann(now))
        return fx

    def set_media(self, media: Body, now: float) -> list[Effect]:
        self._touch(now)
        self._media = media
        return [self._ann(now)]

    def poll(self, now: float) -> list[Effect]:
        self._touch(now)
        fx: list[Effect] = []
        for pid, rec in self._peers.items():
            if not rec.departed and not rec.lost and now - rec.last_seen > PEER_TIMEOUT:
                rec.lost = True
                fx.append(self._evt("peer-lost", {"peer": pid, "reason": "timeout"}))
                fx.extend(self._paired_producer_gone(pid, now))
        self._prune(now)
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
            self._drops_reported = dict(self._drops)
            self._rx_drop_at = now
            fx.append(self._evt("rx-drop", dict(self._drops)))
        return fx

    def next_deadline(self) -> float:
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
                    fx.append(DetachMedia())
                fx.extend(self._paired_producer_gone(origin, now))
            elif rec.media is not None and (
                returned or self._attached_media != rec.media
            ):
                self._attached_media = rec.media
                fx.append(AttachMedia(origin, rec.media))
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
        rejected = self._rejected.get(consumer)
        is_rejected = (
            rejected is not None
            and version_key(version) == version_key(rejected[0])
            and producer == rejected[1]
        )
        entry = self._cache_get(consumer, now)
        if (
            entry is None or version_key(version) > version_key(entry.version)
        ) and not is_rejected:
            self._intent_cache[consumer] = _CachedIntent(version, dict(frame.body), now)
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
            self._evt("peer-lost", {"peer": frame.origin, "reason": "bye"})
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
            if not isinstance(producer, str) or not NODE_ID_RE.match(producer):
                self._note_rejection(self._config.node_id, version, producer)
                return [self._ack(version, applied=False, reason="unknown-producer")]
            if producer == self._config.node_id:
                self._note_rejection(self._config.node_id, version, producer)
                return [self._ack(version, applied=False, reason="self-pair")]
            peer = self._peers.get(producer)
            if peer is not None and peer.kind is not self._config.kind:
                self._note_rejection(self._config.node_id, version, producer)
                return [self._ack(version, applied=False, reason="kind-mismatch")]
            return self._apply_pair(producer, version, now)
        return self._apply_unpair(version, now)

    def _apply_pair(self, producer: str, version: Version, now: float) -> list[Effect]:
        self._pairing = Pairing(producer, version)
        self._state = NodeState.PENDING
        self._err = None
        self._attached_media = None
        self._retry_at = None
        self._backoff = RETRY_BACKOFF_BASE
        fx: list[Effect] = [self._persist()]
        peer = self._peers.get(producer)
        media = (
            peer.media
            if peer is not None and not peer.departed and not peer.lost
            else None
        )
        if media is not None:
            self._attached_media = media
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

    def _apply_unpair(self, version: Version, now: float) -> list[Effect]:
        self._pairing = Pairing(None, version)
        self._state = NodeState.IDLE
        self._err = None
        self._attached_media = None
        self._retry_at = None
        self._backoff = RETRY_BACKOFF_BASE
        return [
            self._persist(),
            DetachMedia(),
            self._ack(version, applied=True, reason="applied"),
            self._evt(
                "pairing-applied",
                {"producer": None, "version": [version[0], version[1]]},
            ),
            self._ann(now),
        ]

    # -- helpers -------------------------------------------------------------

    def _touch(self, now: float) -> None:
        self._last_now = max(self._last_now, now)

    def _next_seq(self) -> int:
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
        return self._send(FrameType.ANN, self._ann_body())

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
        return body

    def _persist(self) -> Persist:
        return Persist(PersistedState(seq=self._seq, pairing=self._pairing))

    def _paired_producer_gone(self, peer_id: str, now: float) -> list[Effect]:
        """Consumer's paired producer departed or timed out: go pending."""
        if (
            self._config.role is not Role.CONSUMER
            or self._pairing.producer != peer_id
            or self._state is NodeState.PENDING
        ):
            return []
        self._state = NodeState.PENDING
        self._err = None
        self._retry_at = None
        return [
            self._evt("media-pending", {"producer": peer_id}),
            self._ann(now),
        ]

    def _fire_retry(self) -> list[Effect]:
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
        for name in ("_replay", "_peers", "_rejected", "_intent_cache"):
            self._cap_fifo(getattr(self, name), keep)

    @staticmethod
    def _cap_fifo(mapping: dict[str, object], keep: str | None) -> None:
        while len(mapping) > MAX_TRACKED:
            for oldest in mapping:
                if oldest != keep:
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
        if body.get("reason") not in self._REJECTION_REASONS:
            return []
        # The ack does not carry the producer; identify it from our own
        # cached copy of the rejected intent. Without one we cannot tell a
        # corrected retry apart, so we do not pin — self-healing (one more
        # reject cycle) covers that rarer case.
        entry = self._intent_cache.get(frame.origin)
        if entry is not None and version_key(entry.version) == version_key(version):
            self._note_rejection(frame.origin, version, entry.body.get("producer"))
        return []

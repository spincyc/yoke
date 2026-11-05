"""ctypes binding of the native YWP/1 node (native/ywp) for host tests.

``CNode`` has the interface of :class:`yoke.core.node.Node` that the
simulation (tests/simnet.py) and its scenarios use, including the private
attributes they inspect (``_drops``, ``_retry_at``, ``_intent_cache`` ...),
so a C node can stand in for a Python node. Effects come back as the
reference's effect dataclasses. Every datagram the node sends is encoded by
C and checked byte-for-byte against ``frames.encode``; every received
datagram's decode verdict is checked against ``frames.decode``.
"""

from __future__ import annotations

import ctypes
import ipaddress
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yoke.core.frames import FrameDrop, decode, encode
from yoke.core.node import Peer
from yoke.core.types import (
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
)

ID = ctypes.c_char * 33
_ROLES = (Role.PRODUCER, Role.CONSUMER)
_KINDS = (Kind.HDMI, Kind.USB)
_STATES = (NodeState.IDLE, NodeState.PENDING, NodeState.ACTIVE, NodeState.ERROR)
_TYPES = (FrameType.ANN, FrameType.INT, FrameType.ACK, FrameType.EVT, FrameType.BYE)
_LEVELS = ("debug", "info", "warning", "error")
_DROPS = ("ok", "malformed", "version", "auth", "stale", "dup", "oversize")
_COUNTERS = ("malformed", "version", "auth", "stale", "oversize")
FX_SEND, FX_PERSIST, FX_ATTACH, FX_DETACH, FX_ADMISSION, FX_LOG = range(1, 7)


class VersionT(ctypes.Structure):
    _fields_ = (("n", ctypes.c_uint64), ("origin", ID))


class PairingT(ctypes.Structure):
    _fields_ = (("paired", ctypes.c_bool), ("producer", ID), ("version", VersionT))


class SavedT(ctypes.Structure):
    _fields_ = (("seq", ctypes.c_uint64), ("pairing", PairingT))


class LimitsT(ctypes.Structure):
    _fields_ = tuple(
        (name, ctypes.c_uint16)
        for name in (
            "peers",
            "origins",
            "intents",
            "rejections",
            "media_bytes",
            "effects",
            "effect_bytes",
        )
    )


class ConfigT(ctypes.Structure):
    _fields_ = (
        ("node_id", ctypes.c_char_p),
        ("role", ctypes.c_int),
        ("kind", ctypes.c_int),
        ("name", ctypes.c_char_p),
        ("psk", ctypes.c_char_p),
        ("psk_len", ctypes.c_size_t),
        ("admission", ctypes.c_bool),
        ("seed", ctypes.c_uint64),
        ("media", ctypes.c_char_p),
        ("media_len", ctypes.c_size_t),
        ("limits", LimitsT),
    )


class AdmitT(ctypes.Structure):
    _fields_ = (("id", ID), ("addr", ctypes.c_uint32))


class SendU(ctypes.Structure):
    _fields_ = (
        ("type", ctypes.c_int),
        ("seq", ctypes.c_uint64),
        ("body", ctypes.POINTER(ctypes.c_uint8)),
        ("body_len", ctypes.c_size_t),
    )


class PersistU(ctypes.Structure):
    _fields_ = (("state", ctypes.POINTER(SavedT)), ("request_id", ctypes.c_uint32))


class AttachU(ctypes.Structure):
    _fields_ = (
        ("producer", ctypes.c_char_p),
        ("media", ctypes.POINTER(ctypes.c_char)),
        ("media_len", ctypes.c_size_t),
    )


class AdmissionU(ctypes.Structure):
    _fields_ = (("peers", ctypes.POINTER(AdmitT)), ("count", ctypes.c_size_t))


class LogU(ctypes.Structure):
    _fields_ = (("level", ctypes.c_int), ("text", ctypes.c_char_p))


class EffectU(ctypes.Union):
    _fields_ = (
        ("send", SendU),
        ("persist", PersistU),
        ("attach", AttachU),
        ("admission", AdmissionU),
        ("log", LogU),
    )


class EffectT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = (("kind", ctypes.c_int), ("u", EffectU))


class StatusT(ctypes.Structure):
    _fields_ = (
        ("state", ctypes.c_int),
        ("pairing", PairingT),
        ("started", ctypes.c_bool),
        ("save_pending", ctypes.c_bool),
        ("held", ctypes.c_bool),
        ("last_seq", ctypes.c_uint64),
        ("reserved_until", ctypes.c_uint64),
        ("retry_at", ctypes.c_double),
        ("backoff", ctypes.c_double),
        ("ann_due", ctypes.c_double),
        ("drops", ctypes.c_uint64 * 7),
        ("drops_reported", ctypes.c_uint64 * 7),
        ("peers", ctypes.c_size_t),
        ("origins", ctypes.c_size_t),
        ("intents", ctypes.c_size_t),
        ("rejections", ctypes.c_size_t),
        ("admitted", ctypes.c_size_t),
        ("overruns", ctypes.c_uint32),
    )


class PeerT(ctypes.Structure):
    _fields_ = (
        ("id", ID),
        ("role", ctypes.c_int),
        ("kind", ctypes.c_int),
        ("state", ctypes.c_int),
        ("has_pairing", ctypes.c_bool),
        ("pairing", PairingT),
        ("has_media", ctypes.c_bool),
        ("last_seen", ctypes.c_double),
        ("departed", ctypes.c_bool),
        ("lost", ctypes.c_bool),
    )


class IntentT(ctypes.Structure):
    _fields_ = (
        ("consumer", ID),
        ("pair", ctypes.c_bool),
        ("producer", ID),
        ("version", VersionT),
        ("cached_at", ctypes.c_double),
        ("resend", ctypes.c_bool),
        ("resend_at", ctypes.c_double),
    )


class RejectionT(ctypes.Structure):
    _fields_ = (
        ("consumer", ID),
        ("version", VersionT),
        ("producer_kind", ctypes.c_int),
        ("producer", ID),
    )


class Library:
    """The shared library with typed entry points."""

    def __init__(self, path: Path) -> None:
        lib = ctypes.CDLL(str(path))
        self.lib = lib
        node, dbl, size, boolean = (
            ctypes.c_void_p,
            ctypes.c_double,
            ctypes.c_size_t,
            ctypes.c_bool,
        )
        signatures: dict[str, tuple[Any, list[Any]]] = {
            "ywp_limits_default": (None, [ctypes.POINTER(LimitsT)]),
            "ywp_node_footprint": (size, [ctypes.POINTER(LimitsT)]),
            "ywp_node_create": (
                node,
                [
                    ctypes.POINTER(ConfigT),
                    ctypes.c_void_p,
                    ctypes.POINTER(SavedT),
                    dbl,
                    dbl,
                    ctypes.POINTER(ctypes.c_int),
                ],
            ),
            "ywp_node_destroy": (None, [node]),
            "ywp_node_start": (None, [node, dbl]),
            "ywp_node_datagram": (
                None,
                [
                    node,
                    ctypes.c_char_p,
                    size,
                    dbl,
                    dbl,
                    ctypes.POINTER(ctypes.c_uint32),
                ],
            ),
            "ywp_node_persisted": (None, [node, ctypes.c_uint32, boolean, dbl]),
            "ywp_node_driver_status": (
                None,
                [node, ctypes.c_int, ctypes.c_char_p, size, dbl],
            ),
            "ywp_node_set_media": (ctypes.c_int, [node, ctypes.c_char_p, size, dbl]),
            "ywp_node_poll": (None, [node, dbl, dbl]),
            "ywp_node_shutdown": (None, [node, dbl]),
            "ywp_node_next_deadline": (dbl, [node]),
            "ywp_node_effects": (size, [node, ctypes.POINTER(ctypes.POINTER(EffectT))]),
            "ywp_node_encode": (
                size,
                [node, ctypes.POINTER(EffectT), dbl, ctypes.POINTER(ctypes.c_uint8)],
            ),
            "ywp_node_status": (None, [node, ctypes.POINTER(StatusT)]),
            "ywp_node_peer_count": (size, [node]),
            "ywp_node_peer": (boolean, [node, size, ctypes.POINTER(PeerT)]),
            "ywp_node_peer_media": (
                ctypes.POINTER(ctypes.c_char),
                [node, size, ctypes.POINTER(size)],
            ),
            "ywp_node_intent_count": (size, [node]),
            "ywp_node_intent": (boolean, [node, size, ctypes.POINTER(IntentT)]),
            "ywp_node_rejection_count": (size, [node]),
            "ywp_node_rejection": (boolean, [node, size, ctypes.POINTER(RejectionT)]),
            "ywp_err_name": (ctypes.c_char_p, [ctypes.c_int]),
        }
        for name, (restype, argtypes) in signatures.items():
            function = getattr(lib, name)
            function.restype = restype
            function.argtypes = argtypes
        self.portable_ops = ctypes.c_void_p.in_dll(lib, "ywp_crypto_portable")

    def default_limits(self) -> LimitsT:
        limits = LimitsT()
        self.lib.ywp_limits_default(ctypes.byref(limits))
        return limits


def _version(v: VersionT) -> Version:
    return (int(v.n), v.origin.decode())


def _pairing(p: PairingT) -> Pairing:
    return Pairing(p.producer.decode() if p.paired else None, _version(p.version))


def _saved(state: PersistedState) -> SavedT:
    out = SavedT()
    out.seq = state.seq
    out.pairing.paired = state.pairing.producer is not None
    out.pairing.producer = (state.pairing.producer or "").encode()
    out.pairing.version.n = state.pairing.version[0]
    out.pairing.version.origin = state.pairing.version[1].encode()
    return out


@dataclass(frozen=True)
class CSendFrame(SendFrame):
    """A SendFrame that also carries the C node's canonical body bytes."""

    body_bytes: bytes = b""


@dataclass(frozen=True)
class CachedIntentView:
    version: Version
    body: Body
    cached_at: float


_OTHER = object()  # a rejected producer that was not a node id or null


class CNode:
    """A native node with the reference Node's interface."""

    def __init__(
        self,
        lib: Library,
        config: NodeConfig,
        *,
        state: PersistedState,
        now: float,
        psk: bytes,
        media: Body | None = None,
        seed: int = 0,
        wall_time: float | None = None,
        admission: bool = False,
        limits: LimitsT | None = None,
    ) -> None:
        self._lib = lib
        self._config = config
        self._psk = psk
        self._raw_effects: list[EffectT] = []
        wall = now if wall_time is None else wall_time
        cfg = ConfigT()
        self._keep = [config.node_id.encode(), config.name.encode(), psk]
        cfg.node_id, cfg.name, cfg.psk = self._keep
        cfg.psk_len = len(psk)
        cfg.role = _ROLES.index(config.role)
        cfg.kind = _KINDS.index(config.kind)
        cfg.admission = admission
        cfg.seed = abs(seed)
        if media is not None:
            text = json.dumps(media).encode()
            self._keep.append(text)
            cfg.media, cfg.media_len = text, len(text)
        cfg.limits = limits if limits is not None else lib.default_limits()
        err = ctypes.c_int(0)
        saved = _saved(state)
        handle = lib.lib.ywp_node_create(
            ctypes.byref(cfg),
            ctypes.addressof(lib.portable_ops),
            ctypes.byref(saved),
            now,
            wall,
            ctypes.byref(err),
        )
        if not handle:
            name = lib.lib.ywp_err_name(err.value).decode()
            raise ValueError(f"native node refused its configuration: {name}")
        self._handle = handle

    def __del__(self) -> None:
        handle = getattr(self, "_handle", None)
        if handle:
            self._lib.lib.ywp_node_destroy(handle)
            self._handle = None

    # -- the reference Node's public interface -------------------------------

    @property
    def node_id(self) -> str:
        return self._config.node_id

    @property
    def node_state(self) -> NodeState:
        return _STATES[self.status().state]

    @property
    def pairing(self) -> Pairing:
        return _pairing(self.status().pairing)

    @property
    def peers(self) -> dict[str, Peer]:
        lib = self._lib.lib
        out: dict[str, Peer] = {}
        for index in range(lib.ywp_node_peer_count(self._handle)):
            row = PeerT()
            lib.ywp_node_peer(self._handle, index, ctypes.byref(row))
            size = ctypes.c_size_t(0)
            media_ptr = lib.ywp_node_peer_media(self._handle, index, ctypes.byref(size))
            media = (
                json.loads(ctypes.string_at(media_ptr, size.value))
                if media_ptr
                else None
            )
            node_id = row.id.decode()
            out[node_id] = Peer(
                node_id=node_id,
                role=_ROLES[row.role],
                kind=_KINDS[row.kind],
                name="",
                state=_STATES[row.state],
                err=None,
                pairing=_pairing(row.pairing) if row.has_pairing else None,
                media=media,
                last_seen=row.last_seen,
                departed=row.departed,
            )
        return out

    def start(self, now: float) -> list[Effect]:
        self._lib.lib.ywp_node_start(self._handle, now)
        return self._effects()

    def on_persist_result(
        self, request_id: int, success: bool, now: float
    ) -> list[Effect]:
        self._lib.lib.ywp_node_persisted(self._handle, request_id, success, now)
        return self._effects()

    def shutdown(self, now: float) -> list[Effect]:
        self._lib.lib.ywp_node_shutdown(self._handle, now)
        return self._effects()

    def on_datagram(
        self,
        raw: bytes,
        now: float,
        *,
        wall: float | None = None,
        source: str | None = None,
    ) -> list[Effect]:
        """Decode and handle one datagram in C, checking the decode verdict."""
        try:
            decode(raw, self._psk)
            expected: str | None = None
        except FrameDrop as drop:
            expected = drop.reason.value
        before = self.status().drops[:]
        address = None
        if source is not None:
            try:
                address = ctypes.c_uint32(int(ipaddress.IPv4Address(source)))
            except ValueError:
                address = None
        self._lib.lib.ywp_node_datagram(
            self._handle,
            raw,
            len(raw),
            now,
            math.nan if wall is None else wall,
            ctypes.byref(address) if address is not None else None,
        )
        after = self.status().drops[:]
        delta = {
            _DROPS[i]: after[i] - before[i] for i in range(7) if after[i] != before[i]
        }
        if expected is not None:
            assert delta == {expected: 1}, (raw[:120], expected, delta)
        else:
            assert not {"version", "auth", "oversize"} & set(delta), (raw[:120], delta)
        return self._effects()

    def on_frame(
        self,
        frame: Frame,
        raw: bytes,
        now: float,
        *,
        wall: float | None = None,
        source: str | None = None,
    ) -> list[Effect]:
        return self.on_datagram(raw, now, wall=wall, source=source)

    def on_drop(self, reason: DropReason, now: float) -> list[Effect]:
        raise AssertionError("native nodes decode their own datagrams (on_datagram)")

    def on_driver_status(self, status: DriverStatus, now: float) -> list[Effect]:
        detail = status.detail.encode("utf-8", errors="replace")
        self._lib.lib.ywp_node_driver_status(
            self._handle, _STATES.index(status.state), detail, len(detail), now
        )
        return self._effects()

    def set_media(self, media: Body, now: float) -> list[Effect]:
        text = json.dumps(media).encode()
        err = self._lib.lib.ywp_node_set_media(self._handle, text, len(text), now)
        effects = self._effects()
        if err:
            raise ValueError(
                f"media rejected: {self._lib.lib.ywp_err_name(err).decode()}"
            )
        return effects

    def poll(self, now: float, *, wall: float | None = None) -> list[Effect]:
        self._lib.lib.ywp_node_poll(
            self._handle, now, math.nan if wall is None else wall
        )
        return self._effects()

    def next_deadline(self) -> float:
        return float(self._lib.lib.ywp_node_next_deadline(self._handle))

    # -- sending -----------------------------------------------------------------

    def encode_frame(self, effect: SendFrame, ts: float) -> bytes:
        """Encode in C at wall time ts; must equal the reference encoder."""
        assert isinstance(effect, CSendFrame)
        body = (ctypes.c_uint8 * len(effect.body_bytes)).from_buffer_copy(
            effect.body_bytes
        )
        fx = EffectT()
        fx.kind = FX_SEND
        fx.send.type = _TYPES.index(effect.type)
        fx.send.seq = effect.seq
        fx.send.body = ctypes.cast(body, ctypes.POINTER(ctypes.c_uint8))
        fx.send.body_len = len(effect.body_bytes)
        out = (ctypes.c_uint8 * 1200)()
        n = self._lib.lib.ywp_node_encode(self._handle, ctypes.byref(fx), ts, out)
        raw = bytes(out[:n])
        assert raw == encode(
            effect.type, self.node_id, effect.seq, ts, effect.body, self._psk
        ), "C and Python encoders differ"
        return raw

    # -- introspection used by the simulation and scenarios ----------------------

    def status(self) -> StatusT:
        out = StatusT()
        self._lib.lib.ywp_node_status(self._handle, ctypes.byref(out))
        return out

    @property
    def overruns(self) -> int:
        return int(self.status().overruns)

    @property
    def _drops(self) -> dict[str, int]:
        drops = self.status().drops
        return {key: int(drops[_DROPS.index(key)]) for key in _COUNTERS}

    @property
    def _drops_reported(self) -> dict[str, int]:
        drops = self.status().drops_reported
        return {key: int(drops[_DROPS.index(key)]) for key in _COUNTERS}

    @property
    def _dups(self) -> int:
        return int(self.status().drops[_DROPS.index("dup")])

    @property
    def _retry_at(self) -> float | None:
        value = self.status().retry_at
        return None if math.isnan(value) else float(value)

    @property
    def _backoff(self) -> float:
        return float(self.status().backoff)

    def _intents(self) -> list[IntentT]:
        lib = self._lib.lib
        rows = []
        for index in range(lib.ywp_node_intent_count(self._handle)):
            row = IntentT()
            lib.ywp_node_intent(self._handle, index, ctypes.byref(row))
            rows.append(row)
        return rows

    @property
    def _intent_cache(self) -> dict[str, CachedIntentView]:
        out: dict[str, CachedIntentView] = {}
        for row in self._intents():
            consumer = row.consumer.decode()
            version = _version(row.version)
            body: Body = {
                "op": "pair" if row.pair else "unpair",
                "consumer": consumer,
                "version": list(version),
            }
            if row.pair:
                body["producer"] = row.producer.decode()
            out[consumer] = CachedIntentView(version, body, row.cached_at)
        return out

    @property
    def _resend_at(self) -> dict[str, float]:
        return {
            row.consumer.decode(): row.resend_at
            for row in self._intents()
            if row.resend
        }

    @property
    def _rejected(self) -> dict[str, tuple[Version, object]]:
        lib = self._lib.lib
        out: dict[str, tuple[Version, object]] = {}
        for index in range(lib.ywp_node_rejection_count(self._handle)):
            row = RejectionT()
            lib.ywp_node_rejection(self._handle, index, ctypes.byref(row))
            producer: object = (None, row.producer.decode(), _OTHER)[row.producer_kind]
            out[row.consumer.decode()] = (_version(row.version), producer)
        return out

    # -- effects -------------------------------------------------------------------

    def _effects(self) -> list[Effect]:
        array = ctypes.POINTER(EffectT)()
        count = self._lib.lib.ywp_node_effects(self._handle, ctypes.byref(array))
        effects = [self._convert(array[i]) for i in range(count)]
        assert self.overruns == 0, "effect space exhausted"
        return effects

    @staticmethod
    def _convert(fx: EffectT) -> Effect:
        if fx.kind == FX_SEND:
            body_bytes = ctypes.string_at(fx.send.body, fx.send.body_len)
            return CSendFrame(
                _TYPES[fx.send.type],
                int(fx.send.seq),
                json.loads(body_bytes),
                body_bytes,
            )
        if fx.kind == FX_PERSIST:
            saved = fx.persist.state.contents
            return Persist(
                PersistedState(int(saved.seq), _pairing(saved.pairing)),
                int(fx.persist.request_id),
            )
        if fx.kind == FX_ATTACH:
            media = ctypes.string_at(fx.attach.media, fx.attach.media_len)
            return AttachMedia(fx.attach.producer.decode(), json.loads(media))
        if fx.kind == FX_DETACH:
            return DetachMedia()
        if fx.kind == FX_ADMISSION:
            peers = tuple(
                (
                    fx.admission.peers[i].id.decode(),
                    str(ipaddress.IPv4Address(fx.admission.peers[i].addr)),
                )
                for i in range(fx.admission.count)
            )
            return Admission(peers)
        assert fx.kind == FX_LOG, fx.kind
        return Log(fx.log.text.decode("utf-8"), _LEVELS[fx.log.level])  # type: ignore[arg-type]


NodeFactory = Callable[..., Any]


def factory(lib: Library, use_c: Callable[[NodeConfig], bool]) -> NodeFactory:
    """A simnet node factory: C nodes where ``use_c(config)``, else Python."""
    from yoke.core.node import Node

    def make(config: NodeConfig, *, psk: bytes, **kwargs: Any) -> Any:
        if use_c(config):
            return CNode(lib, config, psk=psk, **kwargs)
        return Node(config, **kwargs)

    return make

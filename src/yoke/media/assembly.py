"""Compose the stages into sender and receiver pipelines from one config.

This is the single place that knows the canonical stage order, so the
runner, the offline integration tests, and the debug CLI all build the
*same* pipeline. Pure construction — no I/O, no clock.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from yoke.media.pipeline import Pipeline, Stage
from yoke.media.stages.codec import (
    Codec,
    CodecDecodeStage,
    CodecEncodeStage,
    PassthroughCodec,
    RatioCodec,
)
from yoke.media.stages.fec import (
    DuplicatePathFec,
    FecDecodeStage,
    FecEncodeStage,
    SeamlessMerger,
    XorParityDecoder,
    XorParityFec,
)
from yoke.media.stages.jitter import JitterBuffer
from yoke.media.stages.packetize import RTP_HEADER_LEN, Depacketizer, Packetizer
from yoke.media.stages.ratecontrol import RateController, RateState
from yoke.media.stages.sink import IntegritySink
from yoke.media.stages.source import SyntheticSource
from yoke.media.types import (
    IPV4_UDP_HEADER_LEN,
    JUMBO_MTU,
    MAX_FEC_GROUP_SIZE,
    MAX_FRAME_BYTES,
    MAX_UDP_BYTES,
    MEDIA_CLOCK_HZ,
    PT_JPEGXS,
    PT_RAW,
    PT_VC2,
    REFERENCE_TRAILER_LEN,
    require_uint,
)

_ENCODING_PT = {"raw": PT_RAW, "jpegxs": PT_JPEGXS, "vc2": PT_VC2, "h265": 100}
_FEC = frozenset({"none", "xor", "dup"})
MIN_PATH_MTU = IPV4_UDP_HEADER_LEN + REFERENCE_TRAILER_LEN + RTP_HEADER_LEN + 1
MAX_PATH_MTU = MAX_UDP_BYTES + IPV4_UDP_HEADER_LEN


@dataclass(frozen=True)
class MediaConfig:
    """Everything needed to build a matching sender and receiver.

    ``fec`` = ``"none"`` | ``"xor"`` (offline sim only — its parity does not
    cross the wire) | ``"dup"`` (ST 2022-7 duplicate path — wire-capable).
    ``mtu`` is the complete IPv4 path packet budget, including fixed
    IPv4/UDP headers and the reference framing trailer. Encoding names
    select reference software models, not interoperable codec payloads.
    """

    width: int = 1920
    height: int = 1080
    fps: int = 60
    encoding: str = "raw"
    pattern: str = "counter"
    mtu: int = JUMBO_MTU
    fec: str = "none"
    fec_group: int = 8
    ssrc: int = 0x5A5A5A5A
    seed: int = 0
    playout_delay: float = 0.02
    n_slices: int = 4

    def __post_init__(self) -> None:
        for name, maximum in (
            ("width", MAX_FRAME_BYTES),
            ("height", MAX_FRAME_BYTES),
            ("fps", MEDIA_CLOCK_HZ),
            ("mtu", MAX_PATH_MTU),
            ("fec_group", MAX_FEC_GROUP_SIZE),
            ("n_slices", 65_535),
        ):
            if require_uint(getattr(self, name), name, maximum) == 0:
                raise ValueError(f"{name} must be positive")
        if self.width * self.height * 3 > MAX_FRAME_BYTES:
            raise ValueError("RGB frame exceeds the 64 MiB reference frame budget")
        if self.mtu < MIN_PATH_MTU:
            raise ValueError(f"path mtu must be in [{MIN_PATH_MTU}, {MAX_PATH_MTU}]")
        require_uint(self.ssrc, "ssrc", 0xFFFF_FFFF)
        require_uint(self.seed, "seed", 0xFFFF_FFFF_FFFF_FFFF)
        if (
            isinstance(self.playout_delay, bool)
            or not isinstance(self.playout_delay, (int, float))
            or not 0 <= self.playout_delay <= 0.2
            or not math.isfinite(self.playout_delay)
        ):
            raise ValueError("playout_delay must be finite and in [0, 0.2]")
        if not isinstance(self.pattern, str) or self.pattern not in {
            "counter",
            "bars",
            "noise",
        }:
            raise ValueError(f"unknown pattern {self.pattern!r}")
        if not isinstance(self.encoding, str) or self.encoding not in _ENCODING_PT:
            raise ValueError(f"unknown encoding {self.encoding!r}")
        if not isinstance(self.fec, str) or self.fec not in _FEC:
            raise ValueError(f"unknown fec {self.fec!r}")

    @property
    def packet_budget(self) -> int:
        """RTP-header + fragment bytes after reserving every outer header."""
        return self.mtu - IPV4_UDP_HEADER_LEN - REFERENCE_TRAILER_LEN

    @property
    def payload_type(self) -> int:
        return _ENCODING_PT[self.encoding]

    @property
    def lossless(self) -> bool:
        return self.encoding == "raw"

    def make_codec(self) -> Codec:
        if self.encoding == "raw":
            return PassthroughCodec(self.n_slices)
        return RatioCodec(self.n_slices)


@dataclass
class Sender:
    source: SyntheticSource
    pipeline: Pipeline
    rate_state: RateState
    config: MediaConfig

    def producer_media(self, group: str, port: int) -> dict[str, object]:
        """Reference-stream metadata; encoding names describe software models."""
        return {
            "transport": "rtp",
            "framing": "yoke-reference-v1",
            "group": group,
            "port": port,
            "pt": self.config.payload_type,
            "encoding": self.config.encoding,
            "fec": self.config.fec,
            "width": self.config.width,
            "height": self.config.height,
            "fps": self.config.fps,
        }


@dataclass
class Receiver:
    pipeline: Pipeline
    sink: IntegritySink
    config: MediaConfig
    extra: dict[str, object] = field(default_factory=dict)


def build_sender(config: MediaConfig) -> Sender:
    """Sender: source (driven via ``.frame``) feeds the pipeline
    ratecontrol → codec-encode → packetize → [fec-encode]."""
    source = SyntheticSource(
        width=config.width,
        height=config.height,
        fps=config.fps,
        pattern=config.pattern,
        seed=config.seed,
    )
    rate_state = RateState()
    stages: list[Stage] = [
        RateController(rate_state),
        CodecEncodeStage(config.make_codec(), state=rate_state),
        Packetizer(
            config.packet_budget, payload_type=config.payload_type, ssrc=config.ssrc
        ),
    ]
    if config.fec == "xor":
        stages.append(FecEncodeStage(XorParityFec(config.fec_group)))
    elif config.fec == "dup":
        stages.append(FecEncodeStage(DuplicatePathFec()))
    return Sender(source, Pipeline(stages), rate_state, config)


def build_receiver(config: MediaConfig, *, report_every: int = 15) -> Receiver:
    """Receiver: [fec-decode] → jitter → depacketize → codec-decode → sink."""
    sink = IntegritySink(report_every=report_every, stat_every=30)
    stages: list[Stage] = []
    if config.fec == "xor":
        stages.append(FecDecodeStage(XorParityDecoder()))
    elif config.fec == "dup":
        stages.append(FecDecodeStage(SeamlessMerger()))
    stages.extend(
        [
            JitterBuffer(playout_delay=config.playout_delay),
            Depacketizer(),
            CodecDecodeStage(config.make_codec()),
            sink,
        ]
    )
    return Receiver(Pipeline(stages), sink, config)

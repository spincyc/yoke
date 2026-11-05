#!/usr/bin/env python3
"""Replay usbmon traces through a USB/IP wire model, per ADR 0028 lever.

Analytic upper bounds from a replay model, software-only; not hardware.
For each trace and lever configuration it reports payload and wire bytes,
the eligible and captured fractions, the tiers that can use the
configuration, the real YX encoder's CPU time on this host (and a labelled
Cortex-A55 inference), and the throughput USB/IP could at best reach over
100 Mbit and 1 Gbit Ethernet. The model is stated in MODEL_TEXT, in the JSON, and in
tools/usbtrace/README.md. Stdlib only, Python >= 3.9.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pcapfmt
import urbs
import yxenc

LABEL = "analytic upper bound; replay model, software-only; not hardware"
DEFAULT_DIR = yxenc.ROOT / ".scratch" / "usbtrace"
MIB = 1 << 20
YX_BLOCK = 4096
YX_BLOCK_HEADER = 4
YX_MIN_LENGTH = 512
NATIVE_MAX_URB = 32768  # native/yusb/linux/worker_main.c policy.max_transfer

# ADR 0027 tiers by device-side exporter. Only a yusb exporter (the export
# core) speaks YX and does read-ahead; tier 2 runs the kernel's stock
# usbip-host, which speaks plain USB/IP only. Write-behind lives in the
# computer-side proxy and needs no peer support (ADR 0028, Consequences).
TIERS = {
    "1": "ESP32-S3, 10/100, yusb firmware; full-speed USB only",
    "1.5": "ESP32-P4, 10/100, yusb firmware",
    "2": "ZERO 3E, GbE, stock usbip-host exporter: plain USB/IP only",
    "native": "future yusb native exporter on Linux, GbE (ADR 0028 follow-up)",
}
# Tier 1's ESP32-S3 has full-speed USB only: bulk tops out near 1 MB/s, so a
# row whose 100M figure exceeds this describes tier 1.5, not tier 1.
FULL_SPEED_MBPS = 1.0
STOCK_TIERS = ("1", "1.5", "2", "native")  # plain USB/IP or proxy-only levers
YUSB_TIERS = ("1", "1.5", "native")  # YX or read-ahead: a yusb exporter

MODEL_TEXT = (
    "Each URB costs a 48-byte USB/IP header in each direction (CMD_SUBMIT, "
    "RET_SUBMIT) plus its payload: OUT data in CMD_SUBMIT, IN data in RET_SUBMIT; "
    "an unlinked URB adds CMD_UNLINK and RET_UNLINK. Bulk transfers are split "
    "into max_urb URBs (default 32 KiB, the native worker's max transfer). With "
    "YX levers, eligible payloads (bulk, >= 512 bytes) are replaced by the block "
    "stream of the real C encoder (one encoder per device and direction, in "
    "trace order); control, interrupt and short bulk payloads stay raw. usbmon "
    "may capture only a prefix of a payload (captured_fraction < 1): uncaptured "
    "eligible bytes are neither encoded nor counted as RAW but scaled by the "
    "wire/data ratio the encoder reached on the captured bytes of the same "
    "device and direction (sent plain when that stream has no captured bytes), "
    "and are reported separately. Each message is cut into 1448-byte TCP "
    "segments; each segment costs "
    "90 more bytes on Ethernet (20 IP + 20 TCP + 12 timestamp option + 14 MAC + "
    "4 FCS + 8 preamble + 12 inter-frame gap) and every second segment draws a "
    "90-byte ACK in the other direction. Link time is wire bits over the link "
    "rate, summing per URB the busier direction (BOT is sequential, so reads and "
    "writes do not overlap; ACKs ride the idle direction). Device time is the "
    "union of the trace's submit-to-complete intervals, but at least bulk bytes "
    "over a 40 MB/s high-speed ceiling. Elapsed time = max(link time, device "
    "time) + "
    "RTT x unhidden round trips. Plain BOT pays one round trip per command "
    "(CBW..CSW) and one per data-out URB (window 1); write-behind removes "
    "the data-out ones; read-ahead hides min(RTT, command device time) for a "
    "READ(10) that exactly continues a good READ(10) (same LUN and length); any "
    "other command after a good READ(10) waits for the discarded speculation, "
    "max(0, that read's device time - RTT). Control transfers pay one round trip "
    "each, other OUT URBs one each, IN pumps none. Isochronous transfers are "
    "unsupported (ADR 0021) and excluded; a trace whose isochronous payload "
    "exceeds the rest is reported unsupported. Host think time is not modelled. "
    "Throughput = payload bytes / elapsed time, in MB/s (10^6 bytes/s). The "
    "results are analytic upper bounds: device, network and gadget are assumed "
    "to overlap perfectly. Rows are labelled with the tiers whose exporter can "
    "use them: YX and read-ahead need a yusb exporter (tiers 1 and 1.5, or a "
    "future native exporter); tier 2's stock usbip-host takes plain USB/IP only, "
    "so its 1G figures are the plain and write-behind rows."
)
A55_TEXT = (
    "inference: host encode time x factor. The default 6 assumes a desktop x86 "
    "host near 5.5 GHz: clock ratio to the 1.8 GHz RK3566 Cortex-A55 (about 3) x "
    "per-clock ratio of a wide out-of-order core to the in-order dual-issue A55 "
    "(about 2). Not measured; run replay.py on the ZERO 3E to measure instead."
)


@dataclass(frozen=True)
class Lever:
    name: str
    yx: int  # YX levers given to the encoder; 0 sends payloads plain
    write_behind: bool = False
    read_ahead: bool = False

    def describe(self) -> list[str]:
        names = [
            n for bit, n in ((1, "FILL"), (2, "LZ4"), (4, "CACHE")) if self.yx & bit
        ]
        names += ["write-behind"] * self.write_behind + ["read-ahead"] * self.read_ahead
        return names or ["none"]

    @property
    def needs_yusb(self) -> bool:
        """YX blocks and read-ahead need the export core on the device side."""
        return bool(self.yx) or self.read_ahead

    def tiers(self) -> tuple[str, ...]:
        return YUSB_TIERS if self.needs_yusb else STOCK_TIERS


_ALL_YX = yxenc.FILL | yxenc.LZ4 | yxenc.CACHE_LEVER
LEVERS = (
    Lever("plain", 0),
    Lever("write-behind", 0, write_behind=True),
    Lever("read-ahead", 0, read_ahead=True),
    Lever("FILL", yxenc.FILL),
    Lever("FILL+LZ4", yxenc.FILL | yxenc.LZ4),
    Lever("FILL+LZ4+CACHE", _ALL_YX),
    Lever("all", _ALL_YX, write_behind=True, read_ahead=True),
)


@dataclass(frozen=True)
class Model:
    links: tuple[tuple[str, float], ...] = (("100M", 100e6), ("1G", 1e9))
    rtts_ms: tuple[float, ...] = (0.2, 1.0)
    usbip_header: int = 48
    mss: int = 1448
    segment_overhead: int = 90
    ack_frame: int = 90
    ack_every: int = 2
    max_urb: int = NATIVE_MAX_URB
    bus_ceiling: float = 40e6  # bytes/s, high-speed bulk
    cache_blocks: int = 256
    a55_factor: float = 6.0
    # Tiers each link stands for: 10/100 boxes, and GbE ones.
    link_tiers: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("100M", ("1", "1.5")),
        ("1G", ("2", "native")),
    )

    def usable_on(self, lever: Lever) -> dict[str, list[str]]:
        """Per link, the tiers that can run ``lever`` at that link rate."""
        tiers = lever.tiers()
        return {
            link: [t for t in on_link if t in tiers]
            for link, on_link in self.link_tiers
        }

    def message(self, size: int) -> tuple[int, int]:
        """(wire bytes, reverse ACK bytes) of one USB/IP message."""
        frames = max(1, -(-size // self.mss))
        return (
            size + frames * self.segment_overhead,
            frames // self.ack_every * self.ack_frame,
        )


@dataclass
class Trace:
    name: str
    path: str
    events: int
    pairing: urbs.Pairing
    transfers: list[urbs.Transfer]  # supported ones (no isochronous)
    unsupported: list[urbs.Transfer]
    commands: list[urbs.BotCommand]


def load_trace(
    path: Path, *, bus: int | None = None, devnum: int | None = None
) -> Trace:
    with path.open("rb") as stream:
        events = [
            e
            for e in pcapfmt.read_events(stream)
            if (bus is None or e.busnum == bus)
            and (devnum is None or e.devnum == devnum)
        ]
    pairing = urbs.pair_events(events)
    supported = [t for t in pairing.transfers if t.xfer != pcapfmt.USB_ISOCHRONOUS]
    unsupported = [t for t in pairing.transfers if t.xfer == pcapfmt.USB_ISOCHRONOUS]
    return Trace(
        name=path.stem,
        path=str(path),
        events=len(events),
        pairing=pairing,
        transfers=supported,
        unsupported=unsupported,
        commands=urbs.bot_commands(supported),
    )


def chunks(transfer: urbs.Transfer, max_urb: int) -> list[tuple[int, bytes]]:
    """(length, captured bytes) per USB/IP URB; bulk splits at ``max_urb``."""
    length = transfer.payload_length
    if transfer.xfer != pcapfmt.USB_BULK or length <= max_urb:
        return [(length, transfer.data)]
    return [
        (min(max_urb, length - start), transfer.data[start : start + max_urb])
        for start in range(0, length, max_urb)
    ]


def eligible(transfer: urbs.Transfer, length: int) -> bool:
    """yusb_yx_eligible(): bulk and at least 512 bytes."""
    return transfer.xfer == pcapfmt.USB_BULK and length >= YX_MIN_LENGTH


def raw_stream_cost(length: int) -> int:
    """Wire bytes of ``length`` bytes sent as RAW blocks."""
    return length + YX_BLOCK_HEADER * -(-length // YX_BLOCK)


Stream = tuple[int, int, bool]  # bus, device address, IN


@dataclass
class Wire:
    to_device: int = 0
    to_host: int = 0
    serial: int = 0  # per URB, the busier direction's bytes
    encoded_bytes: int = 0  # captured bytes given to the encoder
    encode_ns: int = 0
    uncaptured_bytes: int = 0  # eligible bytes usbmon did not capture
    uncaptured_wire_estimate: int = 0  # their scaled payload cost
    unscaled_bytes: int = 0  # uncaptured, no captured bytes to scale by: plain
    stats: dict[str, int] = field(default_factory=dict)


def wire_bytes(
    trace: Trace,
    model: Model,
    lever: Lever,
    library: yxenc.Library | None,
    *,
    verify: bool = True,
) -> Wire:
    """Bytes on the Ethernet in each direction for one lever configuration."""
    wire = Wire()
    encoders: dict[Stream, yxenc.Encoder] = {}
    decoders: dict[Stream, yxenc.Decoder] = {}
    cache = model.cache_blocks if lever.yx & yxenc.CACHE_LEVER else 0
    # Pass 1 encodes the captured bytes; pass 2 scales the uncaptured ones by
    # their stream's ratio and frames every URB. A URB is (length, encoded
    # size of its captured prefix, uncaptured bytes), encoded None = plain.
    urb_plan: list[tuple[urbs.Transfer, list[tuple[int, int | None, int]]]] = []
    seen: dict[Stream, list[int]] = {}  # captured bytes, encoded bytes
    try:
        for transfer in trace.transfers:
            stream = (*transfer.device, transfer.is_in)
            planned: list[tuple[int, int | None, int]] = []
            for length, data in chunks(transfer, model.max_urb):
                if not (
                    lever.yx and library is not None and eligible(transfer, length)
                ):
                    planned.append((length, None, 0))
                    continue
                encoded_size = 0
                if data:
                    if stream not in encoders:
                        encoders[stream] = yxenc.Encoder(library, lever.yx, cache)
                        decoders[stream] = yxenc.Decoder(library, lever.yx, cache)
                    encoded = encoders[stream].encode(data)
                    if verify and decoders[stream].decode(encoded, len(data)) != data:
                        raise AssertionError(f"{trace.name}: YX round trip mismatch")
                    encoded_size = len(encoded)
                    wire.encoded_bytes += len(data)
                    totals = seen.setdefault(stream, [0, 0])
                    totals[0] += len(data)
                    totals[1] += encoded_size
                planned.append((length, encoded_size, length - len(data)))
            urb_plan.append((transfer, planned))
        for transfer, planned in urb_plan:
            stream = (*transfer.device, transfer.is_in)
            for length, coded, missing in planned:
                payload = length
                if coded is not None:
                    payload = coded + _scaled(wire, seen.get(stream), missing)
                out_size = model.usbip_header + (0 if transfer.is_in else payload)
                in_size = model.usbip_header + (payload if transfer.is_in else 0)
                _add_pair(wire, model, out_size, in_size)
            if transfer.outcome == "unlinked":  # CMD_UNLINK and RET_UNLINK per URB
                for _ in planned:
                    _add_pair(wire, model, model.usbip_header, model.usbip_header)
        for encoder in encoders.values():
            wire.encode_ns += encoder.elapsed_ns
            for key, value in encoder.stats().items():
                wire.stats[key] = wire.stats.get(key, 0) + value
    finally:
        for handle in [*encoders.values(), *decoders.values()]:
            handle.close()
    return wire


def _scaled(wire: Wire, totals: list[int] | None, missing: int) -> int:
    """Estimated payload cost of ``missing`` uncaptured bytes of one stream."""
    if not missing:
        return 0
    wire.uncaptured_bytes += missing
    if not totals or not totals[0]:
        wire.unscaled_bytes += missing
        cost = missing
    else:
        cost = -(-missing * totals[1] // totals[0])
    wire.uncaptured_wire_estimate += cost
    return cost


def _add_pair(wire: Wire, model: Model, to_device: int, to_host: int) -> None:
    down, down_ack = model.message(to_device)
    up, up_ack = model.message(to_host)
    wire.to_device += down + up_ack
    wire.to_host += up + down_ack
    wire.serial += max(down + up_ack, up + down_ack)


def union_ns(intervals: Iterable[tuple[int, int]]) -> int:
    total, end = 0, None
    for start, stop in sorted(intervals):
        if end is None or start > end:
            total += stop - start
            end = stop
        elif stop > end:
            total += stop - end
            end = stop
    return total


def bulk_bytes(transfers: Sequence[urbs.Transfer]) -> int:
    return sum(t.payload_length for t in transfers if t.xfer == pcapfmt.USB_BULK)


def device_time_s(trace: Trace, model: Model) -> float:
    busy = union_ns((t.submit_ns, t.complete_ns) for t in trace.transfers) / 1e9
    return max(busy, bulk_bytes(trace.transfers) / model.bus_ceiling)


def command_time_s(command: urbs.BotCommand, model: Model) -> float:
    traced = (command.end_ns - command.start_ns) / 1e9
    return max(traced, (command.data_in + command.data_out) / model.bus_ceiling)


@dataclass
class Stalls:
    round_trips: int = 0
    seconds: float = 0.0  # RTT time net of read-ahead savings and waits
    wasted_device_s: float = 0.0  # discarded speculation (overlaps RTT and wait)
    hits: int = 0
    misses: int = 0


def _continues(previous: urbs.BotCommand, current: urbs.BotCommand) -> bool:
    """The host's command is exactly the speculated READ(10)."""
    before, after = previous.cbw.rw10(), current.cbw.rw10()
    if before is None or after is None or current.cbw.opcode != urbs.READ_10:
        return False
    return (
        current.cbw.lun == previous.cbw.lun
        and after[0] == before[0] + before[1]
        and after[1] == before[1]
    )


def stalls(trace: Trace, model: Model, lever: Lever, rtt_s: float) -> Stalls:
    result = Stalls()
    members = {index for command in trace.commands for index in command.members}
    for index, transfer in enumerate(trace.transfers):
        if index in members:
            continue
        if transfer.xfer == pcapfmt.USB_CONTROL:
            result.round_trips += 1
        elif not transfer.is_in:
            result.round_trips += len(chunks(transfer, model.max_urb))
    result.seconds = result.round_trips * rtt_s
    speculation: dict[tuple[int, int], tuple[urbs.BotCommand, float]] = {}
    for command in trace.commands:
        trips = 1
        if not lever.write_behind:
            trips += sum(
                max(1, -(-n // model.max_urb)) for n in command.data_out_lengths
            )
        result.round_trips += trips
        seconds = trips * rtt_s
        busy = command_time_s(command, model)
        if lever.read_ahead:
            pending = speculation.pop(command.device, None)
            if pending is not None and _continues(pending[0], command):
                result.hits += 1
                seconds -= min(rtt_s, busy)
            elif pending is not None:
                result.misses += 1
                seconds += max(0.0, pending[1] - rtt_s)
                result.wasted_device_s += pending[1]
            if command.cbw.opcode == urbs.READ_10 and command.good:
                speculation[command.device] = (command, busy)
        result.seconds += seconds
    return result


def _ms_per_mib(nanoseconds: int, data_bytes: int) -> float | None:
    if not data_bytes:
        return None
    return round(nanoseconds / 1e6 / (data_bytes / MIB), 3)


def evaluate(
    trace: Trace,
    model: Model,
    levers: Sequence[Lever],
    library: yxenc.Library | None,
    *,
    library_note: str = "",
    verify: bool = True,
) -> dict[str, Any]:
    payload = sum(t.payload_length for t in trace.transfers)
    eligible_bytes = sum(
        length
        for t in trace.transfers
        for length, _data in chunks(t, model.max_urb)
        if eligible(t, length)
    )
    captured = sum(min(len(t.data), t.payload_length) for t in trace.transfers)
    truncated_transfers = sum(len(t.data) < t.payload_length for t in trace.transfers)
    unsupported = sum(t.payload_length for t in trace.unsupported)
    report: dict[str, Any] = {
        "name": trace.name,
        "path": trace.path,
        "events": trace.events,
        "transfers": len(trace.transfers),
        "unmatched_submits": trace.pairing.unmatched_submits,
        "orphan_completions": trace.pairing.orphan_completions,
        "outcomes": _count(t.outcome for t in trace.pairing.transfers),
        "xfer_types": _count(
            pcapfmt.XFER_NAMES[t.xfer] for t in trace.pairing.transfers
        ),
        "bot_commands": len(trace.commands),
        "payload_bytes": payload,
        "eligible_fraction": round(eligible_bytes / payload, 4) if payload else 0.0,
        "captured_fraction": _floor4(captured / payload) if payload else 1.0,
        "captured_bytes": captured,
        "uncaptured_bytes": payload - captured,
        "truncated": captured < payload,
        "truncated_transfers": truncated_transfers,
        "unsupported_transfers": len(trace.unsupported),
        "unsupported_bytes": unsupported,
        "supported": payload > 0 and unsupported <= payload,
        "results": [],
    }
    if trace.unsupported:
        report["note"] = (
            "isochronous transfers are not eligible and unsupported by Yoke "
            "(ADR 0021); they are excluded from the model"
        )
    if report["truncated"]:
        report["truncation_note"] = (
            f"usbmon captured {captured} of {payload} payload bytes in "
            f"{truncated_transfers} transfers; plain rows are exact, YX rows "
            "scale the uncaptured eligible bytes by the captured ratio of their "
            "stream (see uncaptured_* per row); recapture with a larger ring"
        )
    if not report["supported"]:
        return report
    device_s = device_time_s(trace, model)
    report["device_time_s"] = round(device_s, 6)
    plain_wire: int | None = None
    wires: dict[int, Wire] = {}  # by YX levers: latency levers change no bytes
    for lever in levers:
        if lever.yx and library is None:
            report["results"].append(
                {
                    "config": lever.name,
                    "levers": lever.describe(),
                    "tiers": list(lever.tiers()),
                    "usable_on": model.usable_on(lever),
                    "unavailable": library_note,
                }
            )
            continue
        if lever.yx not in wires:
            wires[lever.yx] = wire_bytes(trace, model, lever, library, verify=verify)
        wire = wires[lever.yx]
        total = wire.to_device + wire.to_host
        plain_wire = total if plain_wire is None and not lever.yx else plain_wire
        row: dict[str, Any] = {
            "config": lever.name,
            "levers": lever.describe(),
            "exporter": "yusb" if lever.needs_yusb else "any",
            "tiers": list(lever.tiers()),
            "usable_on": model.usable_on(lever),
            "payload_bytes": payload,
            "wire_bytes": total,
            "wire_bytes_to_device": wire.to_device,
            "wire_bytes_to_host": wire.to_host,
            "ratio": round(payload / total, 4),
            "gain_vs_plain": round(plain_wire / total, 4) if plain_wire else None,
            "eligible_fraction": report["eligible_fraction"],
            "encoded_bytes": wire.encoded_bytes,
            "uncaptured_bytes": wire.uncaptured_bytes,
            "uncaptured_wire_estimate": wire.uncaptured_wire_estimate,
            "uncaptured_unscaled_bytes": wire.unscaled_bytes,
            "encode_ms_per_mib_host": _ms_per_mib(wire.encode_ns, wire.encoded_bytes),
            "encode_ms_per_mib_a55_inference": None,
            "yx_stats": wire.stats or None,
            "throughput_mbps": {},
            "bound": {},
        }
        host_ms = row["encode_ms_per_mib_host"]
        if host_ms is not None:
            row["encode_ms_per_mib_a55_inference"] = round(
                host_ms * model.a55_factor, 3
            )
        for rtt_ms in model.rtts_ms:
            stall = stalls(trace, model, lever, rtt_ms / 1e3)
            key = f"rtt_{rtt_ms:g}ms"
            row.setdefault("round_trips", stall.round_trips)
            if lever.read_ahead:
                row["read_ahead"] = {
                    "hits": stall.hits,
                    "misses": stall.misses,
                    "discarded_device_s": round(stall.wasted_device_s, 6),
                }
            for link, rate in model.links:
                link_s = wire.serial * 8 / rate
                elapsed = max(link_s, device_s) + stall.seconds
                speed = float(f"{payload / elapsed / 1e6:.4g}")
                row["throughput_mbps"].setdefault(link, {})[key] = speed
                row["bound"].setdefault(link, {})[key] = (
                    "link" if link_s >= device_s else "device"
                )
        if any(
            v > FULL_SPEED_MBPS for v in row["throughput_mbps"].get("100M", {}).values()
        ):
            row["tiers"] = [t for t in row["tiers"] if t != "1"]
            row["usable_on"]["100M"] = [t for t in row["usable_on"]["100M"] if t != "1"]
        report["results"].append(row)
    return report


def _floor4(value: float) -> float:
    """Four decimals, rounded down so a truncated trace never shows 1.0."""
    return int(value * 10000) / 10000


def _count(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def run(
    paths: Sequence[Path],
    model: Model,
    *,
    levers: Sequence[Lever] = LEVERS,
    bus: int | None = None,
    devnum: int | None = None,
    use_encoder: bool = True,
    native: Path = yxenc.NATIVE,
    verify: bool = True,
) -> dict[str, Any]:
    library: yxenc.Library | None = None
    encoder: dict[str, Any] = {"available": False}
    if use_encoder:
        try:
            library = yxenc.load(native)
            encoder = {"available": True, "library": str(library.path)}
        except yxenc.EncoderUnavailableError as exc:
            encoder["reason"] = str(exc)
    else:
        encoder["reason"] = "disabled (--no-encoder)"
    note = f"YX encoder unavailable: {encoder.get('reason', '')}"
    traces = [
        evaluate(
            load_trace(path, bus=bus, devnum=devnum),
            model,
            levers,
            library,
            library_note=note,
            verify=verify,
        )
        for path in paths
    ]
    return {
        "label": LABEL,
        "estimate": "analytic upper bound",
        "tiers": TIERS,
        "model": {
            "text": MODEL_TEXT,
            "a55_inference": A55_TEXT,
            "parameters": {
                "links_bps": dict(model.links),
                "rtts_ms": list(model.rtts_ms),
                "usbip_header": model.usbip_header,
                "mss": model.mss,
                "segment_overhead": model.segment_overhead,
                "ack_frame": model.ack_frame,
                "ack_every": model.ack_every,
                "max_urb": model.max_urb,
                "bus_ceiling_bytes_per_s": model.bus_ceiling,
                "cache_blocks": model.cache_blocks,
                "a55_factor": model.a55_factor,
                "link_tiers": {k: list(v) for k, v in model.link_tiers},
            },
            "filters": {"bus": bus, "devnum": devnum},
        },
        "host": {
            "machine": platform.machine(),
            "processor": platform.processor() or platform.machine(),
            "python": platform.python_version(),
        },
        "encoder": encoder,
        "traces": traces,
    }


FUTURE_ONLY = "*"


def markdown(result: dict[str, Any]) -> str:
    model = result["model"]["parameters"]
    links = list(model["links_bps"])
    rtts = [f"rtt_{r:g}ms" for r in model["rtts_ms"]]
    heads = [f"{link} {key[4:]}" for link in links for key in rtts]
    link_tiers: dict[str, list[str]] = model.get("link_tiers", {})
    lines = [
        f"Results: {result['label']}. Analytic upper bounds: throughput in MB/s "
        f"over each link and RTT, assuming device, network and gadget overlap; "
        f"A55 column is an inference (x{model['a55_factor']:g}); "
        f"URBs split at {model['max_urb']} bytes.",
        "",
        "Tiers: which exporters can use the row. Tier 1 is capped near "
        f"{FULL_SPEED_MBPS:g} MB/s by full-speed USB, so it is dropped from rows "
        "whose 100M figures exceed that; there the 100M columns describe tier 1.5. "
        + "; ".join(f"{k} = {v}" for k, v in result.get("tiers", TIERS).items())
        + f". {FUTURE_ONLY} marks a figure no tier on that link can use "
        "today (YX or read-ahead on GbE needs the future native exporter, "
        "not tier 2's stock usbip-host).",
        "",
        "| Trace | Config | Tiers | Payload MiB | Wire MiB | Ratio | Eligible "
        "| Captured | Enc ms/MiB | A55 ms/MiB | " + " | ".join(heads) + " |",
        "|---" * (10 + len(heads)) + "|",
    ]
    notes: list[str] = []
    for trace in result["traces"]:
        captured = _captured_cell(trace)
        if trace.get("truncated"):
            notes.append(
                f"- {trace['name']}: TRUNCATED capture, captured_fraction "
                f"{trace['captured_fraction']:.4f}; {trace['truncation_note']}."
            )
        if not trace["supported"]:
            cells = [
                "not eligible / unsupported (isochronous, ADR 0021)"
                if trace["unsupported_transfers"]
                else "no paired payload (nothing to replay)"
            ]
            cells += [""] * (9 + len(heads) - 1)
            lines.append(f"| {trace['name']} | " + " | ".join(cells) + " |")
            continue
        for row in trace["results"]:
            tiers = ", ".join(row["tiers"])
            if "unavailable" in row:
                cells = [tiers, "encoder unavailable"]
                cells += [""] * (7 + len(heads) - 1)
                lines.append(
                    f"| {trace['name']} | {row['config']} | " + " | ".join(cells) + " |"
                )
                continue
            speeds = [
                _speed(row["throughput_mbps"][link][key])
                + (
                    FUTURE_ONLY
                    if link in link_tiers and not _live(row["usable_on"][link])
                    else ""
                )
                for link in links
                for key in rtts
            ]
            cells = [
                trace["name"],
                row["config"],
                tiers,
                f"{row['payload_bytes'] / MIB:.2f}",
                f"{row['wire_bytes'] / MIB:.2f}",
                f"{row['ratio']:.2f}",
                f"{row['eligible_fraction']:.2f}",
                captured,
                _cell(row["encode_ms_per_mib_host"]),
                _cell(row["encode_ms_per_mib_a55_inference"]),
                *speeds,
            ]
            lines.append("| " + " | ".join(cells) + " |")
    if notes:
        lines += ["", "Truncated captures:", *notes]
    return "\n".join(lines) + "\n"


def _live(tiers: Sequence[str]) -> bool:
    """A tier that exists today can use the figure (not only a future one)."""
    return any(t != "native" for t in tiers)


def _captured_cell(trace: dict[str, Any]) -> str:
    fraction = trace.get("captured_fraction", 1.0)
    return f"{fraction:.2f} TRUNCATED" if trace.get("truncated") else f"{fraction:.2f}"


def _cell(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def _speed(value: float) -> str:
    return f"{value:.1f}" if value >= 1 else f"{value:.2g}"


def find_traces(paths: Sequence[Path]) -> list[Path]:
    found: list[Path] = []
    for path in paths or [DEFAULT_DIR]:
        found += sorted(path.glob("*.pcap")) if path.is_dir() else [path]
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"USB/IP lever replay ({LABEL}).")
    parser.add_argument(
        "traces", nargs="*", type=Path, help=f"pcaps or dirs ({DEFAULT_DIR})"
    )
    parser.add_argument("--bus", type=int)
    parser.add_argument("--devnum", type=int)
    parser.add_argument("--rtt-ms", type=float, action="append", help="repeatable")
    parser.add_argument("--cache-blocks", type=int, default=Model.cache_blocks)
    parser.add_argument(
        "--max-urb",
        type=int,
        default=Model.max_urb,
        help="bulk URB split, bytes (default: the native worker's 32768)",
    )
    parser.add_argument("--a55-factor", type=float, default=Model.a55_factor)
    parser.add_argument(
        "--bus-ceiling-mbps", type=float, default=Model.bus_ceiling / 1e6
    )
    parser.add_argument("--config", action="append", choices=[lv.name for lv in LEVERS])
    parser.add_argument("--native", type=Path, default=yxenc.NATIVE)
    parser.add_argument("--no-encoder", action="store_true")
    parser.add_argument(
        "--no-verify", action="store_true", help="skip YX decode checks"
    )
    parser.add_argument(
        "--markdown", action="store_true", help="print a table, not JSON"
    )
    parser.add_argument("--json-out", type=Path, help="also write the JSON here")
    args = parser.parse_args(argv)
    if args.max_urb < YX_MIN_LENGTH:
        parser.error(f"--max-urb must be at least {YX_MIN_LENGTH}")
    paths = find_traces(args.traces)
    if not paths:
        print("no traces found; run tools/usbtrace/synth.py first", file=sys.stderr)
        return 2
    model = Model(
        rtts_ms=tuple(args.rtt_ms or Model.rtts_ms),
        cache_blocks=args.cache_blocks,
        max_urb=args.max_urb,
        a55_factor=args.a55_factor,
        bus_ceiling=args.bus_ceiling_mbps * 1e6,
    )
    levers = [lv for lv in LEVERS if not args.config or lv.name in args.config]
    result = run(
        paths,
        model,
        levers=levers,
        bus=args.bus,
        devnum=args.devnum,
        use_encoder=not args.no_encoder,
        native=args.native,
        verify=not args.no_verify,
    )
    text = json.dumps(result, indent=2)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(text + "\n")
    print(markdown(result) if args.markdown else text)
    if not result["encoder"]["available"]:
        print(f"note: {result['encoder'].get('reason')}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

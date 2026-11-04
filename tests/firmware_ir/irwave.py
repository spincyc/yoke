"""IR protocol timing, Pronto conversion and waveform analysis for host tests.

Protocol timings are the published nominal values (NEC, Philips RC5, Sony
SIRC 12-bit). Everything here is test-side reference code; nothing is
imported by firmware. All waveform results come from the HOST SIMULATION
of the RMT channel, never from hardware.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from itertools import pairwise

GROUP_HZ = 80_000_000
PRONTO_UNIT_US = 0.241246


def to_cycles(durations_us: Iterable[float], carrier_hz: int) -> list[int]:
    return [max(1, round(us * carrier_hz / 1e6)) for us in durations_us]


def nec_us(address: int, command: int) -> tuple[list[float], list[float]]:
    """One NEC frame (108 ms period) and its repeat code (108 ms period)."""
    bits = [
        (value >> i) & 1
        for value in (address, address ^ 0xFF, command, command ^ 0xFF)
        for i in range(8)
    ]
    frame = [9000.0, 4500.0]
    for bit in bits:
        frame += [562.5, 1687.5 if bit else 562.5]
    frame += [562.5]
    frame.append(108_000.0 - sum(frame))
    repeat = [9000.0, 2250.0, 562.5]
    repeat.append(108_000.0 - sum(repeat))
    return frame, repeat


def rc5_us(address: int, command: int, toggle: int) -> list[float]:
    """Philips RC5: Manchester, 889 us halves, 113.778 ms frame period."""
    bits = [1, 1, toggle]
    bits += [(address >> i) & 1 for i in range(4, -1, -1)]
    bits += [(command >> i) & 1 for i in range(5, -1, -1)]
    halves = []
    for bit in bits:  # 1 = space then mark, 0 = mark then space
        halves += [0, 1] if bit else [1, 0]
    while halves[0] == 0:
        halves.pop(0)
    runs: list[list[float]] = []
    for level in halves:
        if runs and runs[-1][0] == level:
            runs[-1][1] += 889.0
        else:
            runs.append([level, 889.0])
    if runs[-1][0] == 1:
        runs.append([0, 0.0])
    durations = [run[1] for run in runs]
    durations[-1] += 113_778.0 - sum(durations)
    return durations


def sirc_us(address: int, command: int) -> list[float]:
    """Sony SIRC 12-bit, 45 ms frame period."""
    bits = [(command >> i) & 1 for i in range(7)] + [
        (address >> i) & 1 for i in range(5)
    ]
    durations = [2400.0, 600.0]
    for bit in bits:
        durations += [1200.0 if bit else 600.0, 600.0]
    durations[-1] += 45_000.0 - sum(durations)
    return durations


def pronto_from_us(durations_us: list[float], carrier_hz: int) -> str:
    """A learned-format Pronto hex code (once sequence only)."""
    word = round(1e6 / (carrier_hz * PRONTO_UNIT_US))
    period_us = word * PRONTO_UNIT_US
    bursts = [max(1, round(us / period_us)) for us in durations_us]
    words = [0, word, len(bursts) // 2, 0, *bursts]
    return " ".join(f"{value:04X}" for value in words)


def pronto_to_sendir(pronto: str, connector: int, ident: int) -> tuple[str, int]:
    """Global Cache style conversion: frequency from the Pronto word, burst
    counts copied as carrier cycles; the repeat sequence (if any) becomes the
    offset target."""
    words = [int(value, 16) for value in pronto.split()]
    if words[0] != 0:
        raise ValueError("only learned (0000) Pronto codes")
    frequency = round(1e6 / (words[1] * PRONTO_UNIT_US))
    once, repeat = words[2], words[3]
    bursts = words[4 : 4 + 2 * (once + repeat)]
    offset = 2 * once + 1 if repeat else 1
    pulses = ",".join(str(value) for value in bursts)
    return f"sendir,1:{connector},{ident},{frequency},1,{offset},{pulses}", frequency


def sendir(
    connector: int,
    ident: int,
    carrier_hz: int,
    repeat: int,
    offset: int,
    cycles: list[int],
) -> str:
    pulses = ",".join(str(value) for value in cycles)
    return f"sendir,1:{connector},{ident},{carrier_hz},{repeat},{offset},{pulses}"


@dataclass(frozen=True)
class Segment:
    level: int
    start: int  # ticks (us)
    ticks: int


def segments(halves: list[list[int]], start: int, end: int) -> list[Segment]:
    """Merge same-level RMT halves inside [start, end) into envelope runs."""
    runs: list[Segment] = []
    for begin, level, ticks in halves:
        if begin < start or begin >= end:
            continue
        if (
            runs
            and runs[-1].level == level
            and runs[-1].start + runs[-1].ticks == begin
        ):
            last = runs[-1]
            runs[-1] = Segment(level, last.start, last.ticks + ticks)
        else:
            if runs:
                assert runs[-1].start + runs[-1].ticks == begin, "gap in RMT stream"
            runs.append(Segment(level, begin, ticks))
    return runs


def ideal_edges_us(cycles: list[int], carrier_hz: int) -> list[float]:
    edges, total = [], 0
    for value in cycles:
        total += value
        edges.append(total * 1e6 / carrier_hz)
    return edges


def carrier_actual(total_ticks: int) -> float:
    return GROUP_HZ / total_ticks


def within(value: float, nominal: float, tolerance: float) -> bool:
    return abs(value - nominal) <= nominal * tolerance


def decode_nec(durations: list[int]) -> tuple[int, int]:
    if not (within(durations[0], 9000, 0.1) and within(durations[1], 4500, 0.1)):
        raise ValueError("no NEC header")
    bits = []
    for i in range(32):
        mark, space = durations[2 + 2 * i], durations[3 + 2 * i]
        if not within(mark, 562.5, 0.25):
            raise ValueError("bad NEC mark")
        if within(space, 1687.5, 0.25):
            bits.append(1)
        elif within(space, 562.5, 0.25):
            bits.append(0)
        else:
            raise ValueError("bad NEC space")
    value = sum(bit << i for i, bit in enumerate(bits))
    address, inverse, command, check = (value >> s & 0xFF for s in (0, 8, 16, 24))
    if address ^ inverse != 0xFF or command ^ check != 0xFF:
        raise ValueError("NEC check failed")
    return address, command


def decode_rc5(durations: list[int]) -> tuple[int, int, int]:
    halves = [0]  # leading space of the first start bit
    for index, ticks in enumerate(durations[:-1]):
        count = round(ticks / 889)
        if count not in (1, 2) or not within(ticks, 889 * count, 0.2):
            raise ValueError("bad RC5 timing")
        halves += [1 - index % 2] * count
    if len(halves) % 2:
        halves.append(0)
    bits = []
    for first, second in zip(halves[::2], halves[1::2], strict=True):
        if (first, second) == (0, 1):
            bits.append(1)
        elif (first, second) == (1, 0):
            bits.append(0)
        else:
            raise ValueError("RC5 Manchester violation")
    if len(bits) != 14 or bits[:2] != [1, 1]:
        raise ValueError("RC5 start bits")
    address = int("".join(map(str, bits[3:8])), 2)
    command = int("".join(map(str, bits[8:14])), 2)
    return bits[2], address, command


def decode_sirc(durations: list[int]) -> tuple[int, int]:
    if not within(durations[0], 2400, 0.1):
        raise ValueError("no SIRC header")
    bits = []
    for i in range(12):
        mark = durations[2 + 2 * i]
        if within(mark, 1200, 0.2):
            bits.append(1)
        elif within(mark, 600, 0.25):
            bits.append(0)
        else:
            raise ValueError("bad SIRC mark")
    command = sum(bit << i for i, bit in enumerate(bits[:7]))
    address = sum(bit << i for i, bit in enumerate(bits[7:]))
    return address, command


def raw_capture(
    runs: list[Segment], total: int, high: int, resolution_hz: int = 1_000_000
) -> list[tuple[int, int]]:
    """What a wideband learning sensor sees: each mark expanded into carrier
    pulses (group-clock exact, quantized to the RX resolution)."""
    period = total / GROUP_HZ * resolution_hz
    on = high / GROUP_HZ * resolution_hz
    edges: list[tuple[int, int]] = []  # (time, level) transitions
    for run in runs:
        if run.level == 0:
            continue
        pulse = 0
        while pulse * period < run.ticks - 1e-9:
            begin = run.start + pulse * period
            edges.append((round(begin), 1))
            edges.append((round(min(begin + on, run.start + run.ticks)), 0))
            pulse += 1
    out: list[tuple[int, int]] = []
    for (time, level), (following, _) in pairwise(edges):
        if following > time:
            out.append((level, following - time))
    return out

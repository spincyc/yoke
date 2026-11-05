"""Python side of the native frame-layer differential (tests/native_ywp).

``FrameTool`` batches requests to the sanitized ``frame_check`` driver;
``reference`` computes the same observation with the Python implementation
(``yoke.core.frames``), so a test is a list comparison.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import struct
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from yoke.core.frames import FrameDrop, canonical_bytes, decode

Observation = tuple[str, ...]


def bits(value: float) -> str:
    return f"{struct.unpack('<Q', struct.pack('<d', value))[0]:016x}"


def hexed(data: bytes) -> str:
    return data.hex() or "-"


def unhexed(text: str) -> bytes:
    return b"" if text == "-" else bytes.fromhex(text)


def reference(raw: bytes, psk: bytes) -> Observation:
    """What the Python reference observes for one datagram."""
    try:
        frame = decode(raw, psk)
    except FrameDrop as drop:
        return ("drop", drop.reason.value)
    whole = canonical_bytes(json.loads(raw.decode("utf-8")))
    fingerprint = hmac.new(psk, whole, hashlib.sha256).digest()
    return (
        "ok",
        frame.type.value,
        frame.origin,
        str(frame.seq),
        bits(frame.ts),
        hexed(canonical_bytes(frame.body)),
        hexed(fingerprint),
    )


@dataclass
class FrameTool:
    binary: Path

    def run(self, requests: Sequence[str]) -> list[str]:
        if not requests:
            return []
        result = subprocess.run(
            [str(self.binary)],
            input="\n".join(requests) + "\n",
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )
        if result.returncode != 0:
            raise AssertionError(f"frame_check failed:\n{result.stderr[-4000:]}")
        lines = result.stdout.splitlines()
        assert len(lines) == len(requests), result.stderr[-4000:]
        return lines

    def decode(self, items: Iterable[tuple[bytes, bytes]]) -> list[Observation]:
        """Decode (psk, datagram) pairs; returns one observation each."""
        requests: list[str] = []
        current: bytes | None = None
        kinds: list[bool] = []
        for psk, raw in items:
            if psk != current:
                requests.append(f"key {hexed(psk)}")
                kinds.append(False)
                current = psk
            requests.append(f"dec {hexed(raw)}")
            kinds.append(True)
        return [
            tuple(line.split(" "))
            for line, keep in zip(self.run(requests), kinds, strict=True)
            if keep
        ]

    def encode(
        self,
        psk: bytes,
        items: Iterable[tuple[str, str, int, float, bytes]],
    ) -> list[bytes | None]:
        requests = [f"key {hexed(psk)}"]
        for frame_type, origin, seq, ts, body in items:
            requests.append(f"enc {frame_type} {origin} {seq} {bits(ts)} {hexed(body)}")
        out: list[bytes | None] = []
        for line in self.run(requests)[1:]:
            out.append(unhexed(line[3:]) if line.startswith("ok ") else None)
        return out

    def canonical(self, texts: Iterable[bytes]) -> list[bytes | None]:
        lines = self.run([f"can {hexed(text)}" for text in texts])
        return [unhexed(line[3:]) if line.startswith("ok ") else None for line in lines]

    def overhead(self, origin: str) -> int:
        line = self.run([f"ovh {origin}"])[0]
        return int(line.split()[1])

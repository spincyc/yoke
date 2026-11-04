"""Thermite's iTach stand-in transcripts, replayed against the Yoke endpoint.

The fixture is copied data (see its provenance field); Thermite is never
imported. Yoke answers identity honestly and uses the iTach specification's
CR terminator and ERR_ formats, so those differences are asserted as such.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from conftest import Endpoint

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/thermite_itach.json").read_text()
)


def finish(endpoint: Endpoint) -> None:
    for _ in range(200):
        if endpoint.counters()["idle"]:
            return
        endpoint.advance(50_000)
    raise AssertionError("transmission did not complete")


@pytest.mark.parametrize(
    "transcript", FIXTURE["transcripts"], ids=lambda item: str(item["name"])
)
def test_thermite_transcript(endpoint: Endpoint, transcript: dict[str, Any]) -> None:
    client = endpoint.connect()
    client.send(transcript["request"])
    finish(endpoint)
    response = client.line()
    expect = transcript["expect"]
    if expect == "identity":
        # One non-error line, like the stand-in, but never an iTach version.
        assert response.startswith("Make=Yoke;")
        assert response != transcript["thermite"]
    elif expect == "error":
        assert response.startswith("ERR_")
        assert transcript["thermite"].startswith("ERR_")
    else:
        assert response == expect == transcript["thermite"]
    assert client.pending() == b""  # exactly one response line, CR-terminated


def test_every_thermite_valid_profile_code_is_accepted(endpoint: Endpoint) -> None:
    rules = FIXTURE["profile_rules"]
    codes = list(FIXTURE["boundary_codes"])
    codes.append(FIXTURE["pairs_260"].format(pairs=",".join(["1"] * 520)))
    client = endpoint.connect()
    for counter, code in enumerate(codes):
        numbers = [int(value) for value in code.split(",")]
        frequency, repeats, offset, *pulses = numbers
        low, high = rules["frequency_hz"]
        assert low <= frequency <= high and 1 <= repeats <= rules["repeats"][1]
        assert offset % 2 == 1 and offset <= len(pulses) and len(pulses) <= 520
        assert sum(pulses) * repeats <= 2 * frequency  # Thermite's own bound
        connector = counter % 3 + 1
        packet = rules["packet"].format(connector=connector, counter=counter, code=code)
        client.send_raw(packet.encode())
        finish(endpoint)
        assert client.line() == f"completeir,1:{connector},{counter}"


def test_binding_session_shape(endpoint: Endpoint) -> None:
    """The binding's session: connect, probe, keys, periodic probes."""
    client = endpoint.connect()
    for counter in range(1, 4):
        client.send("getversion")
        assert client.line().startswith("Make=Yoke;")
        client.send(f"sendir,1:1,{counter},38000,1,1,24,24")
        finish(endpoint)
        assert client.line() == f"completeir,1:1,{counter}"
    assert endpoint.counters()["completeir"] == 3

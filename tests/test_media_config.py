"""Reject invalid media configuration before building or allocating stages."""

from __future__ import annotations

import pytest

from yoke.media.assembly import (
    MAX_PATH_MTU,
    MIN_PATH_MTU,
    MediaConfig,
    build_receiver,
    build_sender,
)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"width": 0},
        {"width": True},
        {"height": -1},
        {"width": 100_000, "height": 100_000},
        {"fps": 0},
        {"fps": 90_001},
        {"fps": 60.5},
        {"fps": "60"},
        {"mtu": 12},
        {"mtu": MIN_PATH_MTU - 1},
        {"mtu": MAX_PATH_MTU + 1},
        {"n_slices": 0},
        {"n_slices": 65_536},
        {"fec_group": 0},
        {"fec_group": 1025},
        {"ssrc": -1},
        {"ssrc": 1 << 32},
        {"seed": -1},
        {"playout_delay": float("nan")},
        {"playout_delay": float("inf")},
        {"playout_delay": -0.01},
        {"playout_delay": 0.201},
        {"playout_delay": True},
        {"playout_delay": 10**400},
        {"encoding": "invalid"},
        {"pattern": "invalid"},
        {"fec": "invalid"},
    ],
)
def test_config_rejects_invalid_fields_early(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        MediaConfig(**kwargs)


def test_config_keeps_realistic_4k120_and_wire_boundary() -> None:
    config = MediaConfig(
        width=3840,
        height=2160,
        fps=120,
        n_slices=2160,
        mtu=MAX_PATH_MTU,
        pattern="bars",
        fec_group=1024,
    )
    assert build_sender(config).config == config
    assert build_receiver(config).config == config


def test_path_mtu_boundary_reserves_all_reference_and_network_headers() -> None:
    assert MediaConfig(mtu=MIN_PATH_MTU).packet_budget == 13
    assert MediaConfig(mtu=1500).packet_budget == 1415
    assert MediaConfig(mtu=9000).packet_budget == 8915
    assert MediaConfig(mtu=MAX_PATH_MTU).packet_budget == 65450


@pytest.mark.parametrize("encoding", ["raw", "jpegxs", "vc2"])
def test_reference_producer_metadata_explicitly_identifies_private_framing(
    encoding: str,
) -> None:
    sender = build_sender(MediaConfig(encoding=encoding))
    media = sender.producer_media("239.255.42.42", 5004)
    assert media["transport"] == "rtp"
    assert media["framing"] == "yoke-reference-v1"
    assert media["encoding"] == encoding  # model label, qualified by framing

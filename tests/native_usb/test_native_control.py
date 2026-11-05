"""ADR 0022 control schema in C: differential tests against control.py.

``control_cli.c`` drives ``native/yusb/linux/control.c`` and
``native/yusb/src/target.c`` (built with ASan/UBSan). ``control.py`` on this
interpreter is the oracle: every request is judged by both
``control.parse_request(control.decode(raw))`` and the C parser, and the
decisions must agree at the decision level: whatever C accepts, Python
accepts with the same route; whatever Python accepts, C accepts too unless
the request falls under one of the strictness rules documented in
control.c (only ASCII, schema the integer 1, no duplicate keys, no scoped
IPv6), each recognized here independently of the C code. Error texts are
categories and are not compared. Status and error replies must equal
``control.encode`` byte for byte except the ``updated`` number and the
detail cut. Host software evidence only.
"""

from __future__ import annotations

import codecs
import contextlib
import dataclasses
import fcntl
import functools
import json
import math
import os
import random
import re
import resource
import shutil
import signal
import socket
import struct
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from tests.usbsim import native
from tests.usbsim.classes import MassStorageHost
from tests.usbsim.devices import Loopback

from yoke.usb import ch9, control

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SOURCES = [
    ROOT / "native/yusb/src/target.c",
    ROOT / "native/yusb/linux/control.c",
    HERE / "control_cli.c",
]
Runner = Callable[[str, list[str]], list[str]]


@pytest.fixture(scope="module")
def cli_exe(native_build: Callable[..., Path]) -> Path:
    return native_build("control_cli", SOURCES)


@pytest.fixture(scope="module")
def cli(cli_exe: Path) -> Runner:
    def run(mode: str, lines: list[str]) -> list[str]:
        result = subprocess.run(
            [str(cli_exe), mode],
            input="".join(f"{line}\n" for line in lines),
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr[-3000:]
        out = result.stdout.splitlines()
        assert len(out) == len(lines)
        return out

    return run


def hx(data: bytes | None) -> str:
    return "!" if data is None else "x" + data.hex()


# --- requests -----------------------------------------------------------------


def oracle(raw: bytes) -> str:
    try:
        route = control.parse_request(control.decode(raw))
    except control.ControlError as exc:
        return f"E {exc}"
    if route is None:
        return "S"
    if route.target is None:
        return f"R {route.generation} -"
    t = route.target
    return f"R {route.generation} {t.host} {t.port} {t.busid} {t.source or '-'}"


CATEGORIES = {
    "control message is not one bounded line",
    "control message is not a well-formed request",
    "unknown request schema",
    "unknown control request",
    "route generation must be a positive integer",
    "target needs exactly host, port, busid and source",
    "bad host",
    "bad port",
    "bad busid",
    "bad source IPv4 address",
}


def c_result(line: str) -> str:
    """ "E" for a refusal (its category checked), else the route as oracle()."""
    if line.startswith("E "):
        _, number, text = line.split(" ")
        assert int(number) > 0 and bytes.fromhex(text[1:]).decode() in CATEGORIES
        return "E"
    return line


def strictness(raw: bytes, accepted: str) -> str | None:
    """Which documented control.c strictness refuses a request Python takes."""
    if accepted.startswith("R ") and "%" in accepted.split()[2]:
        return "scoped"  # target.c: scoped IPv6 hosts are refused
    if not raw.isascii() or json.detect_encoding(raw) != "utf-8":
        return "encoding"  # a UTF-8 BOM, or UTF-16/32 (json.detect_encoding)
    duplicates = False

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        nonlocal duplicates
        duplicates |= len({k for k, _ in items}) != len(items)
        return dict(items)

    value = json.loads(raw, object_pairs_hook=pairs)
    if duplicates:
        return "duplicate"
    schema = value["schema"]
    if type(schema) is not int or schema != 1:
        return "schema"  # true, 1.0, 1e0, ... compare equal to 1 in Python
    return None


@dataclass
class Tally:
    agree: int = 0
    accepted: int = 0
    strict: dict[str, int] = dataclasses.field(default_factory=dict)


def check_requests(cli: Runner, raws: list[bytes]) -> Tally:
    tally = Tally()
    outputs = cli("parse", [hx(raw) for raw in raws])
    for raw, line in zip(raws, outputs, strict=True):
        expected, got = oracle(raw), c_result(line)
        context = f"request {raw[:300]!r}: python {expected[:120]!r}, C {got!r}"
        if got != "E":
            assert got == expected, context  # C never accepts more, nor differently
            tally.accepted += 1
            tally.agree += 1
        elif expected.startswith("E "):
            tally.agree += 1
        else:
            reason = strictness(raw, expected)
            assert reason is not None, context
            tally.strict[reason] = tally.strict.get(reason, 0) + 1
    return tally


TARGET = b'{"host":"box.example","port":3240,"busid":"1-1","source":"10.0.0.2"}'


def route(
    gen: bytes = b"1",
    target: bytes = TARGET,
    schema: bytes = b"1",
    op: bytes = b'"route"',
) -> bytes:
    return b'{"schema":%s,"op":%s,"generation":%s,"target":%s}\n' % (
        schema,
        op,
        gen,
        target,
    )


def tgt(
    host: bytes = b'"h"',
    port: bytes = b"1",
    busid: bytes = b'"1-1"',
    source: bytes = b"null",
) -> bytes:
    return b'{"host":%s,"port":%s,"busid":%s,"source":%s}' % (
        host,
        port,
        busid,
        source,
    )


def status(schema: bytes = b"1", op: bytes = b'"status"', extra: bytes = b"") -> bytes:
    return b'{"schema":%s,"op":%s%s}\n' % (schema, op, extra)


SCHEMAS = [
    b"1", b"true", b"1.0", b"1e0", b"10e-1", b"0.1E+1", b"100e-2",
    b"1.0000000000000001", b"0.99999999999999995", b"0.9999999999999999",
    b"1.00000000000000011102230246251565404236316680908203125",
    b"1.00000000000000011102230246251565404236316680908203126",
    b"0.999999999999999944488848768742172978818416595458984375",
    b"0.999999999999999944488848768742172978818416595458984374",
    b"0.00000000000000000001e20", b"1" + b"0" * 400 + b"e-400",
    b"2", b"0", b"-1", b"-1.0", b"false", b"null", b'"1"', b"[1]", b"{}",
    b"NaN", b"Infinity", b"-Infinity", b"1e400", b"-0", b"1.5", b"1e-400",
    b"1e99999999999999999999", b"11e-1", b"0.1",
]  # fmt: skip
HOSTS = [
    b'"BOX.Example."', b'"a."', b'"a..b"', b'"-a"', b'"a-"', b'"a.-b"', b'"a-.b"',
    b'"' + b"a" * 63 + b'"', b'"' + b"a" * 64 + b'"', b'"' + b"a." * 126 + b'a"',
    b'"' + b"a." * 126 + b'ab"', b'"' + b"a." * 126 + b'a."', b'"1.2.3.4"',
    b'"01.2.3.4"', b'"1.2.3.256"', b'"::1"', b'"::FFFF:1.2.3.4"', b'"1::"',
    b'"fe80::1%eth0"', b'"fe80::1%"', b'""', b'" "', b'"a b"', b'"\xc3\xa9.com"',
    b'"\\u0041.com"', b'"a_b"', b"5", b"null", b"true", b"[]", b'{"a":1}', b"1.5",
    b'"\\ud800"', b'"a\\u0000"', b'"\\u00e9\\u00a0\\u0378\\ud83d\\ude00"',
    b'"it\'s"', b'"it\'s \\"x\\""', b'"\\\\"', b'"\\t\\n\\r\\b\\f\\u007f"',
    b'"0:0:0:0:0:0:0:0"', b'"1:2:3:4:5:6:7:8:9"', b'"::1.2.3.4"', b'"::ffff:0:0"',
    b'{"a":1,"b":[1,2.0,"x\'y",1e400,-0,1.5e-7,1e16,123456789012345678]}',
    b'{"k":1,"k":2,"\\u006b":3}', b'[1e22, 1e-5, 0.0001, -2.5e-300, 5e-324]',
]  # fmt: skip
PORTS = [
    b"0", b"1", b"65535", b"65536", b"-1", b"-0", b"80.0", b"8e1", b"true", b'"80"',
    b"null", b"1e400", b"NaN", b"100000000000000000000", b"[80]",
]  # fmt: skip
BUSIDS = [
    b'"1-1"', b'"1-1.2.3"', b'"01-001"', b'"1-"', b'"-1"', b'"1-1."', b'"1--1"',
    b'"1-1..2"', b'"1-' + b"1" * 29 + b'"', b'"1-' + b"1" * 30 + b'"',
    b'"\xd9\xa1-\xd9\xa1"', b"5", b"null", b'"1-1\\u0000"', b'"1-1 "',
]  # fmt: skip
SOURCES_V4 = [
    b"null", b'"10.0.0.1"', b'"010.0.0.1"', b'"10.0.0"', b'"::1"', b'""', b"5",
    b'"10.0.0.1 "', b'"255.255.255.255"', b'"256.0.0.0"', b'"1.2.3.4/32"',
]  # fmt: skip
GENERATIONS = [
    b"1", b"9223372036854775807", b"9223372036854775808", b"0", b"-0", b"-1",
    b"1.0", b"1e2", b"true", b"false", b"null", b'"1"', b"18446744073709551616",
    b"1" + b"0" * 30, b"1" * 4300, b"[1]",
]  # fmt: skip
SYNTAX = [
    b"", b"\n", b" \n", b"  \t\r\n", b"[]\n", b'"x"\n', b"1\n", b"null\n", b"{}\n",
    b'{"schema":1,"op":"status"}', b'{"schema":1,"op":"status"}\r\n',
    b'{"schema":1,"op":"status"}\n\n', b'{"schema":1,"op":"status"}\n ',
    b'{"schema":1,"op":"status",}\n', b'{"schema":1 "op":"status"}\n',
    b'{"schema" 1}\n', b"{'schema':1}\n", b'{"schema":1,"op":"status\n',
    b'{"schema":1,"op":"st\\xtus"}\n', b'{"schema":1,"op":"st\\u12"}\n',
    b'{"schema":1,"op":"st\\ud800\\u12xx"}\n', b'{"schema":1,"op":"s\x01"}\n',
    b'{"schema":1,"op":"s\tt"}\n', b'{"schema":1}/*c*/\n', b'{"a":nul}\n',
    b'{"a":tru}\n', b'{"a":-}\n', b'{"a":-x}\n', b'{"a":1.}\n', b'{"a":1.e5}\n',
    b'{"a":.5}\n', b'{"a":+1}\n', b'{"a":01}\n', b'{"a":1e}\n', b'{"a":1e+}\n',
    b'{"a":0x10}\n', b'{"a":[1,]}\n', b'{"a":[1 2]}\n', b'{"a":[}\n', b'{"a"}\n',
    b'{"a":1}}\n', b'{"a":"\\', b'{"a":"\\\n', b'{"a":"\\u\n', b'{"a":"\\ud800\\\n',
    b'{"a":"\\uD83D\\uDE00"}\n', b'{"a":"\\ud800\\ud800\\udc00"}\n',
    b'{"\\ud83d\\ude00":1,"schema":1,"op":"status"}\n', b'{"a":"\\/"}\n',
    b"\xef\xbb\xbf" + status(), b"\xef\xbb\xbf\xef\xbb\xbf{}\n", b"\xef\xbb\xbf\n",
    "{\"schema\":1,\"op\":\"status\"}\n".encode("utf-16-be"),
    "{\"schema\":1,\"op\":\"status\"}\n".encode("utf-32-be"),
    "{\"schema\":1,\"op\":\"status\"}\n".encode("utf-16"),
    b"\xfe\xff\x00{\x00}\x00\n", b"\x00\n", b"a\x00\n", b"\x00a\n", b"{\x00}\n",
    b'{"a":"\xff"}\n', b'{"a":"\xc0\x80"}\n', b'{"a":"\xe0\x80\x80"}\n',
    b'{"a":"\xed\xa0\x80"}\n', b'{"a":"\xed\xa0"}\n', b'{"a":"\xf4\x90\x80\x80"}\n',
    b'{"a":"\xf0\x8f\xbf\xbf"}\n', b'{"a":"\xe2\x82"}\n', b'{"a":"\xf0\x9f\x98"}\n',
    b'{"a":"\x80"}\n', b'{"a":"\xf5"}\n', b"\xff{}\n", b'{"a":"\xed\xbf\xbf\xed"}\n',
    b'{"a":1}\n{"b":2}\n', b'\n\n{"a"\n:\n1 x}\n',
]  # fmt: skip
OPS = [
    b'"Status"', b'"st\\u0061tus"', b'"status\\u0000"', b"5", b"null", b'"route"',
]  # fmt: skip


def edge_requests() -> list[bytes]:
    raws = list(SYNTAX)
    raws += [status(schema=s) for s in SCHEMAS]
    raws += [route(schema=s, target=b"null") for s in SCHEMAS[:6]]
    raws += [status(op=o) for o in OPS]
    raws += [
        status(extra=b',"generation":1'),
        status(extra=b',"x":1'),
        b'{"op":"status","schema":1}\n',
        b' \t\r{"schema" : 1 , "op" : "status" } \r\t \n',
        b'{"schema":2,"schema":1,"op":"status"}\n',
        b'{"schema":1,"schema":2,"op":"status"}\n',
        b'{"schema":1,"op":"route","op":"status"}\n',
        b'{"schema":1}\n',
        b'{"op":"status"}\n',
        route().replace(b'"target"', b'"target":null,"target"'),
        route().replace(b'"target"', b'"target":[],"target"'),
        route(target=b"null").replace(
            b'"target"', b'"target":' + TARGET + b',"target"'
        ),
        route(target=b"null").replace(b'"generation"', b'"generation":0,"generation"'),
        route().replace(b'"generation":1,', b""),
        route()[:-2] + b',"x":1}\n',
        b'{"schema":1,"op":"route","generation":1,"target":null,"x":null}\n',
        status(extra=b"," + b'"a":' * 3 + b"1" + b"}" * 3),
    ]
    raws += [route(gen=g, target=b"null") for g in GENERATIONS]
    raws += [route(gen=g, target=tgt(host=b"5")) for g in GENERATIONS]
    raws += [route(target=t) for t in (b"{}", b"[]", b'"x"', b"1", b"true")]
    raws += [
        route(target=b'{"host":"h","port":1,"busid":"1-1"}'),
        route(target=b'{"host":"h","port":1,"busid":"1-1","source":null,"x":1}'),
        route(target=b'{"host":5,"host":"h","port":1,"busid":"1-1","source":null}'),
        route(target=b'{"host":"h","port":1,"busid":"1-1","source":null,"port":0}'),
    ]
    raws += [route(target=tgt(host=h)) for h in HOSTS]
    raws += [route(target=tgt(port=p)) for p in PORTS]
    raws += [route(target=tgt(busid=b)) for b in BUSIDS]
    raws += [route(target=tgt(source=s)) for s in SOURCES_V4]
    raws += [route(target=tgt(host=b"5", port=b"0", busid=b"0", source=b"0"))]
    raws += [route(target=tgt(host=b'"h"', port=b"0", busid=b"0", source=b"0"))]
    raws += [route(target=tgt(busid=b"0", source=b"0"))]
    return raws


def test_selftest(cli_exe: Path) -> None:
    result = subprocess.run(
        [str(cli_exe), "selftest"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0 and result.stdout == "ok\n", result.stderr


def test_edge_requests(cli: Runner) -> None:
    raws = edge_requests()
    tally = check_requests(cli, raws)
    # Each documented strictness was exercised; everything else agrees.
    assert set(tally.strict) == {"scoped", "encoding", "duplicate", "schema"}
    assert tally.accepted >= 30 and tally.agree > 0.85 * len(raws)


def test_bounds_depth_and_big_numbers(cli: Runner) -> None:
    base = b'{"schema":1,"op":"status"}'
    raws = [
        base + b" " * (control.MAX_MESSAGE - len(base) - 1) + b"\n",
        base + b" " * (control.MAX_MESSAGE - len(base)) + b"\n",
        b" " * 20000 + b"\n",
        b"[" * 8191 + b"]" * 8191 + b"\n",
        b"[" * 8192 + b"]" * 8191 + b"\n",
        b'{"a":' * 3000 + b"1" + b"}" * 3000 + b"\n",
        status(extra=b',"x":' + b"[" * 5000 + b"]" * 5000),
        route(target=tgt(host=b"[" * 400 + b"]" * 400)),
        route(target=tgt(host=b'{"a":' * 300 + b"1" + b"}" * 300)),
        route(target=tgt(host=b'"' + b"x" * 3000 + b'"')),
        route(target=tgt(host=b'"' + "é".encode() * 3000 + b'"')),
        route(target=tgt(port=b"[" + b"1," * 3000 + b"1]")),
        status(extra=b',"x":' + b"1" * 4300),
        status(extra=b',"x":' + b"1" * 4301),
        status(extra=b',"x":-' + b"1" * 4301),
        status(extra=b',"x":' + b"1" * 6000 + b".0"),
        status(extra=b',"x":0.' + b"1" * 6000 + b"e-99999999999"),
        route(gen=b"1" * 4301, target=b"["),
        route(target=tgt(port=b"1" * 4300)),
        route(target=tgt(port=b"1." + b"0" * 5000 + b"1")),
        route(target=tgt(port=b"0." + b"0" * 5000 + b"1e5000")),
    ]  # fmt: skip
    tally = check_requests(cli, raws)
    assert tally.agree == len(raws)


# --- random requests ------------------------------------------------------------


@dataclass(frozen=True)
class Raw:
    text: bytes


Value = object  # JSON-ish: Raw, str, int, None, bool, list, or list of pairs


def enc_str(text: str, rng: random.Random) -> bytes:
    out = ['"']
    for ch in text:
        cp = ord(ch)
        roll = rng.random()
        if ch in '"\\':
            out.append("\\" + ch)
        elif cp < 0x20 or (roll < 0.12 and cp < 0x10000):
            out.append(f"\\u{cp:04x}" if roll < 0.5 else f"\\u{cp:04X}")
        elif roll < 0.15 and cp >= 0x10000:
            cp -= 0x10000
            out.append(f"\\u{0xD800 | cp >> 10:04x}\\u{0xDC00 | cp & 0x3FF:04x}")
        elif ch == "/" and roll < 0.5:
            out.append("\\/")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out).encode("utf-8", "surrogatepass")


WS = [b"", b"", b"", b"", b" ", b"\t", b"\r", b"  \n"]


def dump(value: Value, rng: random.Random) -> bytes:
    def ws() -> bytes:
        return rng.choice(WS)

    if isinstance(value, Raw):
        return value.text
    if isinstance(value, bool) or value is None:
        return {True: b"true", False: b"false", None: b"null"}[value]
    if isinstance(value, int):
        return str(value).encode()
    if isinstance(value, str):
        return enc_str(value, rng)
    assert isinstance(value, Pairs | list)
    if isinstance(value, Pairs):
        body = b",".join(
            ws() + enc_str(k, rng) + ws() + b":" + ws() + dump(v, rng) + ws()
            for k, v in value.items
        )
        return b"{" + ws() + body + ws() + b"}"
    return b"[" + b",".join(ws() + dump(v, rng) + ws() for v in value) + b"]"


@dataclass
class Pairs:
    items: list[tuple[str, Value]]


def pick(rng: random.Random, good: list[Value], bad: list[Value]) -> Value:
    return rng.choice(good) if rng.random() < 0.8 else rng.choice(bad)


GOOD_HOSTS: list[Value] = [
    "box.example", "BOX.example.", "10.0.0.7", "::1", "2001:DB8::0:1", "a", "x-1.y",
    "::ffff:10.1.2.3", "1.2.3", "a" * 63 + ".b",
]  # fmt: skip
BAD_VALUES: list[Value] = [
    None, True, False, 0, -1, 2**63, 2**64, Raw(b"1.0"), Raw(b"NaN"), Raw(b"-0"),
    "", " ", "1-1", "é", "\ud800", "a\u0378", "x" * 300, [], [1, "a"],
    Pairs([("a", 1), ("a", 2)]), Pairs([]), "1.2.3.4 ", "01.2.3.4",
]  # fmt: skip


def random_target(rng: random.Random) -> Value:
    items: list[tuple[str, Value]] = [
        ("host", pick(rng, GOOD_HOSTS, BAD_VALUES)),
        ("port", pick(rng, [1, 3240, 65535], [0, 65536, *BAD_VALUES])),
        ("busid", pick(rng, ["1-1", "3-1.4.2", "10-20"], ["1-", "1.1", *BAD_VALUES])),
        ("source", pick(rng, [None, "10.0.0.1", "192.168.1.200"], BAD_VALUES)),
    ]
    return Pairs(shuffle_keys(items, rng))


def shuffle_keys(
    items: list[tuple[str, Value]], rng: random.Random
) -> list[tuple[str, Value]]:
    if rng.random() < 0.08:
        del items[rng.randrange(len(items))]
    if rng.random() < 0.08:
        items.insert(rng.randrange(len(items) + 1), ("extra", rng.choice(BAD_VALUES)))
    if rng.random() < 0.15 and items:
        key, _ = rng.choice(items)
        clone = (key, rng.choice(BAD_VALUES))
        items.insert(rng.randrange(len(items) + 1), clone)
    if rng.random() < 0.5:
        rng.shuffle(items)
    return items


def random_request(rng: random.Random) -> bytes:
    schema = Raw(rng.choice(SCHEMAS)) if rng.random() < 0.2 else 1
    if rng.random() < 0.25:
        items: list[tuple[str, Value]] = [("schema", schema), ("op", "status")]
    else:
        generation: Value = pick(
            rng,
            [1, 2, 2**63 - 1, rng.randrange(1, 2**63)],
            [0, -1, 2**63, Raw(b"1.0"), True, None, "1", Raw(b"1e3")],
        )
        target = random_target(rng) if rng.random() < 0.85 else None
        if rng.random() < 0.03:
            target = rng.choice(BAD_VALUES)
        items = [("schema", schema), ("op", "route")]
        items += [("generation", generation), ("target", target)]
    if rng.random() < 0.05:
        items[1] = ("op", rng.choice(["Route", "STATUS", "route ", 1]))
    tail = rng.choice([b"\n", b"\n", b"\n", b" \n", b"\r\n", b""])
    return rng.choice(WS) + dump(Pairs(shuffle_keys(items, rng)), rng) + tail


INSERTS = [
    b"{", b"}", b"[", b"]", b'"', b"\\", b",", b":", b" ", b"\n", b"\x00", b"\xff",
    b"\xc3", b"\xed\xa0\x80", b"0", b"9", b"e", b".", b"-", b"u", b"n", b"\\u",
    b"\\ud800", b"1e999", b"\xef\xbb\xbf", b'"x":1,', b"null", b"\t",
]  # fmt: skip


def mutate(raw: bytes, rng: random.Random) -> bytes:
    data = bytearray(raw.rstrip(b"\n"))
    for _ in range(rng.randint(1, 3)):
        pos = rng.randrange(len(data) + 1)
        roll = rng.randrange(5)
        if roll == 0 and data:
            del data[min(pos, len(data) - 1)]
        elif roll == 1:
            data[pos:pos] = rng.choice(INSERTS)
        elif roll == 2 and data:
            data[min(pos, len(data) - 1)] = rng.randrange(256)
        elif roll == 3:
            end = min(len(data), pos + rng.randint(1, 12))
            data[pos:pos] = data[pos:end]
        else:
            del data[pos:]
    return bytes(data) + (b"\n" if rng.random() < 0.95 else b"")


def test_random_requests(cli: Runner) -> None:
    rng = random.Random(0x22C7)
    raws = [random_request(rng) for _ in range(5000)]
    raws += [mutate(rng.choice(raws), rng) for _ in range(9000)]
    tally = check_requests(cli, raws)
    assert tally.accepted > 500
    assert tally.agree > 0.9 * len(raws)


# --- validators -------------------------------------------------------------------


def ipv6_text(rng: random.Random) -> str:
    hextets = [
        rng.choice([0, 0, 0, 1, 0xFFFF, rng.randrange(0x10000)]) for _ in range(8)
    ]
    if rng.random() < 0.15:
        hextets[:6] = [0, 0, 0, 0, 0, 0xFFFF]
    parts = [f"{h:0{rng.randint(1, 4)}x}" for h in hextets]
    parts = [p.upper() if rng.random() < 0.2 else p for p in parts]
    if rng.random() < 0.3:
        v4 = (hextets[6] << 16) | hextets[7]
        parts[6:] = [".".join(str((v4 >> s) & 0xFF) for s in (24, 16, 8, 0))]
    if rng.random() < 0.7:
        zeros = [i for i, p in enumerate(parts) if p.strip("0") == "" and p]
        if zeros:
            start = rng.choice(zeros)
            end = start
            while end + 1 < len(parts) and parts[end + 1].strip("0") == "":
                end += 1
            end = rng.randint(start, end)
            head, tail = ":".join(parts[:start]), ":".join(parts[end + 1 :])
            return f"{head}::{tail}"
    return ":".join(parts)


def corrupt(text: str, rng: random.Random) -> str:
    ops: list[Callable[[str], str]] = [
        lambda s: s + ":",
        lambda s: ":" + s,
        lambda s: s.replace("::", ":::", 1),
        lambda s: s + "::1",
        lambda s: s + "%eth0",
        lambda s: s + "0",
        lambda s: "0" + s,
        lambda s: s.replace(":", ":0", 1),
        lambda s: s.replace(".", ".0", 1),
        lambda s: s[: rng.randrange(len(s) + 1)],
        lambda s: s.replace("1", "g", 1),
        lambda s: s + ".1",
        lambda s: s.replace(":", "", 1),
        lambda s: " " + s,
        lambda s: s.replace("1", "\u0661", 1),
    ]
    return rng.choice(ops)(text)


def hostname(rng: random.Random) -> str:
    alphabet = "abcXYZ019-" + ("._ é\u0130" if rng.random() < 0.1 else "")
    labels = []
    for _ in range(rng.randint(1, 6)):
        size = rng.choice([0, 1, 2, 5, 62, 63, 64]) if rng.random() < 0.2 else 3
        labels.append("".join(rng.choice(alphabet) for _ in range(size)))
    name = ".".join(labels)
    if rng.random() < 0.1:
        name = ".".join([("a" * 63)] * 3) + "." + "b" * rng.randint(59, 63)
    return name + ("." if rng.random() < 0.2 else "")


def ipv4_text(rng: random.Random) -> str:
    octets = [str(rng.choice([0, 1, 9, 10, 99, 100, 255, 256, 999])) for _ in range(4)]
    if rng.random() < 0.3:
        octets = [o.zfill(rng.randint(1, 4)) for o in octets]
    if rng.random() < 0.1:
        octets = octets[: rng.randint(0, 5)] + ["1"] * rng.randint(0, 2)
    return ".".join(octets)


def py_validate(kind: str, text: str) -> str:
    try:
        if kind == "host":
            return f"OK {control.validate_host(text)}"
        if kind == "busid":
            control.validate_busid(text)
            return "OK"
        return f"OK {control.validate_source(text)}"
    except control.ControlError:
        return "ERR"


def test_validators(cli: Runner) -> None:
    rng = random.Random(0x0A11)
    cases: list[tuple[str, str]] = [
        ("host", t)
        for t in (
            "1.2.3.4", "0.0.0.0", "001.2.3.4", "1.2.3.4.", "::", "::1", "1::", ":::",
            "1:2:3:4:5:6:7:8", "1:2:3:4:5:6:7::", "::2:3:4:5:6:7:8", "1::2::3",
            "::ffff:1.2.3.4", "::FFFF:0102:0304", "::1.2.3.4", "64:ff9b::1.2.3.4",
            "0:0:0:0:0:ffff:1.2.3.4", "1:0:0:2:0:0:0:3", "1:0:0:2:0:0:3:4",
            "fe80::1%eth0", "fe80::1%", "1:2:3:4:5:6:1.2.3.4", "1:2:3:4:5:6:7:1.2.3.4",
            "a" * 253, "a." * 126 + "a", "a." * 126 + "ab", "-", ".", "a.", "a..",
            "A-B.C", "1:2:3:4:5:6:7:8:9", "12345::",
            "0000:0000:0000:0000:0000:0000:255.255.255.255", "1:2:3:4:5:6:7:8%x",
            "", "é", "\ud800",
        )
    ]  # fmt: skip
    for _ in range(2500):
        cases.append(("host", ipv6_text(rng)))
        cases.append(("host", corrupt(ipv6_text(rng), rng)))
        cases.append(("host", hostname(rng)))
        cases.append(("host", ipv4_text(rng)))
        cases.append(("ipv4", ipv4_text(rng)))
        cases.append(("ipv4", corrupt(ipv4_text(rng), rng)))
        busid = "".join(rng.choice("0123456789-.") for _ in range(rng.randint(0, 35)))
        cases.append(("busid", busid))
        cases.append(("busid", f"{rng.randrange(99)}-{rng.randrange(99)}.1" * 2))
    cases += [
        ("busid", b) for b in ("1-1", "1-" + "1" * 29, "1-" + "1" * 30, "\u0661-\u0661")
    ]
    cases += [("ipv4", s) for s in ("", "1.2.3.4/8", "1.2.3.4\n", "\u0661.2.3.4")]
    lines = [f"{k} {hx(t.encode('utf-8', 'surrogatepass'))}" for k, t in cases]
    scoped = accepted = 0
    for (kind, text), got in zip(cases, cli("validate", lines), strict=True):
        expected = py_validate(kind, text)
        if kind == "host" and "%" in expected:
            assert got == "ERR", text  # scoped IPv6: refused in C (documented)
            scoped += 1
            continue
        accepted += got != "ERR"
        assert got == expected, f"{kind} {text!r}"
    assert scoped >= 1 and accepted > 3000


# --- replies --------------------------------------------------------------------


_spans: list[tuple[int, int]] = []


def _record(exc: UnicodeError) -> tuple[str, int]:
    assert isinstance(exc, UnicodeDecodeError)
    _spans.append((exc.start, exc.end))
    return "\ufffd", exc.end


codecs.register_error("yctl-test-units", _record)


def units(data: bytes) -> list[tuple[int, str]]:
    """Characters of data.decode(errors="replace") with their byte sizes."""
    _spans.clear()
    data.decode("utf-8", "yctl-test-units")
    out, pos = [], 0
    for start, end in [*_spans, (len(data), len(data))]:
        out += [(len(ch.encode()), ch) for ch in data[pos:start].decode()]
        if end > start:
            out.append((end - start, "\ufffd"))
        pos = end
    return out


def text(data: bytes | None) -> str:
    return "" if data is None else data.decode("utf-8", "replace")


def detail_cut(data: bytes | None) -> str:
    kept, size = [], 0
    for width, ch in units(data or b""):
        if size + width > 500:
            break
        kept.append(ch)
        size += width
    return "".join(kept)


@dataclass(frozen=True)
class AnyTarget:
    """A target whose fields the formatter prints without validation."""

    host: str
    port: int
    busid: str
    source: str | None

    def to_json(self) -> dict[str, object]:
        return {
            "host": self.host,
            "port": self.port,
            "busid": self.busid,
            "source": self.source,
        }


FRAGMENTS = [
    b"ok", b" ", b"caf\xc3\xa9", b"\xe4\xb8\xad", b"\xf0\x9f\x98\x80", b"tab\t",
    b'q"b\\', b"\x01\x1f\x7f", b"\xff\xfe", b"\xed\xa0\x80", b"\xe2\x82", b"\xc3",
    b"\xf4\x90\x80\x80", b"\xc0\x80", b"/</", b"\n\r\b\x0c", b"\xc2\x80\xc2\xa0",
    b"\xef\xbf\xbf", b"\xf0\x90", b"\xe0\xa0\x80", b"\xf0\x9f",
]  # fmt: skip
NAMES = [b"urbs", b"bytes_in", b"vendor", b"product", b"a", b"b", b"", b"\xc3\xa9"]


def blob(rng: random.Random, longest: int) -> bytes:
    data = b""
    size = rng.choice([0, 1, 8, 40, longest])
    while len(data) < size:
        data += rng.choice(FRAGMENTS)
    return data


@dataclass
class StatusCase:
    line: str
    python: bytes  # json.dumps exactly as control.encode, before its size check
    expected: bytes | None  # the C reply; None when it does not fit
    updated: float
    valid: bool  # WorkerStatus.from_json must accept the C reply


UPDATED = re.compile(rb'"updated":([-0-9.eE+naNIfinity]+)\}\}\n$')


def c_seconds(t: float) -> str:
    """control.c put_seconds(): the one allowed difference, six fixed decimals."""
    if not -1e18 < t < 1e18:
        t = 0.0
    negative, t = t < 0, abs(t)
    whole = int(t)
    micros = int((t - whole) * 1e6 + 0.5)
    if micros >= 1000000:
        whole, micros = whole + 1, micros - 1000000
    sign = "-" if negative and (whole or micros) else ""
    return f"{sign}{whole}.{micros:06d}"


def status_case(rng: random.Random) -> StatusCase:
    valid = rng.random() < 0.4
    generation = rng.choice([0, 1, 2**63 - 1, 2**64 - 1, rng.randrange(2**64)])
    reconnects = rng.choice([0, 7, 2**32 - 1])
    updated = rng.choice(
        [
            0.0, 1762300000.123456, rng.uniform(0, 2e9), -1.5, 1e17, 5e18, 2.5e-7,
            math.nan, math.inf, -math.inf, rng.uniform(-1e6, 1e6),
        ]
    )  # fmt: skip
    state: bytes | None = rng.choice(sorted(control.STATES)).encode()
    detail: bytes | None = blob(rng, rng.choice([40, 600, 2000]))
    udc: bytes | None = rng.choice([b"", b"fcc00000.usb", b"dummy_udc.0"])
    link: bytes | None = rng.choice([b"", b"high-speed", b"super-speed"])
    target_line, target_obj = "-", None
    if rng.random() < 0.7:
        port = rng.choice([1, 3240, 65535])
        host, busid = b"box.example", b"1-1.2"
        source = rng.choice([b"", b"10.0.0.2"])
        if not valid and rng.random() < 0.5:
            host, busid, source = blob(rng, 200), blob(rng, 31), blob(rng, 15)
            host, busid, source = host[:253], busid[:31], source[:15]
            port = rng.choice([0, 80])
        target_line = f"{hx(host)},{port},{hx(busid)},{hx(source)}"
        source_text = text(source) or None
        target_obj = AnyTarget(text(host), port, text(busid), source_text)
    keys = [b"vendor", b"product", b"remote_speed", b"presented_speed"]
    names = [b"urbs", b"bytes_in", b"bytes_out", b"stalls", b"a.b:c-d_e"]
    if not valid:
        state = rng.choice([state, blob(rng, 20), None])
        udc, link = rng.choice([udc, blob(rng, 40), None]), blob(rng, 20)
        keys, names = keys + NAMES, names + NAMES
    device = [
        (rng.choice(keys), blob(rng, 64)[:64] if not valid else b"0x1d6b")
        for _ in range(rng.randint(0, 6))
    ]
    counters = [
        (rng.choice(names), rng.choice([0, 1, 2**64 - 1, rng.randrange(2**40)]))
        for _ in range(rng.randint(0, 8))
    ]
    if valid:
        counters = [(k, min(v, 2**63)) for k, v in counters]
    cap = rng.choice([65536, 65536, 65536, 0, 1, 100, 400])
    status_obj = control.WorkerStatus(
        generation=generation,
        state=text(state),
        detail=detail_cut(detail),
        target=target_obj,  # type: ignore[arg-type]
        udc=text(udc),
        link_speed=text(link),
        device={text(k): text(v) for k, v in device},
        counters={text(k): v for k, v in counters},
        reconnects=reconnects,
        updated=updated,
    )
    message = {"ok": True, "status": status_obj.to_json()}
    python = json.dumps(message, sort_keys=True, separators=(",", ":")).encode()
    python += b"\n"
    if len(python) <= control.MAX_MESSAGE:
        assert control.encode(message) == python
    found = UPDATED.search(python)
    assert found
    start, end = found.span(1)
    expected: bytes | None = python[:start] + c_seconds(updated).encode() + python[end:]
    assert expected is not None
    if len(expected) > control.MAX_MESSAGE or len(expected) + 1 > cap:
        expected = None
    bits = struct.unpack("<Q", struct.pack("<d", updated))[0]
    line = " ".join(
        [
            str(cap), str(generation), str(reconnects), f"{bits:016x}", hx(state),
            hx(detail), hx(udc), hx(link), target_line,
            ",".join(f"{hx(k)}:{hx(v)}" for k, v in device) or "-",
            ",".join(f"{hx(k)}:{v}" for k, v in counters) or "-",
        ]
    )  # fmt: skip
    valid = valid and expected is not None and math.isfinite(updated)
    return StatusCase(line, python, expected, updated, valid)


def test_status_format(cli: Runner) -> None:
    rng = random.Random(0x57A7)
    cases = [status_case(rng) for _ in range(3000)]
    parsed = refused = 0
    for case, got in zip(cases, cli("status", [c.line for c in cases]), strict=True):
        if case.expected is None:
            assert got == "Z", case.line
            refused += 1
            continue
        reply = bytes.fromhex(got[1:])
        assert reply == case.expected, case.line
        found = UPDATED.search(reply)
        assert found
        value = float(found.group(1))
        if math.isfinite(case.updated) and abs(case.updated) < 1e18:
            assert abs(value - case.updated) <= 5e-7 + abs(case.updated) * 1e-15
        else:
            assert value == 0.0  # Python would send NaN/Infinity here
        decoded = json.loads(reply)
        assert decoded["ok"] is True
        if case.valid:
            have = control.WorkerStatus.from_json(decoded["status"])
            want = control.WorkerStatus.from_json(json.loads(case.python)["status"])
            assert have == dataclasses.replace(want, updated=value)
            parsed += 1
    assert parsed > 300 and refused > 100


def test_status_size_limit(cli: Runner) -> None:
    """Replies stop at MAX_MESSAGE (control.encode) and at the caller's cap."""

    def line(udc: int, cap: int = 65536) -> str:
        return f"{cap} 1 0 {0:016x} {hx(b'idle')} x {hx(b'u' * udc)} x - - -"

    base = len(bytes.fromhex(cli("status", [line(0)])[0][1:]))
    room = control.MAX_MESSAGE - base
    out = cli("status", [line(room), line(room + 1), line(0, base + 1), line(0, base)])
    assert len(bytes.fromhex(out[0][1:])) == control.MAX_MESSAGE
    assert out[1] == "Z" and out[3] == "Z"
    assert len(bytes.fromhex(out[2][1:])) == base
    big = control.WorkerStatus(generation=1, state="idle", udc="u" * 16384)
    with pytest.raises(control.ControlError, match="too large"):
        control.encode({"ok": True, "status": big.to_json()})


def test_error_format(cli: Runner) -> None:
    rng = random.Random(0xE77)
    errors: list[bytes | None] = [
        None, b"", b"unknown request schema", "é".encode() * 300, b"x" * 199,
        b"x" * 200, b"x" * 201, b"\xff" * 250, "😀".encode() * 250,
        b"bad host: '\\ud800'", b'"quoted" \\ \x01\x7f',
    ]  # fmt: skip
    errors += [blob(rng, rng.choice([50, 250, 900])) for _ in range(1500)]
    lines, expected = [], []
    for error in errors:
        reply = control.encode({"ok": False, "error": text(error)[:200]})
        cap = rng.choice([65536, 65536, len(reply) + 1, len(reply), 0])
        lines.append(f"{cap} {hx(error)}")
        expected.append("x" + reply.hex() if len(reply) < cap else "Z")
    assert cli("error", lines) == expected


# --- the worker's control server and process (yoke-gadget-worker-sim) ------------
#
# The worker below runs the simulation build (tests/usbsim/native.py), under
# ASan/UBSan or, with YOKE_NATIVE_SANITIZER=thread (make tsan-usb), TSan.
# Without a route it makes no Raw Gadget calls, so most of these need no
# simulated kernel.


@pytest.fixture
def short_dir() -> Iterator[Path]:
    """A directory short enough for a Unix socket path (pytest's short_dir
    can exceed sun_path)."""
    path = Path(tempfile.mkdtemp(prefix="yctl-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@contextmanager
def idle_worker(
    directory: Path, *extra: str, nofile: int | None = None
) -> Iterator[tuple[subprocess.Popen[str], Path]]:
    sock = directory / "control.sock"
    log = directory / "worker.log"

    def limit() -> None:
        if nofile is not None:
            hard = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
            resource.setrlimit(resource.RLIMIT_NOFILE, (nofile, hard))

    with log.open("w") as sink:
        process = subprocess.Popen(
            [str(native.build("sim")), "--udc", "u", "--socket", str(sock), *extra],
            stderr=sink,
            text=True,
            preexec_fn=limit,
        )
    try:
        native.wait_for(lambda: "native worker on UDC" in log.read_text(), 10)
        yield process, sock
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            process.wait(10)
        text = log.read_text(errors="replace")
        assert "Sanitizer" not in text and "runtime error" not in text, text[-3000:]


def log_of(short_dir: Path) -> str:
    return (short_dir / "worker.log").read_text(errors="replace")


def status_time(sock: Path) -> float:
    start = time.monotonic()
    assert control.ControlClient(str(sock), timeout=10).status().state == "idle"
    return time.monotonic() - start


def client(sock: Path) -> socket.socket:
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.connect(str(sock))
    return conn


def test_refusals_carry_a_category(short_dir: Path) -> None:
    with idle_worker(short_dir) as (_, sock):
        for raw, error in [
            (b"garbage\n", "control message is not a well-formed request"),
            (b'{"schema":2,"op":"status"}\n', "unknown request schema"),
            (b'{"schema":1,"op":"status","x":1}\n', "unknown control request"),
            (route(target=tgt(port=b"0")), "bad port"),
        ]:
            with client(sock) as conn:
                conn.sendall(raw)
                reply = control.decode(control.read_line(conn))
            assert reply == {"ok": False, "error": error}
        with client(sock) as conn:  # half-closed before a full line
            conn.sendall(b'{"schema":1')
            conn.shutdown(socket.SHUT_WR)
            reply = control.decode(control.read_line(conn))
        assert reply["error"] == "control peer closed before a full line"
        with client(sock) as conn:
            conn.sendall(b" " * control.MAX_MESSAGE)
            reply = control.decode(control.read_line(conn))
        assert reply["error"] == "control message too large"


def test_slow_and_idle_clients_do_not_delay_status(short_dir: Path) -> None:
    """One deadline per connection, served concurrently (reviewer's loris)."""
    with idle_worker(short_dir) as (_, sock):
        stop = threading.Event()
        trickling = [client(sock) for _ in range(3)]
        idle = [client(sock) for _ in range(3)]

        def trickle() -> None:
            while not stop.wait(0.2):
                for conn in trickling:
                    with contextlib.suppress(OSError):
                        conn.send(b" ")

        feeder = threading.Thread(target=trickle, daemon=True)
        feeder.start()
        try:
            assert status_time(sock) < 0.5
            # More idle clients than connection slots: the oldest make room.
            idle += [client(sock) for _ in range(12)]
            assert status_time(sock) < 0.5
            # The trickling clients were dropped (deadline or eviction), unanswered.
            time.sleep(2.5)
            for conn in trickling:
                conn.settimeout(1)
                assert conn.recv(100) == b""
        finally:
            stop.set()
            feeder.join()
            for conn in trickling + idle:
                conn.close()


def test_sigterm_with_a_slow_client_exits_promptly(short_dir: Path) -> None:
    with idle_worker(short_dir) as (process, sock), client(sock) as slow:
        slow.send(b'{"sch')
        time.sleep(0.2)
        start = time.monotonic()
        process.send_signal(signal.SIGTERM)
        assert process.wait(10) == 0
        assert time.monotonic() - start < 1.5  # TimeoutStopSec=10s
        assert not sock.exists()


def test_accept_survives_running_out_of_descriptors(short_dir: Path) -> None:
    with idle_worker(short_dir) as (process, _):
        baseline = len(os.listdir(f"/proc/{process.pid}/fd"))
    (short_dir / "control.sock").unlink(missing_ok=True)
    with idle_worker(short_dir, nofile=baseline + 2) as (process, sock):
        held = [client(sock) for _ in range(6)]  # 2 accepted, then EMFILE
        native.wait_for(lambda: "control socket accept: " in log_of(short_dir), 5)
        assert "Too many open files" in log_of(short_dir)
        assert process.poll() is None
        for conn in held:
            conn.close()
        # The server kept going: once descriptors free up it answers again.
        native.wait_for(lambda: status_time(sock) < 10, 15)
        # Logged once per streak of failures, not on every 100 ms retry.
        assert log_of(short_dir).count("control socket accept: ") < 6


def test_second_instance_leaves_the_live_socket_alone(short_dir: Path) -> None:
    with idle_worker(short_dir) as (first, sock):
        inode = sock.stat().st_ino
        second = subprocess.run(
            [str(native.build("sim")), "--udc", "u", "--socket", str(sock)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert second.returncode == 1
        assert "another worker is serving it" in second.stderr
        assert sock.stat().st_ino == inode and first.poll() is None
        assert status_time(sock) < 1
        first.kill()  # leaves a stale socket file behind
        first.wait(5)
    assert sock.exists()
    with idle_worker(short_dir) as (third, sock):  # a stale socket is replaced
        assert third.poll() is None and status_time(sock) < 1


def start_on(sock: Path) -> subprocess.CompletedProcess[str]:
    """A worker that is expected to refuse to start."""
    return subprocess.run(
        [str(native.build("sim")), "--udc", "u", "--socket", str(sock)],
        capture_output=True,
        text=True,
        timeout=10,
    )


def any_exited(processes: list[subprocess.Popen[str]]) -> bool:
    return any(p.poll() is not None for p in processes)


def answers(sock: Path) -> bool:
    try:
        return control.ControlClient(str(sock), timeout=2).status().state == "idle"
    except control.ControlError:
        return False


def test_a_non_socket_at_the_path_is_refused_and_kept(short_dir: Path) -> None:
    sock = short_dir / "control.sock"
    sock.write_text("not a socket\n")
    result = start_on(sock)
    assert result.returncode == 1
    assert "exists and is not a socket" in result.stderr
    assert sock.read_text() == "not a socket\n"


def test_starts_on_one_path_are_serialized(short_dir: Path) -> None:
    """PATH.lock: a starting worker never mistakes another's socket, bound
    but not yet listening, for a stale one."""
    sock = short_dir / "control.sock"
    with (short_dir / "control.sock.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # as if another worker were starting
        result = start_on(sock)
        assert result.returncode == 1
        assert "another worker is serving it" in result.stderr
        assert not sock.exists()
    for _ in range(5):  # simultaneous starts: exactly one serves
        command = [str(native.build("sim")), "--udc", "u", "--socket", str(sock)]
        pair = [
            subprocess.Popen(command, stderr=subprocess.PIPE, text=True)
            for _ in range(2)
        ]
        native.wait_for(functools.partial(any_exited, pair), 10)
        loser = next(p for p in pair if p.poll() is not None)
        winner = next(p for p in pair if p is not loser)
        assert loser.returncode == 1 and winner.poll() is None
        native.wait_for(lambda: answers(sock), 10)  # it may still be starting
        winner.send_signal(signal.SIGTERM)
        assert winner.wait(10) == 0
        for process in pair:
            assert process.stderr is not None
            process.stderr.close()
        assert not sock.exists()


@contextmanager
def presenting(**kwargs: object) -> Iterator[native.NativeRig]:
    rig = native.make_native_rig(Loopback(), **kwargs)  # type: ignore[arg-type]
    try:
        yield rig
    finally:
        rig.close()


def test_termination_signals_reach_only_the_main_thread() -> None:
    with presenting() as rig:
        rig.host.attach()
        rig.host.enumerate("linux")
        assert rig.process is not None
        pid = rig.process.pid
        named = 0
        for task in Path(f"/proc/{pid}/task").iterdir():
            name = (task / "comm").read_text().strip()
            fields = dict(
                line.split(":\t", 1)
                for line in (task / "status").read_text().splitlines()
            )
            blocked = int(fields["SigBlk"], 16)
            if name.startswith("yw-") or task.name == str(pid):  # not TSan's own
                assert not blocked & 1 << (signal.SIGUSR1 - 1), name  # cancellation
            if name.startswith("yw-"):
                named += 1
                assert blocked & 1 << (signal.SIGTERM - 1), name
                assert blocked & 1 << (signal.SIGINT - 1), name
        # control, io, service, event, ep0 and the loopback's endpoints
        assert named >= 6
        assert rig.stop() == 0


def test_unconfirmed_teardown_does_not_starve_route_requests() -> None:
    with presenting() as rig:
        kernel = rig.kernel
        original = kernel.interrupt

        def lost(thread_ident: int) -> None:
            """Every SIGUSR1 lands outside the ioctl: nothing returns."""

        kernel.interrupt = lost  # type: ignore[method-assign]
        try:
            held = rig.route(None)  # the event thread never leaves EVENT_FETCH
            assert held.state == "error" and "teardown unconfirmed" in held.detail
            assert held.target is not None  # the old generation is kept
            for _ in range(3):  # retries back off and are short
                start = time.monotonic()
                again = rig.route(None)
                assert time.monotonic() - start < 1.5
                assert again.generation == held.generation and again.state == "error"
                time.sleep(0.3)
        finally:
            kernel.interrupt = original  # type: ignore[method-assign]
        # A retry confirms the teardown; the held route is followed again.
        rig.wait_state("presenting", "attached", timeout=15)
        assert rig.route(None).state == "idle"


def test_endpoint_thread_failure_completes_the_operation() -> None:
    """pthread_create failing for an endpoint thread ends the session with a
    confirmed teardown (the operation completes as an error)."""
    with presenting() as rig:
        assert rig.process is not None
        pid = rig.process.pid
        saved = resource.prlimit(pid, resource.RLIMIT_NPROC)
        resource.prlimit(pid, resource.RLIMIT_NPROC, (1, saved[1]))
        try:
            with contextlib.suppress(Exception):
                rig.host.attach(timeout=2)
                rig.host.get_descriptor(ch9.DT_DEVICE, 0, 18)
            native.wait_for(
                lambda: "cannot start an endpoint thread" in rig.output(), 10
            )
            native.wait_for(lambda: "session ended: " in rig.output(), 10)
        finally:
            resource.prlimit(pid, resource.RLIMIT_NPROC, saved)
        assert "teardown unconfirmed" not in rig.output()
        rig.wait_state("presenting", "attached", timeout=15)


def test_yx_unavailable_reconnects_without_the_offer(
    monkeypatch: pytest.MonkeyPatch, short_dir: Path
) -> None:
    """The agreed codec cannot be allocated (allocations over 8 MiB fail):
    the session ends and the retry offers no YX, until the next route."""
    options = ("--yx", "fill,lz4,cache", "--yx-cache", "4096")  # a 16 MiB cache
    exporter = native.ExportServer(*options)
    reports = short_dir / "sanitizer"
    try:
        # Only the worker started below sees these; its sanitizer reports go
        # to `reports` (the expected allocation warning would fail rig.close).
        failing = (
            f"allocator_may_return_null=1:max_allocation_size_mb=8:log_path={reports}"
        )
        monkeypatch.setenv("ASAN_OPTIONS", f"detect_leaks=1:abort_on_error=0:{failing}")
        monkeypatch.setenv("TSAN_OPTIONS", failing)
        rig = native.make_native_rig(Loopback(), options=options, present=False)
        try:
            rig.address = exporter.address
            rig.present()
            assert "cannot allocate the YX codec" in rig.output()
            assert "reconnecting without the YX offer" in rig.output()
            rig.host.attach()
            rig.host.enumerate("linux")
            storage = MassStorageHost(rig.host)
            payload = bytes(range(256)) * 64
            storage.write10(64, payload)
            assert storage.read10(64, len(payload) // 512) == payload
            native.wait_for(lambda: rig.counters().get("in_bytes", 0) > 0)
            counters = rig.counters()
            assert counters["yx_raw_in"] == counters["yx_raw_out"] == 0
            # A new route offers YX again (and fails the same way here).
            rig.connect()
            native.wait_for(
                lambda: rig.output().count("reconnecting without the YX offer") == 2, 15
            )
        finally:
            rig.close()
    finally:
        exporter.stop()
    for report in short_dir.glob("sanitizer*"):
        for line in report.read_text().splitlines():
            assert "failed to allocate 0x1000000 bytes" in line, line

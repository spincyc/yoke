"""YWP/1 number semantics in C (native/ywp/src/ywp_num.c) against CPython.

For a JSON number literal, ``ywp_num_parse`` must equal ``float()``
(correctly rounded, ties to even; false exactly where ``float()`` gives
+-inf), ``ywp_num_finite`` must equal ``math.isfinite(float())`` and
``ywp_num_canonical`` must equal ``repr(float())``; ``ywp_num_repr`` must
equal ``repr()`` of a finite double. Every literal goes through all three
literal functions. Each test feeds one batch through a single run of the
sanitized driver ``c/num_check.c``, with inputs from a seeded generator.
The power table header must match ``gen_num_tables.py``. HOST ONLY.
"""

from __future__ import annotations

import math
import random
import struct
import subprocess
import sys
from collections.abc import Iterable, Iterator
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext
from pathlib import Path

import pytest

from tests.native_ywp import gen_num_tables
from tests.native_ywp.conftest import C_DIR, build_exe

# float(int) raises OverflowError from here up; float(str) gives inf.
OVERFLOW = 2**1024 - 2**970
DIGITS = "0123456789"


@pytest.fixture(scope="session")
def num_check(native_dir: Path) -> Path:
    return build_exe(native_dir / "num_check", C_DIR / "num_check.c")


def run(exe: Path, lines: list[str]) -> list[str]:
    result = subprocess.run(
        [str(exe)],
        input="".join(line + "\n" for line in lines),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout.splitlines()
    assert len(out) == len(lines)
    return out


def compare(inputs: list[str], got: list[str], want: list[str]) -> None:
    bad = [(i, g, w) for i, g, w in zip(inputs, got, want, strict=True) if g != w]
    shown = "\n".join(f"  {i[:120]!r}: got {g} want {w}" for i, g, w in bad[:5])
    assert not bad, f"{len(bad)} of {len(inputs)} differ from CPython:\n{shown}"


def bits_of(x: float) -> str:
    return struct.pack(">d", x).hex()


def float_of(bits: int) -> float:
    value: float = struct.unpack(">d", bits.to_bytes(8, "big"))[0]
    return value


def expected(lit: str) -> tuple[str, str, str]:
    """Driver answers to "p", "f" and "c" for a literal, from CPython."""
    x = float(lit)
    finite = math.isfinite(x)
    return (bits_of(x) if finite else "inf"), ("1" if finite else "0"), repr(x)


def check_parse(exe: Path, literals: list[str]) -> None:
    """ywp_num_parse, ywp_num_finite and ywp_num_canonical on each literal."""
    lines = [f"{cmd} {lit}" for lit in literals for cmd in "pfc"]
    want = [answer for lit in literals for answer in expected(lit)]
    compare(lines, run(exe, lines), want)


def check_repr(exe: Path, values: list[float]) -> None:
    patterns = [bits_of(x) for x in values]
    got = run(exe, [f"r {bits}" for bits in patterns])
    compare(patterns, got, [repr(x) for x in values])


def digits(rng: random.Random, n: int) -> str:
    """``n`` random digits, the first nonzero."""
    return rng.choice("123456789") + "".join(rng.choices(DIGITS, k=n - 1))


def literal(rng: random.Random, sig: str, magnitude: int) -> str:
    """A JSON spelling of 0.<sig> * 10^magnitude, point and exponent random."""
    sign = "-" if rng.random() < 0.3 else ""
    if rng.random() < 0.2:
        sig += "0" * rng.randrange(1, 6)
    point = rng.randrange(len(sig) + 1)
    if rng.random() < 0.15 and abs(magnitude) < 30:
        exp, point = None, magnitude  # no exponent: shift the point instead
    else:
        exp = magnitude - point
    if point <= 0:
        mant = "0." + "0" * -point + sig
    elif point >= len(sig):
        mant = sig + "0" * (point - len(sig))
    else:
        mant = sig[:point] + "." + sig[point:]
    if exp is None:
        return sign + mant
    if exp == 0 and rng.random() < 0.5:
        return sign + mant
    e = rng.choice("eE")
    pad = "0" * rng.randrange(3) if rng.random() < 0.1 else ""
    if exp < 0:
        return f"{sign}{mant}{e}-{pad}{-exp}"
    return f"{sign}{mant}{e}{rng.choice(['', '+'])}{pad}{exp}"


def random_finite(rng: random.Random) -> float:
    while True:
        bits = rng.getrandbits(64)
        if (bits >> 52) & 0x7FF != 0x7FF:
            return float_of(bits)


def neighbours(x: float) -> Iterator[float]:
    yield x
    yield math.nextafter(x, math.inf)
    yield math.nextafter(x, -math.inf)


def exact(x: float) -> Decimal:
    return Decimal(x)  # exact binary value; context precision does not apply


def midpoint_literals(lo: float, hi: float) -> Iterator[str]:
    """The exact midpoint of two adjacent doubles and values just off it.

    The offsets put a final nonzero digit just past the midpoint's own digits
    and at significant positions 800, 801 and 1000, past the 767 a midpoint
    can have, where it only acts as the sticky flag of the exact comparison.
    """
    with localcontext() as ctx:
        ctx.prec = 2000
        mid = ((exact(lo) + exact(hi)) / 2).normalize()
        last = mid.as_tuple().exponent
        assert isinstance(last, int)
        for value in (
            mid,
            mid + Decimal(f"1e{last - 3}"),
            mid - Decimal(f"1e{last - 3}"),
        ):
            yield str(value)
            yield format(value, "f")
            yield format(value, "E")
        for pos in (800, 801, 1000):
            place = mid.adjusted() - pos + 1
            if place < last:
                yield format(mid + Decimal(f"1e{place}"), "E")
                yield format(mid - Decimal(f"1e{place}"), "E")


def decimal_spellings(n: int) -> Iterator[str]:
    s = str(n)
    yield s
    yield f"{s}.0"
    if n > 10:
        yield f"{s[0]}.{s[1:]}e{len(s) - 1}"


def nice_decimal(rng: random.Random) -> float:
    while True:
        mant = rng.randrange(1, 10 ** rng.randint(1, 17))
        x = float(f"{mant}e{rng.randint(-345, 330)}")
        if math.isfinite(x):
            return -x if rng.random() < 0.3 else x


def build(*parts: Iterable[str]) -> list[str]:
    return [item for part in parts for item in part]


def test_parse_random_short(num_check: Path) -> None:
    rng = random.Random(0x5EED_0001)
    lits = [
        literal(rng, digits(rng, rng.randint(1, 20)), rng.randint(-345, 330))
        for _ in range(110_000)
    ]
    lits += [
        literal(rng, digits(rng, rng.randint(21, 40)), rng.randint(-345, 330))
        for _ in range(40_000)
    ]
    # Ordinary magnitudes, the bulk of real frames (timestamps, counters).
    lits += [
        literal(rng, digits(rng, rng.randint(1, 19)), rng.randint(-6, 20))
        for _ in range(20_000)
    ]
    check_parse(num_check, lits)


def test_parse_long_mantissas(num_check: Path) -> None:
    rng = random.Random(0x5EED_0002)
    lits = [
        literal(rng, digits(rng, rng.randint(300, 1100)), rng.randint(-345, 330))
        for _ in range(2_500)
    ]
    # Exact expansions of doubles cut short, optionally with a far nonzero
    # tail: the digits deep in the literal decide. Below 1 the plain form
    # also exercises long runs of leading zeros.
    for _ in range(1_500):
        x = abs(random_finite(rng))
        if x < 1:
            mant, exp = format(exact(x), "f"), ""
        else:
            mant, e = format(exact(x), "E").split("E")
            exp = "E" + e
        cut = rng.randint(min(17, len(mant)), min(len(mant), 1100))
        head = mant[:cut].rstrip(".")
        lits.append(head + exp)
        if "." not in head:
            head += "."
        lits.append(head + rng.choice(["", "0" * 400]) + "1" + exp)
    check_parse(num_check, lits)


def test_parse_shortest_round_trips(num_check: Path) -> None:
    rng = random.Random(0x5EED_0003)
    lits = [repr(random_finite(rng)) for _ in range(20_000)]
    lits += [repr(nice_decimal(rng)) for _ in range(10_000)]
    check_parse(num_check, lits)


def test_parse_midpoints(num_check: Path) -> None:
    rng = random.Random(0x5EED_0004)
    lows: list[float] = [0.0, 5e-324, 2.225073858507201e-308, 2.2250738585072014e-308]
    lows += [math.nextafter(sys.float_info.max, 0.0), 1.0, 2.0**52, 2.0**53]
    lows += [abs(random_finite(rng)) for _ in range(1_200)]
    lows += [math.ldexp(rng.randrange(1, 2**20), -1074) for _ in range(200)]
    lows += [float(f"1e{k}") for k in range(-323, 309, 7)]
    lits: list[str] = []
    for lo in lows:
        if lo < sys.float_info.max:
            lits += midpoint_literals(lo, math.nextafter(lo, math.inf))
    check_parse(num_check, lits)


def test_parse_overflow_and_underflow(num_check: Path) -> None:
    edge = Decimal(OVERFLOW)
    with localcontext() as ctx:
        ctx.prec = 2000
        tiny = Decimal("1e-700")
        near = [edge, edge - 1, edge + 1, edge - tiny, edge + tiny, edge / 2]
    lits = build(
        (str(v) for v in near),
        (format(v, "f") for v in near),
        (format(v, "E") for v in near),
        (f"-{v}" for v in near),
        [
            "1.7976931348623157e308",
            "1.7976931348623158e308",
            "1.797693134862315807e308",
            "1.797693134862315808e308",
            "1.7976931348623159e308",
            "1.8e308",
            "-1.8e308",
            "1e309",
            "1e310",
            "1e999999999999",
            "-1e999999999999",
            "1e-999999999999",
            "-1e-999999999999",
            "123e-999999999999999999999999999999",
            "1e0000000000000000000000000000000000000000308",
            "0.0000000000000000000000000000000001e340",
            "1" + "0" * 308,
            "1" + "0" * 309,
            "0." + "0" * 400 + "1e400",
            "0." + "0" * 400 + "1e401",
            "5e-324",
            "-5e-324",
            "4.9406564584124654e-324",
            "2.4703282292062327e-324",
            "2.4703282292062328e-324",
            "2.4703282292062327208828439643411068618252990130716238221279284125e-324",
            "3e-324",
            "7.4e-324",
            "7.5e-324",
            "1e-323",
            "1e-324",
            "1e-325",
            "1e-400",
            "2.2250738585072011e-308",
            "2.2250738585072012e-308",
            "2.2250738585072014e-308",
            "2.225073858507201136057409796709131975934819546351645648e-308",
            "0",
            "-0",
            "0.0",
            "-0.0",
            "0e0",
            "-0E-5",
            "0.000000",
            "-0.0e+999999999999999999",
            "0e-99999999999999999999",
            "-0." + "0" * 1100,
        ],
    )
    check_parse(num_check, lits)


def test_parse_integers(num_check: Path) -> None:
    rng = random.Random(0x5EED_0005)
    ints: list[int] = [rng.randrange(10 ** rng.randint(1, 400)) for _ in range(6_000)]
    for p in range(0, 1030):
        base = 2**p
        ints += [base - 1, base, base + 1]
        if p >= 53:
            half = 2 ** (p - 53)  # half an ulp just above 2^p
            ints += [base + half, base + half - 1, base + half + 1, base + 3 * half]
            ints += [base - half // 2, base - half // 2 - 1]
    for _ in range(3_000):
        q = rng.randint(1, 970)
        m = rng.randrange(2**52, 2**53)
        mid = m * 2**q + 2 ** (q - 1)  # exact tie between two doubles
        ints += [mid, mid - 1, mid + 1]
    ints += [OVERFLOW + d for d in range(-3, 4)]
    ints += [2**64 + d for d in range(-3, 4)] + [2**1024, 10**308, 10**309]
    lits = [s for n in ints for s in decimal_spellings(n)]
    lits += [f"-{lit}" for lit in lits[::7]]
    check_parse(num_check, lits)
    # Python's int -> float rounds identically and overflows at the same point.
    for n in ints:
        try:
            via_int = float(n)
        except OverflowError:
            via_int = math.inf
        assert via_int == float(str(n)), n


def test_parse_rejects_non_numbers(num_check: Path) -> None:
    bad = ["", "-", "+1", ".5", "-.5", "1.", "1e", "1e+", "1E-", "--1", "1x"]
    bad += ["0x10", "1.2.3", "1e5e5", "1e5.0", " 1", "1 ", "inf", "nan", "1_0"]
    lines = [f"{cmd} {lit}" for lit in bad for cmd in "pfc"]
    assert run(num_check, lines) == ["inf", "0", ""] * len(bad)


def round_sig(value: Decimal, digits: int, rounding: str) -> Decimal:
    with localcontext() as ctx:
        ctx.prec, ctx.rounding = digits, rounding
        return +value


def midpoint_above(rng: random.Random, subnormal: float) -> Decimal:
    """The exact midpoint above a random positive double, any binade."""
    while True:
        x = abs(random_finite(rng))
        if rng.random() < subnormal:
            x = math.ldexp(rng.randrange(2**52), -1074)
        if x < sys.float_info.max:
            with localcontext() as ctx:
                ctx.prec = 2000
                return ((exact(x) + exact(math.nextafter(x, math.inf))) / 2).normalize()


def test_literal_adversarial_classes(num_check: Path) -> None:
    """The classes that reach each conversion path and its exact fallback."""
    rng = random.Random(0x5EED_0008)
    lits: list[str] = []
    # <= 19 digits as close to a midpoint as they get (Eisel-Lemire edge).
    for _ in range(12_000):
        mid = midpoint_above(rng, 0.15)
        n = rng.randint(16, 19)
        lits += [
            format(round_sig(mid, n, r), "E") for r in (ROUND_FLOOR, ROUND_CEILING)
        ]
    # Exact ties with at most 19 digits: integers and short fractions.
    for _ in range(4_000):
        x = math.ldexp(float(rng.randrange(2**52, 2**53)), rng.randint(-4, 10))
        with localcontext() as ctx:
            ctx.prec = 100
            mid = ((exact(x) + exact(math.nextafter(x, math.inf))) / 2).normalize()
        lits.append(format(mid, "f") if rng.random() < 0.5 else format(mid, "E"))
    # 20-40 digits straddling a midpoint: the exact comparison every time.
    for _ in range(8_000):
        mid = midpoint_above(rng, 0.2)
        n = rng.randint(20, 40)
        lits += [
            format(round_sig(mid, n, r), "E") for r in (ROUND_FLOOR, ROUND_CEILING)
        ]
    # 1000+ digits: a midpoint's full expansion with a far tail, and noise.
    for _ in range(300):
        mant, exp = format(midpoint_above(rng, 0.5), "E").split("E")
        mant += "" if "." in mant else "."
        tail = "0" * rng.randint(0, 400) + rng.choice(DIGITS)
        lits.append((mant + tail)[:1150] + "E" + exp)
        lits.append(
            digits(rng, rng.randint(1000, 1150)) + f"e{rng.randint(-1500, -700)}"
        )
    # Subnormal 1-2 digit literals, exhaustively.
    lits += [f"{d}e{e}" for d in range(1, 100) for e in range(-326, -305)]
    # 16-19 digits at extreme exponents.
    for _ in range(15_000):
        e = rng.choice([rng.randint(-345, -290), rng.randint(280, 309)])
        lits.append(f"{digits(rng, rng.randint(16, 19))}e{e}")
    # Up to 15 digits (no conversion in ywp_num_canonical), DBL_MIN, DBL_MAX.
    for _ in range(15_000):
        e = rng.choice([rng.randint(-310, -305), rng.randint(305, 309)])
        lits.append(f"{digits(rng, rng.randint(1, 15))}e{e}")
    lits += ["1.79769313486231e308", "1.79769313486232e308", "9.99999999999999e-308"]
    lits += ["1e-307", "2.22507385850720e-308", "2.2250738585072e-308", "-1e-308"]
    check_parse(num_check, lits)


def test_power_table_matches_generator() -> None:
    assert gen_num_tables.TARGET.read_text() == gen_num_tables.render()


def test_modular_search_matches_brute_force() -> None:
    rng = random.Random(0x5EED_0009)
    for _ in range(3_000):
        mod = rng.randint(2, 400)
        a, lo = rng.randrange(mod), rng.randrange(mod)
        hi = rng.randint(lo, mod - 1)
        x0 = rng.randrange(50)
        x1 = x0 + rng.randrange(50)
        brute = any(lo <= a * x % mod <= hi for x in range(x0, x1 + 1))
        assert gen_num_tables._any_hit(a, mod, lo, hi, x0, x1) == brute


def test_eisel_lemire_fallback_only_near_exact_ties() -> None:
    """The cost bound: with at most 19 digits the exact fallback runs only
    for 10^-4 <= 10^q <= 10^-1, where it multiplies by at most 5^4."""
    assert gen_num_tables.fallback_exponents() == {-4, -3, -2, -1}


def test_repr_random_bits(num_check: Path) -> None:
    rng = random.Random(0x5EED_0006)
    values = [random_finite(rng) for _ in range(120_000)]
    values += [math.ldexp(rng.randrange(1, 2**52), -1074) for _ in range(20_000)]
    values += [
        math.ldexp(float(rng.randrange(2**52, 2**53)), rng.randint(-1074, 971))
        for _ in range(20_000)
    ]
    check_repr(num_check, values)


def test_repr_decimal_like(num_check: Path) -> None:
    rng = random.Random(0x5EED_0007)
    values = [nice_decimal(rng) for _ in range(40_000)]
    values += [
        rng.randrange(-(10**17), 10**17) / 10 ** rng.randint(0, 9)
        for _ in range(10_000)
    ]
    values += [1793000000 + rng.random() for _ in range(5_000)]
    check_repr(num_check, values)


def test_repr_boundaries(num_check: Path) -> None:
    seeds: list[float] = [math.ldexp(1.0, k) for k in range(-1074, 1024)]
    seeds += [float(f"1e{k}") for k in range(-323, 309)]
    seeds += [float(f"{d}e{k}") for d in (2, 5, 9) for k in range(-323, 308)]
    seeds += [
        5e-324,
        2.2250738585072014e-308,
        2.225073858507201e-308,
        sys.float_info.max,
        1e16,
        1e15,
        9999999999999998.0,
        0.0001,
        0.00001,
        0.00009999999999999999,
        9007199254740993.0,
        9007199254740992.0,
        123456789012345680.0,
        1e23,
        0.1,
        0.3,
        1 / 3,
        math.pi,
        1793000000.1234567,
    ]
    seeds += [float(2**53 + d) for d in range(-50, 51)]
    values = [
        x
        for seed in seeds
        for y in neighbours(seed)
        for x in (y, -y)
        if math.isfinite(x)
    ]
    values += [0.0, -0.0]
    check_repr(num_check, values)

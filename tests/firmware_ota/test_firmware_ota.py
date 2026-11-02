"""Exercise target upload code with deterministic host I/O/library doubles.

The assumptions are explicit: ESP-IDF supplies image validation and mTLS;
these tests inject their outcomes and cannot establish physical W5500/PoE,
flash power-loss safety, or cryptographic/TLS correctness.
"""

import shutil
import socket
import ssl
import subprocess
import sys
import time
from itertools import pairwise
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMPONENT = ROOT / "firmware/esp32/components/yoke_ota"
HERE = Path(__file__).resolve().parent


@pytest.fixture(scope="module")
def upload_binary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    compiler = shutil.which("cc")
    if compiler is None:
        pytest.skip("host C compiler unavailable; target build is a separate check")
    binary = tmp_path_factory.mktemp("firmware-ota") / "host-upload"
    subprocess.run(
        [
            compiler,
            "-std=gnu17",
            "-Wall",
            "-Wextra",
            "-Wno-unused-function",
            "-Wno-unused-variable",
            "-ffunction-sections",
            "-fdata-sections",
            "-Wl,--gc-sections",
            "-I",
            str(HERE),
            "-I",
            str(COMPONENT / "include"),
            str(HERE / "host_upload.c"),
            str(COMPONENT / "ota_policy.c"),
            "-o",
            str(binary),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return binary


@pytest.mark.parametrize(
    "fault",
    [
        "policy",
        "watchdog",
        "length-overflow",
        "ok",
        "response-lost",
        "pending",
        "already-selected",
        "type",
        "header",
        "chunked",
        "oversize",
        "begin",
        "write",
        "disconnect",
        "timeout",
        "deadline",
        "digest",
        "image",
        "project",
        "journal",
        "clock",
        "selection",
    ],
)
def test_upload_outcomes(upload_binary: Path, fault: str) -> None:
    subprocess.run([str(upload_binary), fault], check=True, capture_output=True)


def test_partition_boundaries() -> None:
    """OTA slots never overlap the preserved application/trust partitions."""
    import csv

    partitions: dict[str, tuple[int, int]] = {}
    with (ROOT / "firmware/esp32/partitions.csv").open() as stream:
        for row in csv.reader(line for line in stream if not line.startswith("#")):
            if not row:
                continue
            partitions[row[0].strip()] = (
                int(row[3].strip(), 0),
                int(row[4].strip(), 0),
            )
    ordered = sorted(partitions.values())
    for (offset, size), (next_offset, _) in pairwise(ordered):
        assert offset + size <= next_offset
    assert ordered[-1][0] + ordered[-1][1] <= 16 * 1024 * 1024
    assert partitions["ota_0"][1] == partitions["ota_1"][1] == 6 * 1024 * 1024
    assert partitions["otatrust"] == (0x19000, 0x10000)
    assert partitions["nvs"] == (0x9000, 0x10000)


def test_unique_local_credentials(tmp_path: Path) -> None:
    """Provisioning creates unique keys, a private directory, and never flashes."""
    import csv
    import sys

    if shutil.which("openssl") is None:
        pytest.skip("OpenSSL provisioning tool unavailable")
    outputs = [tmp_path / "one", tmp_path / "two"]
    for out in outputs:
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "firmware/esp32/provision.py"),
                "--name",
                "ir-test.local",
                "--out",
                str(out),
            ],
            check=True,
            capture_output=True,
        )
        assert out.stat().st_mode & 0o777 == 0o700
        assert (out / "server.key").stat().st_mode & 0o777 == 0o600
        with (out / "trust.csv").open() as stream:
            rows = list(csv.DictReader(stream))
        assert {row["key"] for row in rows} == {
            "ota",
            "server_cert",
            "server_key",
            "client_cert",
            "epoch",
        }
        assert all("client.key" not in row["value"] for row in rows)
    assert (outputs[0] / "server.key").read_bytes() != (
        outputs[1] / "server.key"
    ).read_bytes()
    assert (outputs[0] / "client.key").read_bytes() != (
        outputs[1] / "client.key"
    ).read_bytes()


def test_sdk_override_rejects_drift(tmp_path: Path) -> None:
    source = ROOT / ".scratch/esp32-ota/esp-idf/components/esp-tls/esp_tls_mbedtls.c"
    if not source.exists():
        pytest.skip("optional pinned ESP-IDF source unavailable")
    original = source.read_bytes()
    changed = tmp_path / "changed.c"
    changed.write_bytes(original + b"\n/* unexpected SDK drift */\n")
    output = tmp_path / "override.c"
    script = ROOT / "firmware/esp32/prepare_tls.py"
    result = subprocess.run(
        [sys.executable, str(script), str(changed), str(output)], capture_output=True
    )
    assert result.returncode != 0
    assert not output.exists()
    subprocess.run(
        [sys.executable, str(script), str(source), str(output)],
        check=True,
        capture_output=True,
    )
    assert "yoke_tls_handshake_deadline" in output.read_text()
    assert source.read_bytes() == original


@pytest.fixture(scope="module")
def crypto_tools(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, Path, Path, Path]:
    """Optional real-library checks after the pinned IDF source/tools install."""
    scratch = ROOT / ".scratch/esp32-ota"
    headers = scratch / "esp-idf/components/mbedtls/mbedtls/include"
    libraries = scratch / "host-mbedtls/library"
    compiler = shutil.which("cc")
    if not compiler or not (libraries / "libmbedtls.a").exists():
        pytest.skip(
            "optional pinned host mbedTLS build unavailable; see firmware README"
        )
    out = tmp_path_factory.mktemp("firmware-crypto")
    for source in ("host_trust", "host_tls"):
        subprocess.run(
            [
                compiler,
                "-std=gnu17",
                "-ffunction-sections",
                "-fdata-sections",
                "-Wl,--gc-sections",
                "-pthread",
                "-I",
                str(HERE),
                "-I",
                str(COMPONENT / "include"),
                "-I",
                str(headers),
                str(HERE / f"{source}.c"),
                str(libraries / "libmbedtls.a"),
                str(libraries / "libmbedx509.a"),
                str(libraries / "libmbedcrypto.a"),
                "-o",
                str(out / source),
            ],
            check=True,
            capture_output=True,
        )
    for name in ("one", "two"):
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "firmware/esp32/provision.py"),
                "--name",
                "ir-test.local",
                "--out",
                str(out / name),
            ],
            check=True,
            capture_output=True,
        )
    # OpenSSL 3.0 (Ubuntu 24.04) supports explicit dates through `ca`.
    # Its newer `x509 -not_before/-not_after` flags are not required.
    (out / "index.txt").write_text("")
    (out / "serial").write_text("01\n")
    config = out / "expired.cnf"
    config.write_text(
        "[ca]\ndefault_ca=local\n[local]\n"
        f"database={out / 'index.txt'}\nserial={out / 'serial'}\n"
        f"new_certs_dir={out}\ncertificate={out / 'one/server.crt'}\n"
        f"private_key={out / 'one/server.key'}\n"
        "default_md=sha256\npolicy=names\nx509_extensions=server\n"
        "[names]\ncommonName=supplied\n[server]\n"
        "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n"
        "extendedKeyUsage=serverAuth\nsubjectAltName=DNS:ir-test.local\n"
    )
    subprocess.run(
        [
            "openssl",
            "req",
            "-new",
            "-key",
            str(out / "one/server.key"),
            "-subj",
            "/CN=ir-test.local-server",
            "-out",
            str(out / "expired.csr"),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "openssl",
            "ca",
            "-batch",
            "-selfsign",
            "-config",
            str(config),
            "-in",
            str(out / "expired.csr"),
            "-startdate",
            "20200101000000Z",
            "-enddate",
            "20200102000000Z",
            "-notext",
            "-out",
            str(out / "one/expired.crt"),
        ],
        check=True,
        capture_output=True,
    )
    return out / "host_trust", out / "host_tls", out / "one", out / "two"


@pytest.mark.parametrize("case", ["valid", "wrong-key", "wrong-role", "expired"])
def test_real_library_trust(
    crypto_tools: tuple[Path, Path, Path, Path], case: str
) -> None:
    trust_binary, _, one, two = crypto_tools
    server = one / ("expired.crt" if case == "expired" else "server.crt")
    key = (two if case == "wrong-key" else one) / "server.key"
    client = one / ("server.crt" if case == "wrong-role" else "client.crt")
    result = subprocess.run(
        [str(trust_binary), str(server), str(key), str(client)], capture_output=True
    )
    assert result.returncode == (0 if case == "valid" else 1)


@pytest.mark.parametrize("client", ["valid", "missing", "foreign"])
def test_real_library_mutual_tls(
    crypto_tools: tuple[Path, Path, Path, Path], client: str
) -> None:
    """Local loopback peer only; no device is contacted or enrolled."""
    _, tls_binary, one, two = crypto_tools
    context = ssl.create_default_context(cafile=str(one / "server.crt"))
    if client != "missing":
        credentials = one if client == "valid" else two
        context.load_cert_chain(credentials / "client.crt", credentials / "client.key")
    with subprocess.Popen(
        [
            str(tls_binary),
            str(one / "server.crt"),
            str(one / "server.key"),
            str(one / "client.crt"),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        assert process.stdout is not None
        port = int(process.stdout.readline())
        accepted = False
        try:
            with (
                socket.create_connection(("127.0.0.1", port), timeout=3) as raw,
                context.wrap_socket(raw, server_hostname="ir-test.local") as connection,
            ):
                accepted = connection.recv(2) == b"ok"
        except (ssl.SSLError, ConnectionResetError):
            pass
        finally:
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                pytest.fail("loopback TLS server did not finish")
        assert accepted == (client == "valid")
        assert process.returncode == (0 if client == "valid" else 1)


@pytest.mark.parametrize("phase", ["handshake", "upload"])
def test_real_library_absolute_deadlines(
    crypto_tools: tuple[Path, Path, Path, Path], phase: str
) -> None:
    """Dribble a partial TLS record faster than any per-read idle timeout."""
    _, tls_binary, one, _ = crypto_tools
    with subprocess.Popen(
        [
            str(tls_binary),
            str(one / "server.crt"),
            str(one / "server.key"),
            str(one / "client.crt"),
            "200" if phase == "handshake" else "upload",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        assert process.stdout is not None
        port = int(process.stdout.readline())
        with socket.create_connection(("127.0.0.1", port), timeout=3) as raw:
            if phase == "handshake":
                wire = bytes([22, 3, 3, 0, 255]) + bytes(100)
            else:
                context = ssl.create_default_context(cafile=str(one / "server.crt"))
                context.load_cert_chain(one / "client.crt", one / "client.key")
                incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
                connection = context.wrap_bio(
                    incoming, outgoing, server_hostname="ir-test.local"
                )
                while True:
                    try:
                        connection.do_handshake()
                        break
                    except ssl.SSLWantReadError:
                        if outgoing.pending:
                            raw.sendall(outgoing.read())
                        incoming.write(raw.recv(16384))
                if outgoing.pending:
                    raw.sendall(outgoing.read())
                while True:
                    try:
                        assert connection.read(2) == b"ok"
                        break
                    except ssl.SSLWantReadError:
                        incoming.write(raw.recv(16384))
                connection.write(b"x" * 1024)
                wire = outgoing.read()
                assert len(wire) > 1024
            started = time.monotonic()
            try:
                for byte in wire[:30]:
                    raw.sendall(bytes([byte]))
                    time.sleep(0.03)
                    if process.poll() is not None:
                        break
            except (BrokenPipeError, ConnectionResetError):
                pass
        try:
            process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            pytest.fail("dribbled TLS record exceeded absolute deadline")
        assert process.returncode == 1
        assert time.monotonic() - started < 2

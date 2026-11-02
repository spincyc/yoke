#!/usr/bin/env python3
"""Create unique local OTA credentials and an ESP-IDF NVS input CSV.

Offline provisioning tool, not part of the Linux agent or ESP32 runtime.
It never connects to or flashes a board. The output directory is secret.
"""

import argparse
import csv
import ipaddress
import os
import re
import subprocess
import time
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True, help="device DNS name used in HTTPS")
    parser.add_argument("--ip", help="optional IPv4 address also placed in server SAN")
    parser.add_argument(
        "--out", type=Path, required=True, help="new private output directory"
    )
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,250}[a-z0-9])?", args.name):
        parser.error("--name must be a lowercase DNS name")
    san = f"DNS:{args.name}"
    if args.ip:
        san += f",IP:{ipaddress.IPv4Address(args.ip)}"
    os.umask(0o077)
    # Refuse existing output, including symlinks. Never overwrite credentials.
    args.out.mkdir(mode=0o700, parents=True, exist_ok=False)
    out = args.out.resolve()
    for role, purpose in (("server", "serverAuth"), ("client", "clientAuth")):
        command = [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:P-256",
            "-nodes",
            "-sha256",
            "-days",
            "3650",
            "-subj",
            f"/CN={args.name}-{role}",
            "-addext",
            "basicConstraints=critical,CA:FALSE",
            "-addext",
            "keyUsage=critical,digitalSignature",
            "-addext",
            f"extendedKeyUsage={purpose}",
            "-keyout",
            str(out / f"{role}.key"),
            "-out",
            str(out / f"{role}.crt"),
        ]
        if role == "server":
            command += ["-addext", f"subjectAltName={san}"]
        subprocess.run(command, check=True, capture_output=True)
    # Capture after certificate generation so the local floor is not before
    # notBefore. No clock service is needed by the board.
    epoch = int(time.time())
    with (out / "trust.csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("key", "type", "encoding", "value"))
        writer.writerow(("ota", "namespace", "", ""))
        writer.writerow(("server_cert", "file", "string", out / "server.crt"))
        writer.writerow(("server_key", "file", "string", out / "server.key"))
        writer.writerow(("client_cert", "file", "string", out / "client.crt"))
        writer.writerow(("epoch", "data", "i64", epoch))
    print(f"Created unique device/server and authorized operator credentials in {out}")
    print(
        "Keep client.key on the operator host; "
        "never distribute trust.bin as an app image."
    )
    print("This command did not flash or contact a device.")


if __name__ == "__main__":
    main()

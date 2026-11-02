# W5500 Ethernet updater reference

This ESP-IDF project provides authenticated firmware updates after an
initial USB flash. IR routing is not implemented. Read the normative
[protocol and security contract](../../doc/design/ESP32-UPDATES.md) before
commissioning. Hardware checks remain **NOT RUN**.

Use ESP-IDF **v5.5.6**, commit
`880a203ae603d08a94d06aa01e0d6761958e5e9e`. The tested board configuration is
ESP32-S3, 16 MiB flash, no PSRAM requirement, W5500 on SPI2. Pin defaults are
isolated in `main/Kconfig.projbuild` and require physical bench validation.

Run from the repository root. All tools, build output, generated configs,
temporary data and private commissioning files below stay in `.scratch`.
The install/build commands download the pinned Espressif toolchain; later
device operation and updates are entirely local.

```sh
mkdir -p .scratch/esp32-ota/tmp .scratch/esp32-ota/cache
export IDF_TOOLS_PATH="$PWD/.scratch/esp32-ota/tools"
export TMPDIR="$PWD/.scratch/esp32-ota/tmp"
export XDG_CACHE_HOME="$PWD/.scratch/esp32-ota/cache"
export PIP_CACHE_DIR="$PWD/.scratch/esp32-ota/cache/pip"
export PYTHONPYCACHEPREFIX="$PWD/.scratch/esp32-ota/cache/pycache"
git clone --branch v5.5.6 --depth 1 --recursive --shallow-submodules \
  https://github.com/espressif/esp-idf.git .scratch/esp32-ota/esp-idf
.scratch/esp32-ota/esp-idf/install.sh esp32s3
. .scratch/esp32-ota/esp-idf/export.sh
idf.py -C firmware/esp32 -B "$PWD/.scratch/esp32-ota/build" \
  -D SDKCONFIG="$PWD/.scratch/esp32-ota/sdkconfig" build
```

DHCP is the default. For an isolated static network, run the same `idf.py`
command with `menuconfig` instead of `build`, choose Yoke W5500 Ethernet,
enable static IPv4 and set the address/netmask/gateway, then build. Use the
device name plus curl `--resolve` below; no DNS or mDNS service is required.

For each board, generate new credentials. A correct local operator clock is
needed when generating certificates. The output includes private keys and
the trust image will contain the device key; protect it and back it up
outside this disposable workspace before `wt tidy`.

```sh
python firmware/esp32/provision.py --name ir-01.local \
  --out .scratch/esp32-ota/trust/ir-01
python "$IDF_PATH/components/nvs_flash/nvs_partition_generator/nvs_partition_gen.py" \
  generate .scratch/esp32-ota/trust/ir-01/trust.csv \
  .scratch/esp32-ota/trust/ir-01/trust.bin 0x10000
```

The following commands are **operator hardware actions**, provided for an
initial installation; they have not been executed in software validation.
Choose the actual USB serial device. Do not use `erase-flash`. The app flash
command writes the bootloader, partition table, initial OTA metadata and app; the explicit
trust write populates its dedicated partition. Never reflash partition
geometry over an existing commissioned application with incompatible layout.

```sh
idf.py -C firmware/esp32 -B "$PWD/.scratch/esp32-ota/build" \
  -D SDKCONFIG="$PWD/.scratch/esp32-ota/sdkconfig" -p /dev/ttyACM0 flash
python -m esptool --chip esp32s3 --port /dev/ttyACM0 \
  write_flash 0x19000 .scratch/esp32-ota/trust/ir-01/trust.bin
idf.py -C firmware/esp32 -B "$PWD/.scratch/esp32-ota/build" \
  -D SDKCONFIG="$PWD/.scratch/esp32-ota/sdkconfig" -p /dev/ttyACM0 monitor
```

After trust is written, reset the board. Missing trust before this step is
an expected closed state. The serial log reports Ethernet IP, app version,
and boot/update errors. The first confirmed app is the rollback baseline.
For each future build, set a visible new `PROJECT_VER` in `CMakeLists.txt`.
Keep project name and partition geometry unchanged; distribute the **app
binary only**, never the bootloader, merged flash image, or `trust.bin`.

The exact local client flow uses a chosen IP (replace `192.168.20.50` with
the board's address). `--resolve` gives hostname verification without DNS.
Do not add `-k`/`--insecure`. No Thermite process is involved.

```sh
curl --fail-with-body --noproxy '*' --resolve ir-01.local:443:192.168.20.50 \
  --cacert .scratch/esp32-ota/trust/ir-01/server.crt \
  --cert .scratch/esp32-ota/trust/ir-01/client.crt \
  --key .scratch/esp32-ota/trust/ir-01/client.key \
  https://ir-01.local/v1/status

sha256sum .scratch/esp32-ota/build/yoke-ir-updater.bin
# Copy the 64-character lowercase digest above into the header below.
curl --fail-with-body --max-time 130 --noproxy '*' \
  --resolve ir-01.local:443:192.168.20.50 \
  --cacert .scratch/esp32-ota/trust/ir-01/server.crt \
  --cert .scratch/esp32-ota/trust/ir-01/client.crt \
  --key .scratch/esp32-ota/trust/ir-01/client.key \
  -H 'Content-Type: application/octet-stream' -H 'Expect:' \
  -H 'X-Image-SHA256: REPLACE_WITH_64_CHARACTER_DIGEST' \
  -X PUT --data-binary @.scratch/esp32-ota/build/yoke-ir-updater.bin \
  https://ir-01.local/v1/firmware
```

A 202 response schedules reboot after two seconds. Re-run the same status
GET after reconnecting: check the running `version`, slot and `state`, and
read `error`. If the connection is lost before a response, inspect status
before repeating the upload. `pending-verify` is not success; `healthy`
requires the running app's explicit local health confirmation.

Rollback is automatic on crash/reset before health confirmation or after a
60-second deadline. The previous confirmed app reports `rolled-back` and
the attempted version/digest. A power interruption before boot selection
may instead report `activation-not-completed`; inspect the running version.
To deliberately downgrade, upload an older compatible app through the same
authenticated endpoint. There is no unauthenticated recovery or eFuse
anti-downgrade policy. If neither slot boots, physically recover over USB;
back up application NVS first and preserve its layout. Certificate rotation
uses the physical trust write only and must preserve application NVS.

The explicit software verification command installs/reuses the pinned SDK,
its official ESP32-S3 toolchain and Python constraints, and the repository's
pinned pytest development dependencies under `.scratch/esp32-ota`. It builds
host mbedTLS, requires the entire firmware suite with **zero skipped tests**,
and builds the target image with the actual generated SDK TLS adapter. It
checks the SDK commit, submodule hashes and clean tracked source before and
afterward. No device is contacted or flashed, and nothing is uploaded.

Developer prerequisites are Python 3.12+, git, bash, CMake, Ninja, a host C
compiler and OpenSSL 3.0+. First use downloads developer tools and test
dependencies from GitHub, Espressif and the configured Python package index;
these are build dependencies and are never device runtime requirements.
The reusable `.github/workflows/esp32.yml` runs this command on Ubuntu 24.04
with Python 3.12 and a 20-minute timeout; it publishes no artifacts or keys.

```sh
python3.12 firmware/esp32/verify.py
# Equivalent root Make entry point:
make verify-firmware PYTHON=python3.12

# Reuse an existing SDK/toolchain and pinned dev environment without downloads:
python3 firmware/esp32/verify.py --offline \
  --test-python "$PWD/.scratch/dev/bin/python"
```

For an ordinary offline unit run, the optional real-library checks remain
skippable when tools are absent; no unit test downloads them. To prepare just
the pinned host library manually after SDK installation:

```sh
cmake -S .scratch/esp32-ota/esp-idf/components/mbedtls/mbedtls \
  -B .scratch/esp32-ota/host-mbedtls -DENABLE_TESTING=OFF -DENABLE_PROGRAMS=OFF
cmake --build .scratch/esp32-ota/host-mbedtls -j 8
python -m pytest tests/firmware_ota -ra --basetemp=.scratch/esp32-ota/pytest
```

The dedicated verifier fails on any skip, failed test, incomplete test report,
or missing target adapter. Target compilation, the real-library tests, and the
mocked upload/storage fault campaign are separate evidence. None proves
physical W5500, PoE, flash power-loss behavior or ADC entropy quality; run
the documented hardware acceptance checks before deployment. The reference
app reserves the SAR ADC continuously for primary entropy; see the
[application integration contract](../../doc/design/ESP32-UPDATES.md).

# Yoke USB box (ESP32-S3 tier 1, ESP32-P4 tier 1.5)

This ESP-IDF project is the microcontroller USB box of
[ADR 0027](../../doc/decisions/0027-tiered-usb-product-line-and-native-c-data-path.md).
Each box has one USB role per boot:

| Role | USB controller | Connects to |
|---|---|---|
| `device` (device-side box) | USB host, ESP-IDF USB Host Library | the peripheral, on a USB-A socket |
| `computer` (computer-side box) | USB device, TinyUSB (esp_tinyusb 3.0.0) | the computer, on the native USB port |

A box configured for YWP/1 pairing is a **USB/IP bridge**: the device-side
box exports its USB-A device (USB/IP server, TCP 3240, admitted consumers
only) and the computer-side box imports the paired producer's device and
presents it to the computer. Until it is configured, a box runs its role's
**hardware self-test**. Both share the endpoint services of the IR
firmware: Ethernet with DHCP, the mutual-TLS admin and update API, the boot
health gate, rollback and the watchdog.
Design and limits: [USB-TIER1.md](../../doc/design/USB-TIER1.md). Every
pin, power path and controller limit below is ⚠ bench-unverified.

## Wiring (ESP32-S3-DevKitC-1 N16R8 clone, ⚠)

The module has 8 MB of octal PSRAM (GPIO 33–37; nothing below uses them).
The S3 image puts malloc's large allocations there (TLS buffers, the YWP/1
node), leaving internal RAM to USB/IP; a board without PSRAM still boots on
internal RAM alone ⚠.

| Signal | GPIO | Notes |
|---|---|---|
| W5500 MOSI | 11 | SPI2 at 20 MHz |
| W5500 MISO | 12 | |
| W5500 SCLK | 13 | |
| W5500 CS | 14 | |
| W5500 INT | 10 | |
| W5500 RST | 9 | |
| W5500 power | 3V3 (or 5V if the module has a regulator) | |
| USB D− / D+ | 19 / 20 | full speed; the board's native "USB" connector |

- **Computer-side box:** connect the native "USB" USB-C port to the computer.
  ⚠ The S3 and the W5500 draw about 250–300 mA before the computer
  configures the device, more than the 100 mA USB allows then: power the
  board from its "UART" USB-C (a charger or the same computer) as well, if
  the board's two inputs are diode-isolated (check its schematic).
- **Device-side box:** a USB-A socket breakout on GPIO 19 (D−), 20 (D+), 5V
  and GND; the peripheral takes VBUS from the 5V pin, which the "UART" USB-C
  feeds. Leave the native USB-C unplugged on this box (two hosts on one bus).
  ⚠ The 5V pin passes whatever the supply and the board's input path allow;
  use a powered hub for anything drawing more than about 100 mA.
- The "UART" USB-C (USB-serial bridge) flashes the board and carries the
  console on UART0 at 115200 baud. The native port never carries the console.

## Build

Run from the repository root. The SDK, tools and build output stay in
`.scratch`, exactly as for the [IR firmware](../esp32/README.md); the first
build also downloads `espressif/esp_tinyusb` 3.0.0, pinned by hash in
`dependencies.lock.<target>`, into the ignored `managed_components/`. The
native cores are the repository's own `native/yusb` and `native/ywp`, found
without any setting. `YOKE_NATIVE_YUSB` and `YOKE_NATIVE_YWP` (absolute
paths, exported before the first `idf.py`) are optional overrides for
building against another checkout's cores; a missing core stops the
configuration and names its path.

```sh
mkdir -p .scratch/esp32-ota/tmp .scratch/esp32-ota/cache
export IDF_TOOLS_PATH="$PWD/.scratch/esp32-ota/tools"
export TMPDIR="$PWD/.scratch/esp32-ota/tmp"
export XDG_CACHE_HOME="$PWD/.scratch/esp32-ota/cache"
export PIP_CACHE_DIR="$PWD/.scratch/esp32-ota/cache/pip"
export PYTHONPYCACHEPREFIX="$PWD/.scratch/esp32-ota/cache/pycache"
git clone --branch v5.5.6 --depth 1 --recursive --shallow-submodules \
  https://github.com/espressif/esp-idf.git .scratch/esp32-ota/esp-idf
.scratch/esp32-ota/esp-idf/install.sh esp32s3,esp32p4
. .scratch/esp32-ota/esp-idf/export.sh

# Computer-side image (the default role):
idf.py -C firmware/esp32-usb -B "$PWD/.scratch/esp32-usb/computer" \
  -D SDKCONFIG="$PWD/.scratch/esp32-usb/sdkconfig-computer" -D IDF_TARGET=esp32s3 build
# Device-side image:
idf.py -C firmware/esp32-usb -B "$PWD/.scratch/esp32-usb/device" \
  -D SDKCONFIG="$PWD/.scratch/esp32-usb/sdkconfig-device" -D IDF_TARGET=esp32s3 \
  -D SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.role-device" build
```

A role stored over the admin API (below) overrides the image's default,
so one image serves both boxes once they are on the network.

## Commission and flash (operator hardware actions)

These commands have **not** been run in software validation. Like the IR
endpoint, the box stays inert ("Trust/storage failure … inert until USB
recovery") until its trust partition is written, so provision every board.
A correct local clock is needed; protect the output directory (it holds
private keys).

```sh
python firmware/components/yoke_ota/provision.py --name usb-01.local \
  --out .scratch/esp32-ota/trust/usb-01
python "$IDF_PATH/components/nvs_flash/nvs_partition_generator/nvs_partition_gen.py" \
  generate .scratch/esp32-ota/trust/usb-01/trust.csv \
  .scratch/esp32-ota/trust/usb-01/trust.bin 0x10000
```

Repeat with `usb-02` for the second board. Find the board's **UART**
connector port with `ls -l /dev/serial/by-id/` (a CP210x bridge is usually
`/dev/ttyUSB0`, a CH34x `/dev/ttyACM0`). Never erase the whole flash: that
removes the trust partition and the box stays inert until it is provisioned
again. To clear the box's own settings (role, YWP/1 identity and pairing
state) and keep its trust and clock floor, erase only the `nvs` partition:
`python -m esptool --chip esp32s3 --port "$PORT" erase_region 0x9000 0x10000`.

```sh
PORT=/dev/ttyUSB0   # the UART connector of the computer-side board
idf.py -C firmware/esp32-usb -B "$PWD/.scratch/esp32-usb/computer" \
  -D SDKCONFIG="$PWD/.scratch/esp32-usb/sdkconfig-computer" -p "$PORT" flash
python -m esptool --chip esp32s3 --port "$PORT" \
  write_flash 0x19000 .scratch/esp32-ota/trust/usb-01/trust.bin
idf.py -C firmware/esp32-usb -B "$PWD/.scratch/esp32-usb/computer" \
  -D SDKCONFIG="$PWD/.scratch/esp32-usb/sdkconfig-computer" -p "$PORT" monitor
```

For the device-side board use the `device` build directory and
`sdkconfig-device` in both `idf.py` commands, and `usb-02/trust.bin`. Leave
`monitor` with Ctrl+]. Press the board's RST button after writing trust.

## What the self-tests show

**Ethernet (both roles).** With a cable to a DHCP network:
`yoke-eth: Ethernet link up`, then
`yoke-eth: Ethernet IPv4 192.168.x.y; mTLS HTTPS 443` and
`yoke-node: Boot health confirmed (IP acquired)`. Without a cable the box
confirms after 10 s with "link observed down". A W5500 that does not
answer logs `Ethernet start attempt N failed` four times, then restarts with
a growing backoff (check MISO/MOSI/CS/RST and 3V3).

**Computer role.** The log shows `presenting 1209:0001 at full speed`, then
`self-test: connect`, `bus reset` and `configuration 1` once the computer
enumerates it. The computer sees a CDC-ACM serial port named "Yoke USB
self-test" with the board's MAC as serial number (pid.codes test ID
1209:0001, for bench use only). Everything typed is echoed back:

```sh
SELFTEST=$(ls /dev/serial/by-id/*Yoke_USB_self-test*)
python3 - "$SELFTEST" <<'EOF'
import os, sys, tty
fd = os.open(sys.argv[1], os.O_RDWR | os.O_NOCTTY)  # sends line coding, DTR/RTS
tty.setraw(fd)
os.write(fd, b"yoke echo test\n")
print(os.read(fd, 64))
EOF
```

The box logs the line coding and `DTR 1 RTS 1 (status held off 20 ms)`:
the control replies are deliberately deferred, which proves on silicon that
the host is held off (NAK) until the backend answers, the mechanism the
USB/IP presentation uses for the remote device's replies. Windows also asks
GET_LINE_CODING, the deferred IN data path; the port appears there as "USB
Serial Device (COMn)", on macOS as `/dev/cu.usbmodem*`. `lsusb -v -d 1209:0001` lists the descriptors.

**Device role.** The log shows `USB host running; root port powered`. Plug
a keyboard or mouse into the USB-A socket:

```text
yoke-usb: device 1 attached at low speed: USB 1.10 046d:c31c class 00/00/00 (per-interface) ep0 8, 1 configuration
yoke-usb: configuration 1: 1 interface, bus powered, 100 mA
yoke-usb:  interface 0 alt 0: class 03/01/01 (HID), 1 endpoint
yoke-usb:   endpoint 0x81 interrupt IN, 8 bytes, interval 10
yoke-usb: manufacturer: "Logitech"
yoke-usb: product: "USB Keyboard"
yoke-usb: polling endpoint 0x81 (8 bytes); press keys or move the device
yoke-usb: report 1 (8 bytes): 00 00 04 00 00 00 00 00
```

Each changed report is logged (the third byte above is the key "a").
Unplugging logs `device detached after N reports`. A device without an
interrupt IN endpoint (a flash drive) is enumerated and described only.

## Admin API

The same mutual-TLS API as the IR endpoint (`GET /v1/status`,
`PUT /v1/firmware` with `yoke-usb-box.bin`, `DELETE /v1/coredump`; see
[ESP32-UPDATES.md](../../doc/design/ESP32-UPDATES.md)). Status adds a `usb`
object: the running and configured role, the clock, the internal heap
(`heap_internal_free`, `heap_internal_min_free`; `heap_free` also counts
PSRAM) and the self-test or bridge state. Change the role (stored in NVS,
applied by a restart two seconds after the reply):

```sh
curl --fail-with-body --noproxy '*' --resolve usb-02.local:443:192.168.20.51 \
  --cacert .scratch/esp32-ota/trust/usb-02/server.crt \
  --cert .scratch/esp32-ota/trust/usb-02/client.crt \
  --key .scratch/esp32-ota/trust/usb-02/client.key \
  -H 'Content-Type: application/json' -X PUT --data '{"role": "device"}' \
  https://usb-02.local/v1/usb/config
```

An update image must be a `yoke-usb-box` build: the updater refuses another
project's image (an IR endpoint image, for example).

## USB/IP bridge and pairing

Give each box a YWP/1 node id and the site key; the box restarts as a
bridge and announces on 239.121.111.107:9753. The key is used byte for byte
like the agents' PSK file (`/etc/yoke/psk`, normally 64 hex characters, one
trailing newline dropped; 32 to 256 bytes), so it must be printable ASCII
without `"` or `\` to travel in the JSON body. The PSK is never shown
again; status reports only whether one is set. Build the body from the
0600 file and pipe it to curl, so the key is never on a command line or
in a file:

```sh
OUT=.scratch/esp32-ota/trust/usb-02
sudo cat /etc/yoke/psk | python3 -c '
import json, sys
psk = sys.stdin.buffer.read()
psk = psk[:-1] if psk.endswith(b"\n") else psk
assert all(0x20 <= b < 0x7f and b not in b"\"\\" for b in psk), "printable ASCII without quote or backslash"
print(json.dumps({"node_id": "bench-keyboard", "name": "Bench keyboard", "psk": psk.decode()}))
' | curl --fail-with-body --noproxy '*' --resolve usb-02.local:443:192.168.20.51 \
  --cacert "$OUT/server.crt" --cert "$OUT/client.crt" --key "$OUT/client.key" \
  -H 'Content-Type: application/json' -X PUT --data @- \
  https://usb-02.local/v1/usb/config
```

**Wall clock.** YWP/1 sequences are Unix time and peers drop frames that
are far off, so a bridging box pairs only once its clock is real
(`GET /v1/status`: `usb.clock_synced`, and `ywp.state` reads
`waiting-for-clock` until then). It asks DHCP for an NTP server (option 42)
and also polls `pool.ntp.org` (menuconfig "Yoke USB box", NTP server; it
needs Internet access). On a network with neither, set the clock over the
admin API once after each power-on (forward only, at most a year ahead
unless `"force": true` is added); restarts after a configuration change,
an update or a watchdog reset keep the synchronized clock:

```sh
curl --fail-with-body --noproxy '*' --resolve usb-02.local:443:192.168.20.51 \
  --cacert "$OUT/server.crt" --cert "$OUT/client.crt" --key "$OUT/client.key" \
  -H 'Content-Type: application/json' -X PUT --data "{\"unix\": $(date +%s)}" \
  https://usb-02.local/v1/usb/clock
```

Do the same for the computer-side box (`usb-01`, for example node id
`desk-usb`), then pair them from any machine with the yoke CLI and the same
key: `yoke --interface <its IPv4> --psk-file /etc/yoke/psk pair
bench-keyboard desk-usb`. The computer box imports the device from the
producer's media (`{"busids": ["1-1"], "host": "<IPv4>", "port": 3240}`)
and presents it; `yoke ... status` and `GET /v1/status` (`usb`, `ywp`)
show progress (`usb.last_refusal` on the device box: why it last turned a
connection away). Other fields: `"open": true` for a lab network without a key
(explicit; never with `psk`), `"rebase_sequence": true` to recover from a
corrupt saved pairing state (refused while the node runs, and until the
clock is synchronized), `"node_id": ""` to return to the self-test. Every
change restarts the box.

Limits of this release (⚠ details in USB-TIER1.md):

- the S3 carries low- and full-speed devices; the P4 presents high-speed
  devices only (keyboards and mice need an S3 computer-side box);
- the S3 computer box has IN endpoint numbers 1–4 only: a device using IN
  endpoint 0x85 or 0x86 is refused ("UDC lacks endpoints");
- no isochronous endpoints (USB audio and video), no hubs;
- the device box enumerates configuration descriptors up to 1 KiB and
  serves one import at a time (a second connection, a device list
  included, is refused while one is open);
- URBs up to 4 KiB on the S3 (tier 1 classes: HID, serial, MIDI; not mass
  storage) and 16 KiB on the P4, control data up to 2 KiB on the S3; a
  stock Linux usb-storage consumer (120 KiB URBs) cannot use a device box
  (its I/O fails with -ENOMEM), the Yoke consumers split their transfers;
- reads parked on a quiet device (Linux cdc-acm parks 16) no longer hold
  up control requests or unlinks; only an OUT transfer with data waits for
  buffer room;
- the computer box answers the computer's standard descriptor requests from
  the cache built at import; the S3 cannot sense VBUS, so an unplugged
  computer shows as "suspended" (status `active`).

## ESP32-P4 (tier 1.5, build only)

No P4 hardware has been touched. The P4 build uses the high-speed OTG port
(internal UTMI PHY) for both roles and the internal EMAC with an IP101 PHY
(MDC 31, MDIO 52, reset 51, address 1, ESP-IDF's RMII pins) ⚠: the
ESP32-P4-Function-EV-Board / Waveshare ESP32-P4-ETH layout. Check any other
board (M5Stack Unit PoE-P4: Ethernet pins and whether its high-speed
Type-C can act as a device are ⚠ unconfirmed) under menuconfig "Yoke Ethernet
board profile". ESP-IDF 5.5 builds for P4 silicon v3.0 and later; for an
earlier chip set `CONFIG_ESP32P4_SELECTS_REV_LESS_V3=y`.

```sh
idf.py -C firmware/esp32-usb -B "$PWD/.scratch/esp32-usb/p4" \
  -D SDKCONFIG="$PWD/.scratch/esp32-usb/sdkconfig-p4" -D IDF_TARGET=esp32p4 build
```

## Software verification

`make verify-firmware` (or `python3 firmware/verify.py`) runs every
firmware host test with no skips, including `tests/firmware_usb` (the
TinyUSB backend's descriptor rules and state machine, both self-tests, the
configuration, media and transport rules, and both boxes' bridge glue with
the native cores over TCP loopback, under AddressSanitizer and
UndefinedBehaviorSanitizer), then builds the IR endpoint and this project
for ESP32-S3 and ESP32-P4. It checks that the pinned lock files did not
change, that TinyUSB is linked to the mirrored descriptor callbacks, and
that every image fits its OTA slot. No device is contacted or flashed.
`YOKE_NATIVE_YUSB` and `YOKE_NATIVE_YWP` point the tests, the verifier and
`idf.py` at other native cores. `make firmware-usb-bench` prints the data
path's host-only cost.

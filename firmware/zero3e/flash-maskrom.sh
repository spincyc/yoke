#!/usr/bin/env bash
# Flash Yoke Radxa ZERO 3E sdcard.img in-place to an inserted microSD card
# over the outer USB-C OTG port while the board is in RK3566 Maskrom mode.
#
# Usage:
#   ./flash-maskrom.sh [--location HEX_LOC_ID] [path/to/sdcard.img]
set -euo pipefail

SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(CDPATH='' cd -- "$SCRIPT_DIR/../.." && pwd)"
SCRATCH="$REPO_ROOT/.scratch"

RKDEVELOPTOOL="${RKDEVELOPTOOL:-$SCRATCH/zero3e-br/rkdeveloptool/rkdeveloptool}"
LOADER="${LOADER:-$SCRATCH/rk3566_loader.bin}"
LOC="${RKDEV_LOCATION:-}"

while [ $# -gt 0 ]; do
	case "$1" in
	--location)
		LOC="$2"
		shift 2
		;;
	*)
		break
		;;
	esac
done

IMAGE="${1:-$SCRATCH/zero3e-br/output/images/sdcard.img}"

[ -x "$RKDEVELOPTOOL" ] || { echo "flash-maskrom.sh: rkdeveloptool not executable at $RKDEVELOPTOOL" >&2; exit 1; }
[ -r "$LOADER" ] || { echo "flash-maskrom.sh: RK3566 SPL loader not found at $LOADER" >&2; exit 1; }
[ -r "$IMAGE" ] || { echo "flash-maskrom.sh: sdcard.img not found at $IMAGE" >&2; exit 1; }

export RKDEV_LOCATION="$LOC"

echo "=== Connected Rockchip devices (target location=${LOC:-auto}) ==="
"$RKDEVELOPTOOL" ld

echo "=== Loading RK3566 DDR + USBPlug loader ==="
"$RKDEVELOPTOOL" db "$LOADER"
sleep 2

echo "=== Switching RockUSB active storage to microSD (storage=2) ==="
python3 - "$LOC" <<'EOF'
import ctypes, struct, sys

want_loc = int(sys.argv[1], 16) if len(sys.argv) > 1 and sys.argv[1] else None

class DeviceDescriptor(ctypes.Structure):
    _fields_ = [
        ("bLength", ctypes.c_uint8),
        ("bDescriptorType", ctypes.c_uint8),
        ("bcdUSB", ctypes.c_uint16),
        ("bDeviceClass", ctypes.c_uint8),
        ("bDeviceSubClass", ctypes.c_uint8),
        ("bDeviceProtocol", ctypes.c_uint8),
        ("bMaxPacketSize0", ctypes.c_uint8),
        ("idVendor", ctypes.c_uint16),
        ("idProduct", ctypes.c_uint16),
        ("bcdDevice", ctypes.c_uint16),
        ("iManufacturer", ctypes.c_uint8),
        ("iProduct", ctypes.c_uint8),
        ("iSerialNumber", ctypes.c_uint8),
        ("bNumConfigurations", ctypes.c_uint8),
    ]

lib = ctypes.CDLL("libusb-1.0.so.0")
lib.libusb_init.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
lib.libusb_get_device_list.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))]
lib.libusb_get_device_list.restype = ctypes.c_ssize_t
lib.libusb_free_device_list.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_int]
lib.libusb_get_device_descriptor.argtypes = [ctypes.c_void_p, ctypes.POINTER(DeviceDescriptor)]
lib.libusb_get_bus_number.argtypes = [ctypes.c_void_p]
lib.libusb_get_bus_number.restype = ctypes.c_uint8
lib.libusb_get_port_number.argtypes = [ctypes.c_void_p]
lib.libusb_get_port_number.restype = ctypes.c_uint8
lib.libusb_open.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
lib.libusb_claim_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
lib.libusb_release_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
lib.libusb_close.argtypes = [ctypes.c_void_p]
lib.libusb_exit.argtypes = [ctypes.c_void_p]
lib.libusb_bulk_transfer.argtypes = [
    ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_void_p,
    ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.c_uint,
]

ctx = ctypes.c_void_p()
if lib.libusb_init(ctypes.byref(ctx)) != 0:
    sys.exit("libusb_init failed")

devs = ctypes.POINTER(ctypes.c_void_p)()
cnt = lib.libusb_get_device_list(ctx, ctypes.byref(devs))
target = None
for i in range(cnt):
    d = devs[i]
    desc = DeviceDescriptor()
    if lib.libusb_get_device_descriptor(d, ctypes.byref(desc)) != 0:
        continue
    if desc.idVendor != 0x2207 or desc.idProduct != 0x350a:
        continue
    loc = (lib.libusb_get_bus_number(d) << 8) | lib.libusb_get_port_number(d)
    if want_loc is None or loc == want_loc:
        target = d
        print(f"Selected 2207:350a at LocationID={loc:x}")
        break

if not target:
    sys.exit(f"failed to find Rockchip RK3566 (2207:350a, loc={want_loc})")

handle = ctypes.c_void_p()
if lib.libusb_open(target, ctypes.byref(handle)) != 0:
    sys.exit("libusb_open failed")
lib.libusb_free_device_list(devs, 1)

if lib.libusb_claim_interface(handle, 0) != 0:
    sys.exit("libusb_claim_interface failed")

# Send RockUSB CHANGE_STORAGE (0x2A) with subcode=2 (SD0 / microSD)
cbw = struct.pack("<IIIBBB", 0x43425355, 0x12345679, 0, 0x00, 0, 6) + bytes([0x2A, 2]) + bytes(14)
transferred = ctypes.c_int(0)
raw = (ctypes.c_ubyte * len(cbw))(*cbw)
rc = lib.libusb_bulk_transfer(handle, 0x01, raw, len(cbw), ctypes.byref(transferred), 3000)
if rc != 0:
    sys.exit(f"CHANGE_STORAGE CBW failed: {rc}")
csw = (ctypes.c_ubyte * 13)()
rc = lib.libusb_bulk_transfer(handle, 0x81, csw, 13, ctypes.byref(transferred), 3000)
if rc != 0 or csw[12] != 0:
    sys.exit(f"CHANGE_STORAGE CSW failed: rc={rc} csw={bytes(csw).hex()}")

lib.libusb_release_interface(handle, 0)
lib.libusb_close(handle)
lib.libusb_exit(ctx)
EOF

echo "=== Probing microSD card ==="
RFI_OUT="$("$RKDEVELOPTOOL" rfi)"
printf '%s\n' "$RFI_OUT"
if printf '%s\n' "$RFI_OUT" | grep -q "Flash Size: 0 MB"; then
	echo "flash-maskrom.sh: microSD card not detected (Flash Size: 0 MB)!" >&2
	exit 1
fi

echo "=== Flashing $IMAGE to microSD LBA 0 ==="
"$RKDEVELOPTOOL" wl 0 "$IMAGE"

echo "=== Verifying GPT and readback ==="
"$RKDEVELOPTOOL" ppt
TMP_CHECK="$(mktemp -d)"
trap 'rm -rf "$TMP_CHECK"' EXIT
"$RKDEVELOPTOOL" rl 0 64 "$TMP_CHECK/lba0_64.bin"
cmp -n 32768 "$IMAGE" "$TMP_CHECK/lba0_64.bin"
"$RKDEVELOPTOOL" rl 64 128 "$TMP_CHECK/uboot_head.bin"
dd if="$IMAGE" bs=512 skip=64 count=128 status=none | cmp - "$TMP_CHECK/uboot_head.bin"
echo "Readback verified (GPT + U-Boot header exact match)."

echo "=== Rebooting Radxa ZERO 3E ==="
"$RKDEVELOPTOOL" rd

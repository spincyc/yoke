#!/bin/sh
# Buildroot post-image hook:
#   1. Builds the ~341 MiB GPT sdcard.img (U-Boot + A/B SquashFS + /data) via genimage.
#   2. Builds and verifies the signed LAN OTA bundle yoke-zero3e.raucb via host-rauc.
set -eu

BINARIES_DIR="$1"
BOARD_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"

# 1. Build GPT microSD image (sdcard.img).
support/scripts/genimage.sh -c "$BOARD_DIR/genimage.cfg"

# 2. Build signed RAUC LAN upgrade bundle (yoke-zero3e.raucb).
if [ -z "${YOKE_RAUC_CERT:-}" ] || [ ! -r "${YOKE_RAUC_CERT}" ]; then
	echo "post-image.sh: YOKE_RAUC_CERT must point to a readable signing certificate PEM" >&2
	exit 1
fi
if [ -z "${YOKE_RAUC_KEY:-}" ] || [ ! -r "${YOKE_RAUC_KEY}" ]; then
	echo "post-image.sh: YOKE_RAUC_KEY must point to a readable signing key PEM" >&2
	exit 1
fi

FIRMWARE_VERSION="${YOKE_FIRMWARE_VERSION:-dev}"
BUNDLE_DIR="$(mktemp -d "${BINARIES_DIR}/rauc-bundle.XXXXXX")"
trap 'rm -rf "$BUNDLE_DIR"' EXIT

cp "$BINARIES_DIR/rootfs.squashfs" "$BUNDLE_DIR/rootfs.squashfs"
cat >"$BUNDLE_DIR/manifest.raucm" <<EOF
[update]
compatible=yoke-radxa-zero-3e
version=${FIRMWARE_VERSION}

[bundle]
format=verity

[image.rootfs]
filename=rootfs.squashfs
EOF

BUNDLE_OUT="$BINARIES_DIR/yoke-zero3e.raucb"
rm -f "$BUNDLE_OUT"
"$HOST_DIR/bin/rauc" bundle \
	--cert="$YOKE_RAUC_CERT" \
	--key="$YOKE_RAUC_KEY" \
	"$BUNDLE_DIR" \
	"$BUNDLE_OUT"

"$HOST_DIR/bin/rauc" info --keyring="$YOKE_RAUC_CERT" "$BUNDLE_OUT"

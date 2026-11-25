#!/usr/bin/env bash
# Build the Yoke Radxa ZERO 3E read-only A/B appliance firmware:
#   - sdcard.img        (~341 MiB GPT microSD image, fits 2 GB+ cards)
#   - yoke-zero3e.raucb (signed atomic LAN upgrade bundle)
#
# Usage:
#   ./build.sh [--fetch-only | --build-only] [--ssh-keys PATH] [--version VER]
#              [--buildroot DIR] [--output DIR] [--dl-dir DIR]
set -euo pipefail

SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(CDPATH='' cd -- "$SCRIPT_DIR/../.." && pwd)"
SCRATCH_ROOT="$REPO_ROOT/.scratch"

BUILDROOT_DIR="${BUILDROOT_DIR:-$SCRATCH_ROOT/zero3e-br/buildroot-2026.02.3}"
OUTPUT_DIR="${OUTPUT_DIR:-$SCRATCH_ROOT/zero3e-br/output}"
DL_DIR="${DL_DIR:-$SCRATCH_ROOT/zero3e-br/dl}"
KEY_DIR="${KEY_DIR:-$SCRATCH_ROOT/zero3e-keys}"

MODE="all"
SSH_KEYS="${YOKE_SSH_AUTHORIZED_KEYS:-}"
VERSION="${YOKE_FIRMWARE_VERSION:-}"

while [ $# -gt 0 ]; do
	case "$1" in
	--fetch-only)
		MODE="fetch"
		shift
		;;
	--build-only)
		MODE="build"
		shift
		;;
	--configure-only)
		MODE="configure"
		shift
		;;
	--ssh-keys)
		[ $# -ge 2 ] || { echo "--ssh-keys requires a path" >&2; exit 2; }
		SSH_KEYS="$2"
		shift 2
		;;
	--version)
		[ $# -ge 2 ] || { echo "--version requires a value" >&2; exit 2; }
		VERSION="$2"
		shift 2
		;;
	--buildroot)
		[ $# -ge 2 ] || { echo "--buildroot requires a directory" >&2; exit 2; }
		BUILDROOT_DIR="$2"
		shift 2
		;;
	--output)
		[ $# -ge 2 ] || { echo "--output requires a directory" >&2; exit 2; }
		OUTPUT_DIR="$2"
		shift 2
		;;
	--dl-dir)
		[ $# -ge 2 ] || { echo "--dl-dir requires a directory" >&2; exit 2; }
		DL_DIR="$2"
		shift 2
		;;
	-h | --help)
		sed -n '1,10p' "$0" | sed 's/^# \{0,1\}//'
		exit 0
		;;
	*)
		echo "build.sh: unknown argument: $1" >&2
		exit 2
		;;
	esac
done

if [ ! -f "$BUILDROOT_DIR/Makefile" ]; then
	echo "build.sh: Buildroot tree not found at $BUILDROOT_DIR" >&2
	echo "Download Buildroot 2026.02.3 LTS into $BUILDROOT_DIR or pass --buildroot DIR." >&2
	exit 1
fi

if [ -z "$VERSION" ]; then
	GIT_REV="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo nogit)"
	VERSION="$(date -u +%Y.%m.%d)-${GIT_REV}"
fi

# Generate a local operator RAUC signing keypair in .scratch/zero3e-keys/ if not supplied.
if [ -z "${YOKE_RAUC_CERT:-}" ] || [ -z "${YOKE_RAUC_KEY:-}" ]; then
	mkdir -p "$KEY_DIR"
	chmod 0700 "$KEY_DIR"
	YOKE_RAUC_CERT="$KEY_DIR/ca.cert.pem"
	YOKE_RAUC_KEY="$KEY_DIR/ca.key.pem"
	if [ ! -f "$YOKE_RAUC_CERT" ] || [ ! -f "$YOKE_RAUC_KEY" ]; then
		echo "Generating local RAUC signing keypair in $KEY_DIR (keep private, never commit)..."
		openssl req -x509 -newkey rsa:4096 -nodes \
			-keyout "$YOKE_RAUC_KEY" \
			-out "$YOKE_RAUC_CERT" \
			-days 3650 \
			-subj "/CN=Yoke ZERO 3E Local Firmware Signing CA"
		chmod 0600 "$YOKE_RAUC_KEY"
		chmod 0644 "$YOKE_RAUC_CERT"
	fi
fi

export YOKE_RAUC_CERT
export YOKE_RAUC_KEY
export YOKE_FIRMWARE_VERSION="$VERSION"
if [ -n "$SSH_KEYS" ]; then
	export YOKE_SSH_AUTHORIZED_KEYS="$SSH_KEYS"
fi

mkdir -p "$OUTPUT_DIR" "$DL_DIR"

BR_ARGS=(
	-C "$BUILDROOT_DIR"
	"O=$OUTPUT_DIR"
	"BR2_EXTERNAL=$SCRIPT_DIR"
	"BR2_DL_DIR=$DL_DIR"
)

if [ ! -f "$OUTPUT_DIR/.config" ] || [ "$MODE" = "configure" ]; then
	make "${BR_ARGS[@]}" yoke_zero3e_defconfig
fi

case "$MODE" in
configure)
	echo "Configured $OUTPUT_DIR/.config"
	;;
fetch)
	make "${BR_ARGS[@]}" source
	echo "All Buildroot sources downloaded to $DL_DIR"
	;;
build)
	# Never pass -j to top-level Buildroot make: Buildroot runs packages serially
	# and uses BR2_JLEVEL=0 to parallelize each package build across nproc+1 jobs.
	make "${BR_ARGS[@]}"
	echo "Built:"
	ls -lh "$OUTPUT_DIR/images/sdcard.img" "$OUTPUT_DIR/images/yoke-zero3e.raucb"
	;;
all)
	make "${BR_ARGS[@]}" source
	make "${BR_ARGS[@]}"
	echo "Built:"
	ls -lh "$OUTPUT_DIR/images/sdcard.img" "$OUTPUT_DIR/images/yoke-zero3e.raucb"
	;;
esac

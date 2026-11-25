#!/usr/bin/env bash
# Push and install a signed RAUC firmware bundle (.raucb) over the LAN to one
# or more Radxa ZERO 3E Yoke appliances, reboot into the new A/B slot, and
# verify that the new slot boots cleanly and is marked good.
#
# Usage:
#   ./ota-push.sh [--bundle PATH] [--no-reboot] <host-or-ip> [<host-or-ip> ...]
set -euo pipefail

SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(CDPATH='' cd -- "$SCRIPT_DIR/../.." && pwd)"
BUNDLE="$REPO_ROOT/.scratch/zero3e-br/output/images/yoke-zero3e.raucb"
REBOOT=1

while [ $# -gt 0 ]; do
	case "$1" in
	--bundle)
		BUNDLE="$2"
		shift 2
		;;
	--no-reboot)
		REBOOT=0
		shift
		;;
	-h | --help)
		sed -n '1,8p' "$0" | sed 's/^# \{0,1\}//'
		exit 0
		;;
	*)
		break
		;;
	esac
done

[ $# -ge 1 ] || { echo "ota-push.sh: specify at least one target IP or hostname" >&2; exit 2; }
[ -r "$BUNDLE" ] || { echo "ota-push.sh: bundle not readable: $BUNDLE" >&2; exit 1; }

for target in "$@"; do
	echo "=== [$target] Installing $(basename "$BUNDLE") over LAN ==="
	scp -o BatchMode=yes "$BUNDLE" "root@${target}:/tmp/yoke-update.raucb"
	ssh -o BatchMode=yes "root@${target}" '
		set -eu
		rauc install /tmp/yoke-update.raucb
		rm -f /tmp/yoke-update.raucb
		fw_printenv BOOT_ORDER BOOT_A_LEFT BOOT_B_LEFT
	'
	if [ "$REBOOT" -eq 1 ]; then
		echo "=== [$target] Rebooting into newly flashed slot ==="
		ssh -o BatchMode=yes "root@${target}" 'systemctl reboot' || true
	fi
done

if [ "$REBOOT" -eq 1 ]; then
	echo "=== Waiting for upgraded host(s) to come back online ==="
	for target in "$@"; do
		ok=0
		for _ in $(seq 1 45); do
			if ssh -o BatchMode=yes -o ConnectTimeout=2 "root@${target}" 'true' 2>/dev/null; then
				ok=1
				break
			fi
			sleep 2
		done
		if [ "$ok" -ne 1 ]; then
			echo "ota-push.sh: $target did not return over SSH within 90s" >&2
			exit 1
		fi
		echo "=== [$target] Post-upgrade verification ==="
		ssh -o BatchMode=yes "root@${target}" '
			echo "version: $(cat /etc/yoke-firmware-version)"
			rauc status | grep -E "Booted from|Activated|boot status|rootfs\."
			fw_printenv BOOT_ORDER BOOT_A_LEFT BOOT_B_LEFT
			systemctl --failed --no-pager
		'
	done
fi

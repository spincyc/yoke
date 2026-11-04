#!/bin/sh
# Install yoke system integration on a Radxa ZERO 3E box (ADR 0024).
#
# Run after the Python package is installed under /opt/yoke/current (see
# doc/design/DEPLOYMENT.md). This script installs units, drop-ins, udev
# rules, module lists, the yoke account and directories, plus a first
# agent.toml for the device side. It never starts services or touches the
# firewall; it prints the remaining commands. Existing /etc/yoke/agent.toml
# is never overwritten.
#
#   install.sh device --node-id ID --host IPV4 --busid BUSID [--busid ...]
#              [--port 3240] [--root DIR]
#   install.sh computer [--root DIR]
#   install.sh vhci-lab [--root DIR]
#
# --root DIR stages files below DIR without creating accounts, changing
# ownership or calling systemctl/udevadm (used by the offline tests).
# ⚠ Paths and kernel behavior on the ZERO 3E image are bench-unverified.
set -eu

usage() {
	sed -n '11,15p' "$0" | sed 's/^# \{0,1\}//' >&2
	exit 2
}

fail() {
	echo "install.sh: $*" >&2
	exit 2
}

matches() {
	printf '%s\n' "$1" | grep -Eq "$2"
}

here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH='' cd -- "$here/../.." && pwd)

[ $# -ge 1 ] || usage
role=$1
shift
case "$role" in
device | computer | vhci-lab) ;;
-h | --help) usage ;;
*) fail "unknown role: $role (device, computer or vhci-lab)" ;;
esac

root=""
node_id=""
host=""
port=3240
busids=""
while [ $# -gt 0 ]; do
	case "$1" in
	--root | --node-id | --host | --port | --busid)
		[ $# -ge 2 ] || fail "$1 needs a value"
		case "$1" in
		--root) root=$2 ;;
		--node-id) node_id=$2 ;;
		--host) host=$2 ;;
		--port) port=$2 ;;
		--busid) busids="$busids $2" ;;
		esac
		shift 2
		;;
	-h | --help) usage ;;
	*) fail "unknown argument: $1" ;;
	esac
done

if [ -n "$root" ]; then
	matches "$root" '^/' || fail "--root must be an absolute path"
	root=${root%/}
elif [ "$(id -u)" -ne 0 ]; then
	fail "run as root, or stage with --root DIR"
fi

matches "$port" '^[0-9]{1,5}$' && [ "$port" -ge 1 ] && [ "$port" -le 65535 ] ||
	fail "--port must be 1 through 65535"

if [ "$role" = device ]; then
	matches "$node_id" '^[a-z0-9][a-z0-9-]{0,31}$' ||
		fail "--node-id must match [a-z0-9][a-z0-9-]{0,31}"
	matches "$host" '^[0-9]{1,3}(\.[0-9]{1,3}){3}$' &&
		printf '%s\n' "$host" | awk -F. '{ exit !($1 <= 255 && $2 <= 255 && $3 <= 255 && $4 <= 255) }' ||
		fail "--host must be this box's IPv4 address on the control VLAN"
	[ -n "$busids" ] || fail "at least one --busid is required (see usbip list -l)"
	for busid in $busids; do
		matches "$busid" '^[0-9]+-[0-9]+(\.[0-9]+)*$' && [ ${#busid} -le 31 ] ||
			fail "invalid --busid: $busid"
	done
elif [ -n "$node_id$host$busids" ]; then
	fail "--node-id, --host and --busid apply to the device role only"
fi

put() { # put SOURCE DEST MODE: copy with @PORT@ substituted
	install -d -m 0755 "$(dirname -- "$root$2")"
	sed "s/@PORT@/$port/g" "$1" >"$root$2.tmp"
	chmod "$3" "$root$2.tmp"
	mv -f "$root$2.tmp" "$root$2"
}

if [ -z "$root" ]; then
	getent group yoke >/dev/null || groupadd --system yoke
	getent passwd yoke >/dev/null ||
		useradd --system --gid yoke --home-dir /var/lib/yoke \
			--shell /usr/sbin/nologin yoke
fi
install -d -m 0755 "$root/etc/yoke"
install -d -m 0700 "$root/var/lib/yoke"
[ -n "$root" ] || chown yoke:yoke /var/lib/yoke

units=/etc/systemd/system
put "$repo/examples/yoked.service" "$units/yoked.service" 0644
put "$repo/examples/yoke-update-recover.service" "$units/yoke-update-recover.service" 0644

case "$role" in
device)
	put "$here/device/yoked.service.d/10-zero3e-device.conf" \
		"$units/yoked.service.d/10-zero3e-device.conf" 0644
	for unit in yoke-usbipd.service yoke-usbip-bind@.service \
		yoke-usbip-admission.service yoke-usbip-admission.path; do
		put "$here/device/$unit" "$units/$unit" 0644
	done
	put "$here/device/modules-load.d/yoke-usbip-host.conf" \
		/etc/modules-load.d/yoke-usbip-host.conf 0644
	rules=""
	list=""
	for busid in $busids; do
		rules="${rules}ACTION==\"add\", SUBSYSTEM==\"usb\", ENV{DEVTYPE}==\"usb_device\", KERNEL==\"$busid\", TAG+=\"systemd\", ENV{SYSTEMD_WANTS}+=\"yoke-usbip-bind@%k.service\"
"
		list="${list:+$list, }\"$busid\""
	done
	install -d -m 0755 "$root/etc/udev/rules.d"
	rule_file="$root/etc/udev/rules.d/70-yoke-usbip-bind.rules"
	awk -v rules="$rules" '{ if ($0 == "@RULES@") printf "%s", rules; else print }' \
		"$here/device/udev/70-yoke-usbip-bind.rules.in" >"$rule_file.tmp"
	chmod 0644 "$rule_file.tmp"
	mv -f "$rule_file.tmp" "$rule_file"
	config="$root/etc/yoke/agent.toml"
	if [ -e "$config" ]; then
		echo "keeping existing /etc/yoke/agent.toml"
	else
		sed -e "s/@NODE_ID@/$node_id/g" -e "s/@HOST@/$host/g" \
			-e "s/@PORT@/$port/g" -e "s/@BUSIDS@/$list/g" \
			"$here/device/agent.toml.in" >"$config.tmp"
		chmod 0640 "$config.tmp"
		[ -n "$root" ] || chgrp yoke "$config.tmp"
		mv -f "$config.tmp" "$config"
	fi
	;;
computer)
	if [ -z "$root" ]; then
		getent passwd yoke-gadget >/dev/null ||
			useradd --system --gid yoke --no-create-home \
				--shell /usr/sbin/nologin yoke-gadget
	fi
	put "$repo/examples/yoke-gadget.service" "$units/yoke-gadget.service" 0644
	put "$here/computer/udev/70-yoke-raw-gadget.rules" \
		/etc/udev/rules.d/70-yoke-raw-gadget.rules 0644
	put "$here/computer/modules-load.d/yoke-raw-gadget.conf" \
		/etc/modules-load.d/yoke-raw-gadget.conf 0644
	;;
vhci-lab)
	put "$here/vhci-lab/yoked.service.d/20-usbip-vhci.conf" \
		"$units/yoked.service.d/20-usbip-vhci.conf" 0644
	put "$here/vhci-lab/udev/70-yoke-vhci.rules" /etc/udev/rules.d/70-yoke-vhci.rules 0644
	put "$here/vhci-lab/tmpfiles.d/yoke-vhci.conf" /etc/tmpfiles.d/yoke-vhci.conf 0644
	put "$here/vhci-lab/modules-load.d/yoke-vhci-hcd.conf" \
		/etc/modules-load.d/yoke-vhci-hcd.conf 0644
	;;
esac

if [ -z "$root" ]; then
	systemctl daemon-reload
	command -v udevadm >/dev/null && udevadm control --reload-rules
fi

echo "installed yoke $role integration${root:+ under $root}"
echo "next (see DEPLOYMENT.md, ZERO 3E boxes):"
echo "  1. provision /etc/yoke/psk (64 hex characters, owner yoke, mode 0600)"
case "$role" in
device)
	echo "  2. systemctl enable --now yoke-usbip-admission.service yoke-usbip-admission.path yoke-usbipd.service"
	echo "     systemctl restart systemd-modules-load && udevadm trigger --subsystem-match=usb --action=add"
	;;
computer)
	echo "  2. unbind any ADB/NCM configfs gadget from fcc00000.usb, then"
	echo "     systemctl restart systemd-modules-load && udevadm trigger --subsystem-match=misc"
	echo "     write /etc/yoke/agent.toml from examples/agent-consumer-usb-gadget.toml"
	echo "     systemctl enable --now yoke-gadget.service"
	;;
vhci-lab)
	echo "  2. systemd-tmpfiles --create yoke-vhci.conf; modprobe vhci-hcd; write"
	echo "     /etc/yoke/agent.toml from examples/agent-consumer-usbip.toml"
	;;
esac
echo "  3. sudo -u yoke /opt/yoke/current/venv/bin/python -m yoke.cli agent --commission --config /etc/yoke/agent.toml"
echo "  4. systemctl enable --now yoked"

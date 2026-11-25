#!/bin/sh
# Buildroot post-build hook for Yoke Radxa ZERO 3E read-only A/B appliance.
set -eu

TARGET_DIR="$1"

# Ensure mountpoints exist on the read-only SquashFS rootfs.
mkdir -p "$TARGET_DIR/data" "$TARGET_DIR/var/lib/yoke"

# Persist /etc/yoke on /data so commissioning and config survive A/B RAUC upgrades.
rm -rf "$TARGET_DIR/etc/yoke"
ln -sfn /data/etc/yoke "$TARGET_DIR/etc/yoke"

# Never ship build-time SSH host keys; each box generates its own in /data/ssh/.
rm -f "$TARGET_DIR"/etc/ssh/ssh_host_*

# Ensure helper scripts are executable and on default non-interactive SSH PATH.
chmod 0755 \
	"$TARGET_DIR/usr/local/sbin/yoke-zero3e-init-data" \
	"$TARGET_DIR/usr/local/sbin/yoke-zero3e-mark-good" \
	"$TARGET_DIR/usr/local/sbin/yoke-zero3e-provision"
ln -sfn /usr/local/sbin/yoke-zero3e-provision "$TARGET_DIR/usr/sbin/yoke-zero3e-provision"

# Install RAUC verification CA certificate (required for signed LAN upgrades).
if [ -z "${YOKE_RAUC_CERT:-}" ] || [ ! -r "${YOKE_RAUC_CERT}" ]; then
	echo "post-build.sh: YOKE_RAUC_CERT must point to a readable RAUC CA certificate PEM" >&2
	exit 1
fi
install -D -m 0644 "$YOKE_RAUC_CERT" "$TARGET_DIR/etc/rauc/ca.cert.pem"

# Optionally seed an initial SSH authorized_keys file (copied to /data/ssh/authorized_keys on first boot).
if [ -n "${YOKE_SSH_AUTHORIZED_KEYS:-}" ]; then
	if [ ! -r "${YOKE_SSH_AUTHORIZED_KEYS}" ]; then
		echo "post-build.sh: YOKE_SSH_AUTHORIZED_KEYS is not readable: ${YOKE_SSH_AUTHORIZED_KEYS}" >&2
		exit 1
	fi
	install -D -m 0644 "$YOKE_SSH_AUTHORIZED_KEYS" \
		"$TARGET_DIR/usr/share/yoke-zero3e/initial-authorized-keys"
fi

# Record firmware version stamp inside the immutable rootfs.
FIRMWARE_VERSION="${YOKE_FIRMWARE_VERSION:-dev}"
printf '%s\n' "$FIRMWARE_VERSION" >"$TARGET_DIR/etc/yoke-firmware-version"

# Enable core and Yoke systemd units in their target wants directories.
enable_unit_in() {
	target="$1"
	unit="$2"
	wants="$TARGET_DIR/etc/systemd/system/${target}.wants"
	mkdir -p "$wants"
	if [ -e "$TARGET_DIR/etc/systemd/system/$unit" ]; then
		ln -sfn "/etc/systemd/system/$unit" "$wants/$unit"
	elif [ -e "$TARGET_DIR/usr/lib/systemd/system/$unit" ]; then
		ln -sfn "/usr/lib/systemd/system/$unit" "$wants/$unit"
	else
		echo "post-build.sh: expected systemd unit missing: $unit" >&2
		exit 1
	fi
}

# Clean up any legacy wants symlinks across rebuilds.
rm -f \
	"$TARGET_DIR/etc/systemd/system/multi-user.target.wants/yoke-data-init.service" \
	"$TARGET_DIR/etc/systemd/system/multi-user.target.wants/var-lib-yoke.mount" \
	"$TARGET_DIR/etc/systemd/system/multi-user.target.wants/systemd-networkd-wait-online.service"

for u in yoke-data-init.service var-lib-yoke.mount; do
	enable_unit_in local-fs.target "$u"
done

enable_unit_in network-online.target systemd-networkd-wait-online.service

for u in \
	sshd.service \
	systemd-networkd.service \
	systemd-resolved.service \
	systemd-timesyncd.service \
	yoke-rauc-good.service \
	yoked.service \
	yoke-gadget.service \
	yoke-usbipd.service \
	yoke-usbip-admission.service \
	yoke-usbip-admission.path; do
	enable_unit_in multi-user.target "$u"
done

#!/bin/sh
# PID 1 inside the test VM (host root, read-only over 9p). See launch.py.
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t tmpfs tmpfs /tmp
mount -t tmpfs tmpfs /run
mount -t configfs configfs /sys/kernel/config
ip link set lo up
WT=$(sed -n 's/.*yoke\.worktree=\([^ ]*\).*/\1/p' /proc/cmdline)
export HOME=/tmp PYTHONDONTWRITEBYTECODE=1 PATH=/usr/bin:/usr/sbin:/bin:/sbin
"$WT/.scratch/venv/bin/python" "$WT/tests/usbsim/kernel_vm/guest.py" "$WT" ||
	echo 'YOKE-VM {"step": "guest", "ok": false, "error": "guest.py crashed"}'
sync
echo 1 > /proc/sys/kernel/sysrq
echo o > /proc/sysrq-trigger
sleep 10

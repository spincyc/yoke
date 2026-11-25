# Yoke Radxa ZERO 3E Appliance Firmware (Buildroot A/B + RAUC)

Minimal, immutable read-only Linux appliance firmware for the **Radxa ZERO 3E**
(Rockchip RK3566, Gigabit Ethernet via RTL8211F-CG, DWC3 OTG + USB 3.0 Host).

One unified image runs either Tier-2 Yoke role (**computer** or **device**),
fits comfortably on a **2 GB microSD card**, includes `CONFIG_USB_RAW_GADGET=y`
and `CONFIG_USBIP_HOST=m` out of the box, and supports **atomic over-the-LAN
whole-firmware upgrades with automatic U-Boot + hardware-watchdog rollback**
via [RAUC](https://rauc.io/).

> Bench-verified on two physical Radxa ZERO 3E boards (mainline Linux 6.12.6,
> Gigabit RTL8211F Ethernet, RAUC A/B `dm-verity` over-the-LAN upgrades,
> `nftables` ADR 0024 USB/IP admission, RK3566 DWC3 `CONFIG_USB_RAW_GADGET`
> + native C11 `yoke-gadget-worker`, and end-to-end HID device enumeration
> over Ethernet).

## Partition Layout (`sdcard.img`, ~341 MiB)

| Offset / Partition | Size | Format | Purpose |
| --- | --- | --- | --- |
| Raw @ 32 KiB | ~12 MiB | Rockchip SPL + ATF BL31 + U-Boot 2026.01 | Bootloader (`u-boot-rockchip.bin`) |
| `p1` (`uboot-env`) | 64 KiB @ 16 MiB | Raw U-Boot env (GPT type `3de21764-...`) | RAUC `BOOT_ORDER`, `BOOT_A_LEFT`, `BOOT_B_LEFT` |
| `p2` (`yoke-boot`) | 4 MiB @ 17 MiB | FAT16 | Static RAUC slot selector (`boot.scr`) |
| `p3` (`yoke-a`) | 128 MiB | Read-only SquashFS | Slot A (kernel + DTB + modules + rootfs + Yoke) |
| `p4` (`yoke-b`) | 128 MiB | Read-only SquashFS | Slot B (atomic OTA target / rollback slot) |
| `p5` (`yoke-data`) | 64 MiB | ext4 (`/data`, `sync`) | Persistent `/etc/yoke`, `/var/lib/yoke`, SSH keys, RAUC state |

## Building

Requires a standard Linux host with Buildroot dependencies (`gcc`, `g++`, `make`,
`bc`, `bison`, `flex`, `unzip`, `wget`, `openssl`, `python3`) and Buildroot
`2026.02.3` LTS extracted under `.scratch/zero3e-br/buildroot-2026.02.3` (or
specified via `--buildroot`).

```bash
# 1. Fetch upstream tarballs (only step requiring internet access)
./firmware/zero3e/build.sh --fetch-only --ssh-keys ~/.ssh/id_ed25519.pub

# 2. Build offline (produces sdcard.img and yoke-zero3e.raucb)
./firmware/zero3e/build.sh --build-only --ssh-keys ~/.ssh/id_ed25519.pub
```

Local RAUC signing keys are automatically generated in
`.scratch/zero3e-keys/` (`ca.cert.pem` / `ca.key.pem`) if `YOKE_RAUC_CERT` and
`YOKE_RAUC_KEY` are not set. Keep `.scratch/zero3e-keys/ca.key.pem` backed up:
future LAN `.raucb` bundles must be signed by the same CA embedded in
`/etc/rauc/ca.cert.pem`.

## Initial MicroSD Flash

Write `sdcard.img` once to any 2 GB+ microSD card:

```bash
# Option A: USB microSD card reader
sudo dd if=.scratch/zero3e-br/output/images/sdcard.img of=/dev/sdX bs=4M status=progress conv=fsync

# Option B: In-place over the outer USB-C OTG port while in RK3566 Maskrom mode
./firmware/zero3e/flash-maskrom.sh
```

Insert the microSD card into the Radxa ZERO 3E, connect Ethernet + power (or
OTG USB-C), and SSH in as `root@yoke-zero3e.local` (or the DHCP IP).

## Provisioning a Box (Persists Across Upgrades)

```bash
# Computer-side box (OTG port plugged into host computer):
yoke-zero3e-provision computer \
  --node-id desk-usb \
  --interface 10.20.30.2 \
  --psk <64-hex-chars>

# Device-side box (USB peripheral plugged into USB 3.0 Host port):
usbip list -l
yoke-zero3e-provision device \
  --node-id rack-usb \
  --host 10.20.30.3 \
  --busid 1-1 \
  --psk <64-hex-chars>
```

## Over-the-LAN Whole-Firmware Upgrades (RAUC)

Every future upgrade (kernel, DTB, `yoke-gadget-worker`, Python `yoke`,
systemd units) is delivered as one signed `yoke-zero3e.raucb` (~44 MiB):

```bash
# Recommended: push, install, reboot, and verify one or more boxes
./firmware/zero3e/ota-push.sh <box-ip-1> [<box-ip-2> ...]

# Manual push via SSH/SCP
scp .scratch/zero3e-br/output/images/yoke-zero3e.raucb root@<box-ip>:/tmp/
ssh root@<box-ip> 'rauc install /tmp/yoke-zero3e.raucb && reboot'

# Pull over HTTP streaming (zero temp RAM required)
ssh root@<box-ip> 'rauc install http://<workstation-ip>:8000/yoke-zero3e.raucb && reboot'
```

On reboot, U-Boot boots the newly flashed slot with 3 tries (`BOOT_*_LEFT`).
Only after `sshd.service` and the configured Yoke services (`yoked`, plus
`yoke-gadget` or `yoke-usbipd`) reach `active` does `yoke-rauc-good.service`
run `rauc status mark-good`. If the new slot crashes, hangs (hardware watchdog
at 30 s), or fails to bring up SSH/Yoke, U-Boot automatically rolls back to the
previous working slot while keeping `/data` intact.

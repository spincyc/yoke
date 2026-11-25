# RAUC A/B slot selector for Yoke Radxa ZERO 3E.
# Expects GPT layout on ${mmcdev} (0 on ZERO 3E microSD, 1 on ZERO 3W microSD):
#   p1: uboot-env  (64 KiB, GPT type 3de21764-95bd-54bd-a5c3-4abe786f38a8)
#   p2: yoke-boot  (4 MiB FAT16, holds this boot.scr)
#   p3: yoke-a     (128 MiB SquashFS, PARTUUID 11111111-1111-4111-8111-00000000000a)
#   p4: yoke-b     (128 MiB SquashFS, PARTUUID 11111111-1111-4111-8111-00000000000b)
#   p5: yoke-data  (64 MiB ext4, persistent config/state/SSH)

test -n "${mmcdev}" || setenv mmcdev 0
test -n "${BOOT_ORDER}" || setenv BOOT_ORDER "A B"
test -n "${BOOT_A_LEFT}" || setenv BOOT_A_LEFT 3
test -n "${BOOT_B_LEFT}" || setenv BOOT_B_LEFT 3

setenv bootpart
setenv rootpartuuid
setenv raucslot

for slot in ${BOOT_ORDER}; do
    if test "x${bootpart}" != "x"; then
        # already selected
    elif test "x${slot}" = "xA"; then
        if test 0x${BOOT_A_LEFT} -gt 0; then
            setexpr BOOT_A_LEFT ${BOOT_A_LEFT} - 1
            echo "Yoke: booting Slot A on mmc ${mmcdev} (${BOOT_A_LEFT} tries remaining)"
            setenv bootpart "${mmcdev}:3"
            setenv rootpartuuid "11111111-1111-4111-8111-00000000000a"
            setenv raucslot "A"
        fi
    elif test "x${slot}" = "xB"; then
        if test 0x${BOOT_B_LEFT} -gt 0; then
            setexpr BOOT_B_LEFT ${BOOT_B_LEFT} - 1
            echo "Yoke: booting Slot B on mmc ${mmcdev} (${BOOT_B_LEFT} tries remaining)"
            setenv bootpart "${mmcdev}:4"
            setenv rootpartuuid "11111111-1111-4111-8111-00000000000b"
            setenv raucslot "B"
        fi
    fi
done

if test -n "${bootpart}"; then
    saveenv
else
    echo "Yoke: no bootable RAUC slot left; resetting boot counters"
    setenv BOOT_A_LEFT 3
    setenv BOOT_B_LEFT 3
    saveenv
    reset
fi

setenv bootargs "console=ttyS2,1500000n8 earlycon root=PARTUUID=${rootpartuuid} rootfstype=squashfs ro rootwait rauc.slot=${raucslot}"

if sqfsload mmc ${bootpart} ${kernel_addr_r} /boot/Image; then
    if sqfsload mmc ${bootpart} ${fdt_addr_r} /boot/rockchip/rk3566-radxa-zero-3e.dtb; then
        booti ${kernel_addr_r} - ${fdt_addr_r}
    fi
fi

echo "Yoke: failed to load kernel/DTB from Slot ${raucslot}; resetting"
reset

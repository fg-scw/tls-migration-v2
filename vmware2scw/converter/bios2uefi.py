"""Convert a BIOS/MBR disk image to UEFI/GPT boot.

Scaleway instances use UEFI firmware. VMware VMs often use BIOS/MBR.

Strategy (qemu-nbd 6.2 workaround):
1. Convert qcow2 → raw (sgdisk cannot work via qemu-nbd on QEMU 6.2)
2. Resize raw +200MB
3. Fix GPT backup header with sgdisk -e on raw file
4. Create ESP partition via sgdisk on raw file
5. Format ESP as FAT32 via losetup + mkfs.vfat
6. Convert raw → qcow2 (uncompressed, for virt-customize)
7. Install grub-efi inside guest via virt-customize (guest-side)
"""

import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

ENV = {"LIBGUESTFS_BACKEND": "direct"}


def _run(cmd, check=True, env_override=None, **kwargs):
    """Run a command, optionally raise on failure."""
    logger.info(f"  $ {' '.join(cmd)}")
    run_env = {**os.environ, **ENV}
    if env_override:
        run_env.update(env_override)
    result = subprocess.run(
        cmd, capture_output=True, text=True,
        env=run_env, **kwargs,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command failed ({' '.join(cmd[:4])}): "
            f"{result.stderr.strip()[-500:]}"
        )
    return result


def detect_boot_type(qcow2_path: str) -> str:
    """Detect if disk uses BIOS or UEFI boot.

    Returns: 'uefi', 'bios-gpt', or 'bios-mbr'
    """
    result = subprocess.run(
        ["guestfish", "--ro", "-a", qcow2_path, "--",
         "run", ":", "part-get-parttype", "/dev/sda"],
        capture_output=True, text=True,
        env={**os.environ, **ENV},
    )
    part_type = result.stdout.strip()

    if part_type == "gpt":
        for part_num in range(1, 10):
            res = subprocess.run(
                ["guestfish", "--ro", "-a", qcow2_path, "--",
                 "run", ":", "part-get-gpt-type", "/dev/sda", str(part_num)],
                capture_output=True, text=True,
                env={**os.environ, **ENV},
            )
            guid = res.stdout.strip().upper()
            if guid == "C12A7328-F81F-11D2-BA4B-00A0C93EC93B":
                logger.info(f"Found EFI System Partition at partition {part_num}")
                return "uefi"
            if res.returncode != 0:
                break

        result4 = subprocess.run(
            ["guestfish", "--ro", "-a", qcow2_path, "-i", "--", "mountpoints"],
            capture_output=True, text=True,
            env={**os.environ, **ENV},
        )
        if "/boot/efi" in result4.stdout:
            return "uefi"

        return "bios-gpt"
    elif part_type in ("msdos", "dos"):
        return "bios-mbr"
    else:
        logger.warning(f"Unknown partition type: '{part_type}'")
        return "bios-mbr"


def _setup_loop(raw_path: str, offset: int = 0, sizelimit: int = 0) -> str:
    """Setup a loop device for a raw file or partition. Returns /dev/loopN."""
    cmd = ["losetup", "--find", "--show"]
    if offset:
        cmd += ["--offset", str(offset)]
    if sizelimit:
        cmd += ["--sizelimit", str(sizelimit)]
    cmd.append(raw_path)
    result = _run(cmd)
    loop_dev = result.stdout.strip()
    logger.info(f"  Loop device: {loop_dev}")
    return loop_dev


def _teardown_loop(loop_dev: str):
    """Detach a loop device."""
    _run(["losetup", "--detach", loop_dev], check=False)


def convert_bios_to_uefi(qcow2_path: str, os_family: str = "linux") -> bool:
    """Convert a BIOS disk to UEFI boot. Returns True if conversion was done.

    Uses raw intermediate file for partition operations to avoid
    qemu-nbd bugs on QEMU 6.2 (Ubuntu 22.04).
    """
    boot_type = detect_boot_type(qcow2_path)
    logger.info(f"Detected boot type: {boot_type}")

    if boot_type == "uefi":
        logger.info("Disk already has UEFI boot — no conversion needed")
        return False

    if os_family == "windows":
        logger.warning("Windows BIOS→UEFI not supported in fallback mode")
        return False

    ESP_SIZE_MB = 200
    raw_path = qcow2_path + ".raw"

    try:
        # ── Phase 1: Convert to raw and do partition work ──
        logger.info("=== Phase 1: Partition operations on raw image ===")

        # Step 1: qcow2 → raw
        logger.info("Converting qcow2 → raw for partition operations...")
        _run(["qemu-img", "convert", "-f", "qcow2", "-O", "raw",
              qcow2_path, raw_path])

        # Step 2: Resize raw to add ESP space
        _run(["qemu-img", "resize", "-f", "raw", raw_path, f"+{ESP_SIZE_MB}M"])
        logger.info(f"Resized raw by +{ESP_SIZE_MB}MB")

        # Step 3: Fix GPT / convert MBR
        if boot_type == "bios-gpt":
            logger.info("Fixing GPT backup header...")
            _run(["sgdisk", "-e", raw_path])
        elif boot_type == "bios-mbr":
            logger.info("Converting MBR → GPT...")
            _run(["sgdisk", "--mbrtogpt", raw_path])

        # Step 4: Find last partition, create ESP
        result = _run(["sgdisk", "-p", raw_path])
        lines = [l for l in result.stdout.split('\n')
                 if l.strip() and l.strip()[0].isdigit()]
        if not lines:
            raise RuntimeError("No partitions found on disk")
        last_part = int(lines[-1].split()[0])
        new_part = last_part + 1
        logger.info(f"Last partition: {last_part}, creating ESP as partition {new_part}")

        _run([
            "sgdisk",
            f"-n{new_part}:0:+{ESP_SIZE_MB}M",
            f"-t{new_part}:EF00",
            f"-c{new_part}:EFI-System",
            raw_path,
        ])
        logger.info(f"Created ESP partition {new_part}")

        # Step 5: Format ESP as FAT32 via losetup
        # Get partition offset and size from sgdisk output
        result = _run(["sgdisk", "-i", str(new_part), raw_path])
        part_start = None
        part_size = None
        for line in result.stdout.split('\n'):
            if "First sector:" in line:
                part_start = int(line.split(":")[1].strip().split()[0])
            if "Partition size:" in line:
                part_size = int(line.split(":")[1].strip().split()[0])

        if part_start is None or part_size is None:
            raise RuntimeError(f"Could not determine ESP partition geometry from sgdisk -i output")

        sector_size = 512
        offset = part_start * sector_size
        sizelimit = part_size * sector_size

        logger.info(f"ESP partition: start={part_start} sectors, size={part_size} sectors, "
                     f"offset={offset} bytes, sizelimit={sizelimit} bytes")

        loop_dev = _setup_loop(raw_path, offset=offset, sizelimit=sizelimit)
        try:
            logger.info(f"Formatting {loop_dev} as FAT32...")
            _run(["mkfs.vfat", "-F", "32", "-n", "ESP", loop_dev])
        finally:
            _teardown_loop(loop_dev)

        # Step 6: Convert raw → qcow2 (uncompressed — virt-customize needs it)
        logger.info("Converting raw → qcow2 (uncompressed)...")
        qcow2_new = qcow2_path + ".new"
        _run(["qemu-img", "convert", "-f", "raw", "-O", "qcow2",
              raw_path, qcow2_new])

        # Replace original
        Path(raw_path).unlink()
        Path(qcow2_path).unlink()
        shutil.move(qcow2_new, qcow2_path)
        logger.info("Raw → qcow2 conversion done, original replaced")

    except Exception:
        # Cleanup on failure
        for p in [raw_path, qcow2_path + ".new"]:
            if Path(p).exists():
                Path(p).unlink(missing_ok=True)
        raise

    # ── Phase 2: Install GRUB EFI inside guest ──
    logger.info("=== Phase 2: Guest-side GRUB EFI installation ===")

    grub_script = _build_grub_efi_script(new_part)

    _run([
        "virt-customize", "-a", qcow2_path,
        "--install", "grub-efi-amd64,grub-efi-amd64-bin,dosfstools",
        "--run-command", grub_script,
    ])

    logger.info("BIOS → UEFI conversion complete")
    return True


def _build_grub_efi_script(esp_part_num: int) -> str:
    """Build script to install GRUB EFI inside the guest."""
    return f'''#!/bin/bash
set -e
echo "=== Installing GRUB EFI ==="

# Find the ESP partition
DISK="/dev/sda"
ESP_DEV="${{DISK}}{esp_part_num}"
if [ ! -b "$ESP_DEV" ]; then
    ESP_DEV="${{DISK}}p{esp_part_num}"
fi
echo "ESP device: $ESP_DEV"

# Mount ESP
mkdir -p /boot/efi
mount "$ESP_DEV" /boot/efi

# Add to fstab
ESP_UUID=$(blkid -o value -s UUID "$ESP_DEV")
if [ -n "$ESP_UUID" ]; then
    sed -i '\\|/boot/efi|d' /etc/fstab
    echo "UUID=$ESP_UUID /boot/efi vfat umask=0077 0 1" >> /etc/fstab
fi

# Install GRUB EFI
grub-install --target=x86_64-efi --efi-directory=/boot/efi --bootloader-id=ubuntu --recheck --no-floppy 2>&1 || \\
grub-install --target=x86_64-efi --efi-directory=/boot/efi --bootloader-id=BOOT --recheck --no-floppy 2>&1 || {{
    echo "grub-install failed, manual EFI setup..."
    mkdir -p /boot/efi/EFI/BOOT
    cp /usr/lib/grub/x86_64-efi/monolithic/grubx64.efi /boot/efi/EFI/BOOT/BOOTX64.EFI 2>/dev/null || true
}}

# Create fallback EFI boot path
mkdir -p /boot/efi/EFI/BOOT
if [ -f /boot/efi/EFI/ubuntu/grubx64.efi ]; then
    cp /boot/efi/EFI/ubuntu/grubx64.efi /boot/efi/EFI/BOOT/BOOTX64.EFI
elif [ -f /boot/efi/EFI/ubuntu/shimx64.efi ]; then
    cp /boot/efi/EFI/ubuntu/shimx64.efi /boot/efi/EFI/BOOT/BOOTX64.EFI
fi

# Enable serial console for Scaleway
if [ -f /etc/default/grub ]; then
    sed -i 's/^GRUB_CMDLINE_LINUX_DEFAULT=.*/GRUB_CMDLINE_LINUX_DEFAULT="console=tty1 console=ttyS0,115200n8"/' /etc/default/grub
fi

# Regenerate GRUB config
grub-mkconfig -o /boot/grub/grub.cfg 2>/dev/null || true

umount /boot/efi 2>/dev/null || true
echo "=== GRUB EFI installation complete ==="
'''

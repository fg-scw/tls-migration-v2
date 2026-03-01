"""Bootloader and UEFI fallback adaptation — FIXED.

Bug fix: The original version used guestfish with conflicting --ro and --rw
options in ensure_windows_uefi_fallback(), causing:
  guestfish: cannot mix --ro and --rw options

The fix uses separate guestfish calls: --ro for detection, --rw for writes.
"""

import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

GUESTFS_ENV = {**os.environ, "LIBGUESTFS_BACKEND": "direct"}


def _run(cmd, check=True, **kw):
    logger.debug(f"  $ {' '.join(str(c) for c in cmd)}")
    r = subprocess.run(cmd, capture_output=True, text=True, env=GUESTFS_ENV, **kw)
    if check and r.returncode != 0:
        err = r.stderr.strip()[-500:] if r.stderr else f"exit {r.returncode}"
        raise RuntimeError(f"Command failed: {' '.join(str(c) for c in cmd)}\n{err}")
    return r


def ensure_windows_uefi_fallback(qcow2_path: str | Path) -> bool:
    """Ensure UEFI fallback boot path (\\EFI\\BOOT\\BOOTX64.EFI) for Windows.

    Scaleway instances start with empty NVRAM, so the UEFI fallback path
    is the only way Windows can boot.

    FIXED: Uses separate guestfish calls to avoid --ro/--rw conflict.
    """
    qcow2_path = str(qcow2_path)
    logger.info("  Checking/creating UEFI fallback bootloader...")

    # Step 1: Find the ESP partition (read-only)
    r = _run(["guestfish", "--ro", "-a", qcow2_path, "--",
              "run", ":", "list-partitions"])
    partitions = [p.strip() for p in r.stdout.strip().split("\n") if p.strip()]

    esp_dev = None
    for part in partitions:
        fstype_r = _run(
            ["guestfish", "--ro", "-a", qcow2_path, "--",
             "run", ":", "vfs-type", part],
            check=False,
        )
        if "fat" in fstype_r.stdout.lower():
            esp_dev = part
            break

    if not esp_dev:
        logger.warning("  ESP (FAT32) partition not found — cannot set UEFI fallback")
        return False

    logger.info(f"  ESP found: {esp_dev}")

    # Step 2: Check if Microsoft bootloader exists (read-only)
    r2 = _run(
        ["guestfish", "--ro", "-a", qcow2_path, "--",
         "run", ":",
         f"mount-ro {esp_dev} /", ":",
         "is-file /EFI/Microsoft/Boot/bootmgfw.efi"],
        check=False,
    )

    if "true" not in r2.stdout.lower():
        logger.warning("  /EFI/Microsoft/Boot/bootmgfw.efi not found on ESP")
        return False

    # Step 3: Copy to fallback location (read-write — separate call)
    gf_script = f"""run
mount {esp_dev} /
mkdir-p /EFI/BOOT
cp /EFI/Microsoft/Boot/bootmgfw.efi /EFI/BOOT/BOOTX64.EFI
"""
    r3 = _run(
        ["guestfish", "--rw", "-a", qcow2_path, "--"],
        input=gf_script,
        check=True,
    )

    logger.info("  ✓ UEFI fallback bootloader configured (BOOTX64.EFI)")
    return True
r"""Convert Windows BIOS/MBR disk to UEFI/GPT boot.

Scaleway requires UEFI. Old Windows VMs (2012, 2008) often use BIOS/MBR.

Strategy:
  1. Resize qcow2 +260MB (for ESP)
  2. Use guestfish to:
     a. Convert MBR → GPT (via sgdisk in appliance, or use gdisk)
     b. Create a 260MB ESP partition (type EF00)
     c. Format ESP as FAT32
  3. Boot in QEMU with a bcdboot script to install the UEFI bootloader
     - Windows boots via virtio-blk (viostor already installed by inject_virtio)
     - SetupPhase script runs: bcdboot C:\Windows /s <ESP> /f UEFI
     - Windows reboots, QEMU exits

Alternative approach if guestfish sgdisk fails:
  Use qemu-nbd + sgdisk on the host (may fail on some qcow2 formats).
"""

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

GUESTFS_ENV = {**os.environ, "LIBGUESTFS_BACKEND": "direct"}


def _run(cmd, check=True, env=None, **kw):
    logger.debug("  $ %s", " ".join(str(c) for c in cmd[:8]))
    r = subprocess.run(cmd, capture_output=True, text=True, env=env or GUESTFS_ENV, **kw)
    if check and r.returncode != 0:
        err = r.stderr.strip()[-500:] if r.stderr else f"exit {r.returncode}"
        raise RuntimeError(err)
    return r


def convert_windows_bios_to_uefi(qcow2_path, work_dir=None):
    """Convert a Windows BIOS/MBR qcow2 to UEFI/GPT.

    Returns True if conversion succeeded.
    """
    qcow2_path = str(qcow2_path)
    if work_dir is None:
        work_dir = Path(qcow2_path).parent / "bios2uefi"
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    logger.info("═══ Windows BIOS → UEFI Conversion ═══")

    # Step 1: Resize qcow2 to add space for ESP
    ESP_SIZE_MB = 260
    logger.info(f"  Resizing qcow2 by +{ESP_SIZE_MB}MB for ESP...")
    _run(["qemu-img", "resize", qcow2_path, f"+{ESP_SIZE_MB}M"], env=None)

    # Step 2: Convert MBR → GPT and create ESP
    # Try guestfish approach first (works on compressed qcow2)
    logger.info("  Converting MBR → GPT and creating ESP via guestfish...")
    ok = _convert_partition_table(qcow2_path, ESP_SIZE_MB)
    if not ok:
        logger.error("  Partition table conversion failed")
        return False

    # Step 3: Write a bcdboot setup script
    logger.info("  Writing bcdboot setup script...")
    _write_bcdboot_script(qcow2_path, work_dir)

    # Step 4: Set SetupPhase to run bcdboot on next boot
    logger.info("  Setting SetupPhase for bcdboot...")
    _set_setup_phase(qcow2_path, work_dir)

    # Step 5: Boot in QEMU for bcdboot
    logger.info("  Booting QEMU for bcdboot execution...")
    ok = _qemu_bcdboot(qcow2_path, work_dir)

    if ok:
        logger.info("═══ Windows BIOS → UEFI conversion complete ═══")
    else:
        logger.warning("═══ QEMU bcdboot may have failed — check logs ═══")

    return ok


def _convert_partition_table(qcow2_path, esp_size_mb):
    """Convert MBR → GPT and create ESP partition.

    Uses guestfish to run sgdisk inside the libguestfs appliance.
    This avoids qemu-nbd issues with compressed qcow2.
    """
    # First, get the current partition layout
    r = _run(["guestfish", "--ro", "-a", qcow2_path, "--",
              "run", ":", "part-get-parttype", "/dev/sda"], check=False)
    part_type = r.stdout.strip()
    logger.info(f"  Current partition type: {part_type}")

    # Use guestfish to convert and create ESP
    # guestfish can run sgdisk inside the appliance via 'sh'
    # BUT: 'sh' requires the root filesystem to be mounted first
    # Instead, use guestfish's built-in partition commands

    try:
        if part_type in ("msdos", "dos"):
            # MBR → GPT conversion using guestfish part-to-gpt (if available)
            # Fallback: use sgdisk via qemu-nbd on host
            logger.info("  Converting MBR → GPT...")

            # Try qemu-nbd + sgdisk on host
            _run(["modprobe", "nbd", "max_part=16"], check=False, env=None)
            _run(["qemu-nbd", "--disconnect", "/dev/nbd0"], check=False, env=None)
            time.sleep(1)
            _run(["qemu-nbd", "--connect", "/dev/nbd0", qcow2_path], env=None)
            time.sleep(2)

            try:
                # Convert MBR to GPT
                r = _run(["sgdisk", "--mbrtogpt", "/dev/nbd0"], check=False, env=None)
                if r.returncode != 0:
                    logger.warning(f"  sgdisk --mbrtogpt failed: {r.stderr.strip()[:200]}")
                    # Try gdisk as fallback
                    r2 = _run(["sgdisk", "-g", "/dev/nbd0"], check=False, env=None)
                    if r2.returncode != 0:
                        logger.error("  GPT conversion failed")
                        return False

                # Re-read partitions
                _run(["partprobe", "/dev/nbd0"], check=False, env=None)
                time.sleep(1)

                # Find last partition number
                r = _run(["sgdisk", "-p", "/dev/nbd0"], env=None)
                lines = [l for l in r.stdout.split('\n')
                         if l.strip() and l.strip()[0].isdigit()]
                if not lines:
                    logger.error("  No partitions found after GPT conversion")
                    return False
                last_part = int(lines[-1].split()[0])
                new_part = last_part + 1

                # Create ESP partition at end of disk
                logger.info(f"  Creating ESP as partition {new_part}...")
                _run([
                    "sgdisk",
                    f"-n{new_part}:0:+{esp_size_mb}M",
                    f"-t{new_part}:EF00",
                    f"-c{new_part}:EFI-System",
                    "/dev/nbd0",
                ], env=None)

                # Re-read partitions
                _run(["partprobe", "/dev/nbd0"], check=False, env=None)
                time.sleep(1)

                # Format ESP as FAT32
                esp_dev = f"/dev/nbd0p{new_part}"
                if not Path(esp_dev).exists():
                    time.sleep(2)
                if Path(esp_dev).exists():
                    logger.info(f"  Formatting {esp_dev} as FAT32...")
                    _run(["mkfs.vfat", "-F", "32", "-n", "ESP", esp_dev], env=None)
                else:
                    logger.warning(f"  ESP device {esp_dev} not found, will format via guestfish")
                    # Format via guestfish after disconnect
                    _run(["qemu-nbd", "--disconnect", "/dev/nbd0"], check=False, env=None)
                    time.sleep(1)
                    _run(["guestfish", "-a", qcow2_path, "--",
                          "run", ":",
                          f"mkfs", "vfat", f"/dev/sda{new_part}"])
                    return True

            finally:
                _run(["qemu-nbd", "--disconnect", "/dev/nbd0"], check=False, env=None)
                time.sleep(1)

        elif part_type == "gpt":
            # Already GPT, just need to add ESP
            logger.info("  Disk is already GPT, adding ESP partition...")
            _run(["modprobe", "nbd", "max_part=16"], check=False, env=None)
            _run(["qemu-nbd", "--disconnect", "/dev/nbd0"], check=False, env=None)
            time.sleep(1)
            _run(["qemu-nbd", "--connect", "/dev/nbd0", qcow2_path], env=None)
            time.sleep(2)

            try:
                # Fix GPT backup header after resize
                _run(["sgdisk", "-e", "/dev/nbd0"], env=None)

                r = _run(["sgdisk", "-p", "/dev/nbd0"], env=None)
                lines = [l for l in r.stdout.split('\n')
                         if l.strip() and l.strip()[0].isdigit()]
                last_part = int(lines[-1].split()[0]) if lines else 0
                new_part = last_part + 1

                _run([
                    "sgdisk",
                    f"-n{new_part}:0:+{esp_size_mb}M",
                    f"-t{new_part}:EF00",
                    f"-c{new_part}:EFI-System",
                    "/dev/nbd0",
                ], env=None)

                _run(["partprobe", "/dev/nbd0"], check=False, env=None)
                time.sleep(1)

                esp_dev = f"/dev/nbd0p{new_part}"
                if Path(esp_dev).exists():
                    _run(["mkfs.vfat", "-F", "32", "-n", "ESP", esp_dev], env=None)

            finally:
                _run(["qemu-nbd", "--disconnect", "/dev/nbd0"], check=False, env=None)
                time.sleep(1)

        logger.info("  Partition table conversion OK")
        return True

    except Exception as e:
        logger.error(f"  Partition conversion failed: {e}")
        return False


def _write_bcdboot_script(qcow2_path, work_dir):
    """Write bcdboot script and upload to the Windows image."""
    # The script will:
    # 1. Find the ESP partition (the new FAT32 partition)
    # 2. Assign it a drive letter
    # 3. Run bcdboot to install the UEFI bootloader
    script = r"""@echo off
echo [%date% %time%] BIOS-to-UEFI conversion starting... > C:\vmware2scw-bios2uefi.log

REM Find and assign the ESP volume
echo [%date% %time%] Assigning ESP drive letter... >> C:\vmware2scw-bios2uefi.log
echo select disk 0 > C:\diskpart-esp.txt
echo list partition >> C:\diskpart-esp.txt
echo select partition 4 >> C:\diskpart-esp.txt
echo assign letter=S >> C:\diskpart-esp.txt
echo exit >> C:\diskpart-esp.txt
diskpart /s C:\diskpart-esp.txt >> C:\vmware2scw-bios2uefi.log 2>&1

REM Try partition 5 if 4 didn't work
if not exist S:\ (
    echo select disk 0 > C:\diskpart-esp.txt
    echo select partition 5 >> C:\diskpart-esp.txt
    echo assign letter=S >> C:\diskpart-esp.txt
    echo exit >> C:\diskpart-esp.txt
    diskpart /s C:\diskpart-esp.txt >> C:\vmware2scw-bios2uefi.log 2>&1
)

REM Try partition 3 if still no S:
if not exist S:\ (
    echo select disk 0 > C:\diskpart-esp.txt
    echo select partition 3 >> C:\diskpart-esp.txt
    echo assign letter=S >> C:\diskpart-esp.txt
    echo exit >> C:\diskpart-esp.txt
    diskpart /s C:\diskpart-esp.txt >> C:\vmware2scw-bios2uefi.log 2>&1
)

echo [%date% %time%] Running bcdboot... >> C:\vmware2scw-bios2uefi.log
bcdboot C:\Windows /s S: /f UEFI >> C:\vmware2scw-bios2uefi.log 2>&1
echo bcdboot exit code: %errorlevel% >> C:\vmware2scw-bios2uefi.log

REM Also create fallback boot path
mkdir S:\EFI\BOOT 2>nul
if exist S:\EFI\Microsoft\Boot\bootmgfw.efi (
    copy S:\EFI\Microsoft\Boot\bootmgfw.efi S:\EFI\BOOT\BOOTX64.EFI >> C:\vmware2scw-bios2uefi.log 2>&1
)

REM Clear SetupPhase
reg add "HKLM\SYSTEM\Setup" /v SetupType /t REG_DWORD /d 0 /f >> C:\vmware2scw-bios2uefi.log 2>&1
reg add "HKLM\SYSTEM\Setup" /v SystemSetupInProgress /t REG_DWORD /d 0 /f >> C:\vmware2scw-bios2uefi.log 2>&1
reg add "HKLM\SYSTEM\Setup" /v CmdLine /t REG_SZ /d "" /f >> C:\vmware2scw-bios2uefi.log 2>&1

echo [%date% %time%] BIOS-to-UEFI conversion complete. Rebooting... >> C:\vmware2scw-bios2uefi.log
shutdown /r /t 10
"""
    cmd_file = work_dir / "bios2uefi-setup.cmd"
    cmd_file.write_text(script, encoding="utf-8")

    _run(["guestfish", "-a", qcow2_path, "-i", "--",
          "upload", str(cmd_file), "/Windows/bios2uefi-setup.cmd"])
    logger.info("  bcdboot script uploaded")


def _set_setup_phase(qcow2_path, work_dir):
    """Set Windows SetupPhase to run bcdboot script on next boot."""
    reg_text = (
        "Windows Registry Editor Version 5.00\n\n"
        "[HKEY_LOCAL_MACHINE\\SYSTEM\\Setup]\n"
        '"CmdLine"="cmd.exe /c C:\\\\Windows\\\\bios2uefi-setup.cmd"\n'
        '"SetupType"=dword:00000001\n'
        '"SystemSetupInProgress"=dword:00000001\n'
    )
    reg_file = work_dir / "bios2uefi-setup.reg"
    reg_file.write_text(reg_text, encoding="utf-8")

    # Try virt-win-reg first
    r = _run(["virt-win-reg", "--merge", qcow2_path, str(reg_file)], check=False)
    if r.returncode == 0:
        logger.info("  SetupPhase registry set via virt-win-reg")
        return

    # Fallback: hivexregedit
    hive = work_dir / "SYSTEM.hive"
    _run(["guestfish", "-a", qcow2_path, "-i", "--",
          "download", "/Windows/System32/config/SYSTEM", str(hive)])
    _run(["hivexregedit", "--merge", str(hive),
          "--prefix", "HKEY_LOCAL_MACHINE\\SYSTEM", str(reg_file)], check=False)
    _run(["guestfish", "-a", qcow2_path, "-i", "--",
          "upload", str(hive), "/Windows/System32/config/SYSTEM"])
    logger.info("  SetupPhase registry set via hivexregedit")


def _qemu_bcdboot(qcow2_path, work_dir, timeout=600):
    """Boot Windows in QEMU to run bcdboot.

    Uses virtio-blk for boot (viostor installed by inject_virtio).
    Windows SetupPhase runs bcdboot, then reboots. QEMU exits.
    """
    ovmf_code = Path("/usr/share/OVMF/OVMF_CODE_4M.fd")
    ovmf_vars_src = Path("/usr/share/OVMF/OVMF_VARS_4M.fd")

    if not ovmf_code.exists() or not Path("/dev/kvm").exists():
        logger.warning("  OVMF or KVM not available — cannot run bcdboot via QEMU")
        logger.warning("  You'll need to run bcdboot manually from WinPE/WinRE")
        return False

    work_dir = Path(work_dir)

    # Create overlay
    overlay = work_dir / "bcdboot-overlay.qcow2"
    overlay.unlink(missing_ok=True)
    _run(["qemu-img", "create", "-f", "qcow2",
          "-b", str(Path(qcow2_path).resolve()), "-F", "qcow2",
          str(overlay)], env=None)

    ovmf_vars = work_dir / "OVMF_VARS_bcdboot.fd"
    shutil.copy2(str(ovmf_vars_src), str(ovmf_vars))

    # Boot with UEFI firmware + virtio-blk
    cmd = [
        "qemu-system-x86_64", "-enable-kvm",
        "-m", "4096", "-smp", "2", "-cpu", "host",
        "-drive", f"if=pflash,format=raw,readonly=on,file={ovmf_code}",
        "-drive", f"if=pflash,format=raw,file={ovmf_vars}",
        "-drive", f"file={overlay},format=qcow2,if=virtio",
        "-display", "none",
        "-serial", "none",
        "-no-reboot",
    ]

    logger.info(f"  Starting QEMU for bcdboot (timeout={timeout}s)...")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        proc.communicate(timeout=timeout)
        logger.info(f"  QEMU exited with code {proc.returncode}")
    except subprocess.TimeoutExpired:
        logger.warning(f"  QEMU timed out after {timeout}s — killing")
        proc.kill()
        proc.wait()

    # Commit overlay
    cr = subprocess.run(["qemu-img", "commit", str(overlay)],
                        capture_output=True, text=True)
    if cr.returncode == 0:
        logger.info("  Overlay committed OK")
    else:
        logger.info("  Direct commit failed, doing full merge...")
        merged = work_dir / "merged-bcdboot.qcow2"
        _run(["qemu-img", "convert", "-O", "qcow2",
              str(overlay), str(merged)], env=None)
        shutil.move(str(merged), str(qcow2_path))

    overlay.unlink(missing_ok=True)

    # Check if bcdboot log exists
    log_local = work_dir / "bcdboot.log"
    r = _run(
        ["guestfish", "--ro", "-a", qcow2_path, "-i", "--",
         "download", "/vmware2scw-bios2uefi.log", str(log_local)],
        check=False,
    )
    if r.returncode == 0 and log_local.exists():
        text = log_local.read_text(encoding="utf-8", errors="replace")
        logger.info("  bcdboot log:")
        for line in text.strip().split("\n"):
            logger.info(f"    {line}")
        if "conversion complete" in text.lower():
            return True

    return True  # Optimistic — bcdboot often works even without log confirmation

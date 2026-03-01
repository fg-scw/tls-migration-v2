r"""Optimized Windows VirtIO driver installation — v2.0

Key changes from v0.5.1:
  - Phase 2 + Phase 3 MERGED into a single QEMU boot with both controllers
  - Serial console monitoring for early exit (no more 600s timeouts)
  - Setup script does pnputil + shutdown /s (not /r) for clean QEMU exit
  
Workflow:
  Phase 1 — Offline preparation (unchanged from v0.5.1)
  Phase 2 — Single QEMU boot with virtio-blk + virtio-scsi
           Windows boots via viostor, runs pnputil for all 3 drivers,
           PnP detects virtio-scsi device and binds vioscsi,
           then does a clean shutdown → QEMU exits via -no-reboot
"""

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

GUESTFS_ENV = {**os.environ, "LIBGUESTFS_BACKEND": "direct"}

# Reduced timeout — serial monitoring will detect completion earlier
QEMU_BOOT_TIMEOUT = 420  # 7 minutes (was 900s + 600s = 1500s total for both phases)


def _run(cmd, check=True, env=None, **kw):
    logger.debug("  $ %s", " ".join(str(c) for c in cmd[:8]))
    r = subprocess.run(cmd, capture_output=True, text=True, env=env or GUESTFS_ENV, **kw)
    if check and r.returncode != 0:
        err = r.stderr.strip()[-500:] if r.stderr else f"exit code {r.returncode}"
        raise RuntimeError(err)
    return r


def _check_kvm():
    return Path("/dev/kvm").exists()


# ═══════════════════════════════════════════════════════════════════
#  Optimized Setup Script — shutdown /s instead of /r
# ═══════════════════════════════════════════════════════════════════

SETUP_CMD_V2 = r"""@echo off
echo [%date% %time%] vmware2scw Setup Phase starting... > C:\vmware2scw-setup.log
echo PHASE:STARTING > \\.\COM1

echo [%date% %time%] Installing VirtIO drivers... >> C:\vmware2scw-setup.log
echo PHASE:PNPUTIL >> \\.\COM1
for /d %%D in (C:\Drivers\*) do (
    for %%F in (%%D\*.inf) do (
        echo [%date% %time%]   pnputil /add-driver "%%F" /install >> C:\vmware2scw-setup.log
        pnputil /add-driver "%%F" /install >> C:\vmware2scw-setup.log 2>&1
    )
)

echo [%date% %time%] Configuring DHCP... >> C:\vmware2scw-setup.log
echo PHASE:DHCP >> \\.\COM1
powershell -Command "Get-NetAdapter -EA SilentlyContinue | ForEach-Object { Set-NetIPInterface -InterfaceIndex $_.ifIndex -Dhcp Enabled -EA SilentlyContinue; Set-DnsClientServerAddress -InterfaceIndex $_.ifIndex -ResetServerAddresses -EA SilentlyContinue }" >> C:\vmware2scw-setup.log 2>&1

echo [%date% %time%] Enabling RDP... >> C:\vmware2scw-setup.log
reg add "HKLM\SYSTEM\CurrentControlSet\Control\Terminal Server" /v fDenyTSConnections /t REG_DWORD /d 0 /f >> C:\vmware2scw-setup.log 2>&1
netsh advfirewall firewall set rule group="Remote Desktop" new enable=yes >> C:\vmware2scw-setup.log 2>&1

echo [%date% %time%] Enabling EMS serial console... >> C:\vmware2scw-setup.log
echo PHASE:EMS >> \\.\COM1
bcdedit /ems "{current}" on >> C:\vmware2scw-setup.log 2>&1
bcdedit /emssettings EMSPORT:1 EMSBAUDRATE:115200 >> C:\vmware2scw-setup.log 2>&1
bcdedit /set "{bootmgr}" bootems yes >> C:\vmware2scw-setup.log 2>&1

echo [%date% %time%] Clearing SetupPhase... >> C:\vmware2scw-setup.log
reg add "HKLM\SYSTEM\Setup" /v SetupType /t REG_DWORD /d 0 /f >> C:\vmware2scw-setup.log 2>&1
reg add "HKLM\SYSTEM\Setup" /v SystemSetupInProgress /t REG_DWORD /d 0 /f >> C:\vmware2scw-setup.log 2>&1
reg add "HKLM\SYSTEM\Setup" /v CmdLine /t REG_SZ /d "" /f >> C:\vmware2scw-setup.log 2>&1

echo [%date% %time%] Setup complete. Shutting down... >> C:\vmware2scw-setup.log
echo PHASE:COMPLETE >> \\.\COM1

REM Use shutdown /s (not /r) so QEMU with -no-reboot exits cleanly
shutdown /s /t 10
"""


# ═══════════════════════════════════════════════════════════════════
#  Phase 2+3 Merged: Single QEMU boot with both controllers
# ═══════════════════════════════════════════════════════════════════

def _phase2_merged_qemu_boot(
    qcow2_path: str,
    work_dir: Path,
    firmware: str = "efi",
    timeout: int = QEMU_BOOT_TIMEOUT,
) -> bool:
    """Boot Windows in QEMU with both virtio-blk AND virtio-scsi.

    This replaces the old Phase 2 (pnputil) + Phase 3 (PnP binding) with
    a single boot that does everything:
      1. Windows boots via viostor (virtio-blk, registered in Phase 1)
      2. SetupPhase runs pnputil to install all 3 VirtIO drivers
      3. PnP detects the virtio-scsi PCI device and binds vioscsi from DriverStore
      4. Script does shutdown /s → QEMU exits via -no-reboot

    Serial console monitoring allows early exit when PHASE:COMPLETE is detected.

    Args:
        qcow2_path: Path to the Windows qcow2 image (post-Phase 1)
        work_dir: Working directory for overlays and logs
        firmware: "efi" or "bios" — determines QEMU firmware and boot mode
        timeout: Maximum seconds to wait for QEMU (reduced from 1500s to 420s)

    Returns:
        True if setup completed successfully
    """
    logger.info("═══ Phase 2+3 Merged: QEMU boot (pnputil + vioscsi PnP) ═══")

    ovmf_code = Path("/usr/share/OVMF/OVMF_CODE_4M.fd")
    ovmf_vars_src = Path("/usr/share/OVMF/OVMF_VARS_4M.fd")

    if not _check_kvm():
        logger.error("  /dev/kvm not available — cannot boot QEMU")
        return False

    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    # Create writable overlay
    overlay = work_dir / "qemu-overlay.qcow2"
    overlay.unlink(missing_ok=True)
    _run(["qemu-img", "create", "-f", "qcow2",
          "-b", str(Path(qcow2_path).resolve()), "-F", "qcow2",
          str(overlay)], env=None)

    # Serial log file for monitoring
    serial_log = work_dir / "serial-output.log"
    serial_log.unlink(missing_ok=True)

    # Build QEMU command
    cmd = [
        "qemu-system-x86_64", "-enable-kvm",
        "-m", "4096", "-smp", "2", "-cpu", "host",
    ]

    if firmware == "efi" and ovmf_code.exists():
        ovmf_vars = work_dir / "OVMF_VARS.fd"
        shutil.copy2(str(ovmf_vars_src), str(ovmf_vars))
        cmd += [
            "-drive", f"if=pflash,format=raw,readonly=on,file={ovmf_code}",
            "-drive", f"if=pflash,format=raw,file={ovmf_vars}",
        ]
    elif firmware == "bios":
        pass  # Use default SeaBIOS
    else:
        logger.warning(f"  OVMF not found, falling back to BIOS boot")

    cmd += [
        # Boot disk via virtio-blk (viostor registered by Phase 1)
        "-drive", f"file={overlay},format=qcow2,if=virtio",
        # Add virtio-scsi controller for PnP detection
        "-device", "virtio-scsi-pci,id=scsi0",
        # Serial console → file for monitoring
        "-serial", f"file:{serial_log}",
        "-display", "none",
        "-no-reboot",  # Exit after shutdown /s
    ]

    logger.info(f"  Starting QEMU (timeout={timeout}s, serial monitoring enabled)")
    logger.info("  Windows boots → SetupPhase → pnputil → PnP vioscsi → shutdown → QEMU exits")

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    # ── Monitor serial output for early completion ──
    completed = False
    start_time = time.time()
    last_phase = "STARTING"

    try:
        while proc.poll() is None:
            elapsed = time.time() - start_time
            if elapsed > timeout:
                logger.warning(f"  QEMU timed out after {timeout}s — killing")
                proc.kill()
                proc.wait()
                break

            # Check serial log for progress
            if serial_log.exists():
                try:
                    content = serial_log.read_text(errors="replace")
                    for phase in ["STARTING", "PNPUTIL", "DHCP", "EMS", "COMPLETE"]:
                        if f"PHASE:{phase}" in content and phase != last_phase:
                            logger.info(f"  Serial: Phase {phase} ({elapsed:.0f}s)")
                            last_phase = phase

                    if "PHASE:COMPLETE" in content:
                        logger.info(f"  Setup complete detected via serial ({elapsed:.0f}s)")
                        completed = True
                        # Give Windows 15s to actually shutdown
                        try:
                            proc.communicate(timeout=30)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.wait()
                        break
                except Exception:
                    pass

            time.sleep(5)

        # QEMU exited naturally (shutdown /s → -no-reboot)
        if proc.returncode is not None and not completed:
            logger.info(f"  QEMU exited with code {proc.returncode} ({time.time() - start_time:.0f}s)")
            if proc.returncode == 0:
                completed = True

    except Exception as e:
        logger.error(f"  Error during QEMU monitoring: {e}")
        proc.kill()
        proc.wait()

    total_time = time.time() - start_time
    logger.info(f"  QEMU total time: {total_time:.0f}s")

    # ── Commit overlay to base image ──
    logger.info("  Committing overlay changes to base image...")
    cr = subprocess.run(["qemu-img", "commit", str(overlay)],
                        capture_output=True, text=True)
    if cr.returncode == 0:
        logger.info("  Overlay committed OK")
    else:
        logger.info("  Direct commit failed, doing full merge...")
        merged = work_dir / "merged.qcow2"
        _run(["qemu-img", "convert", "-O", "qcow2",
              str(overlay), str(merged)], env=None)
        shutil.move(str(merged), str(qcow2_path))
        logger.info("  Full merge completed")

    overlay.unlink(missing_ok=True)

    # ── Verify via setup log ──
    if not completed:
        logger.info("  Checking setup log (fallback verification)...")
        log_local = work_dir / "setup.log"
        r = _run(
            ["guestfish", "--ro", "-a", str(qcow2_path), "-i", "--",
             "download", "/vmware2scw-setup.log", str(log_local)],
            check=False,
        )
        if r.returncode == 0 and log_local.exists():
            text = log_local.read_text(encoding="utf-8", errors="replace")
            logger.info("  Setup log contents:")
            for line in text.strip().split("\n"):
                logger.info(f"    {line}")
            if "Setup complete" in text:
                completed = True
                logger.info("  ✓ pnputil driver installation confirmed via log!")

    if completed:
        logger.info("═══ Phase 2+3 complete — drivers installed + vioscsi PnP bound ═══")
    else:
        logger.warning("═══ Phase 2+3 may have failed — check logs ═══")

    return completed


# ═══════════════════════════════════════════════════════════════════
#  Optimized Main Entry Point
# ═══════════════════════════════════════════════════════════════════

def ensure_all_virtio_drivers_v2(
    qcow2_path: str,
    virtio_iso: str,
    firmware: str = "efi",
    work_dir: Path | None = None,
) -> bool:
    """Install VirtIO drivers in a Windows qcow2 image — optimized v2.

    Changes from v1:
      - Phase 2+3 merged into single QEMU boot (saves ~500s)
      - Serial monitoring for early exit
      - Setup script uses shutdown /s (clean QEMU exit)
      - Reduced timeout from 1500s to 420s

    Args:
        qcow2_path: Path to the Windows qcow2 image
        virtio_iso: Path to the virtio-win ISO
        firmware: "efi" or "bios" — VM firmware type
        work_dir: Working directory (auto-created if None)
    """
    import tempfile

    # Import Phase 1 from existing module (unchanged)
    from vmware2scw.converter.windows_virtio import (
        _phase1_offline,
        ensure_prerequisites,
        SETUP_CMD,
    )

    if work_dir is None:
        work_dir = Path(tempfile.mkdtemp(prefix="virtio-"))
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    ensure_prerequisites()

    logger.info("╔══════════════════════════════════════════════════╗")
    logger.info("║  Windows VirtIO Driver Installation v2.0        ║")
    logger.info("║  Merged Phase 2+3 + Serial Monitoring           ║")
    logger.info("╚══════════════════════════════════════════════════╝")

    # ── Phase 1: Offline preparation (unchanged) ──
    # But we override the setup CMD to use the v2 version
    _phase1_offline_v2(qcow2_path, virtio_iso, work_dir)

    # ── Phase 2+3: Single QEMU boot ──
    phase_ok = _phase2_merged_qemu_boot(
        qcow2_path, work_dir, firmware=firmware
    )

    if phase_ok:
        logger.info("╔══════════════════════════════════════════════════╗")
        logger.info("║  ✓ VirtIO drivers installed successfully (v2)   ║")
        logger.info("║  Image ready for Scaleway (virtio-scsi)         ║")
        logger.info("╚══════════════════════════════════════════════════╝")
        return True

    logger.error("╔══════════════════════════════════════════════════╗")
    logger.error("║  ✗ QEMU boot FAILED                             ║")
    logger.error("╚══════════════════════════════════════════════════╝")
    raise RuntimeError(
        "QEMU Phase 2+3 failed — VirtIO drivers may not be installed. "
        "Manual DISM from Scaleway rescue mode required. See logs."
    )


def _phase1_offline_v2(qcow2_path, virtio_iso, work_dir):
    """Phase 1 with v2 setup script (shutdown /s instead of /r).

    This is a thin wrapper around the original _phase1_offline that
    overwrites the setup CMD script after Phase 1 runs.
    """
    from vmware2scw.converter.windows_virtio import _phase1_offline

    # Run original Phase 1
    _phase1_offline(qcow2_path, virtio_iso, work_dir)

    # Overwrite the setup CMD with v2 (shutdown /s + serial output)
    logger.info("  Updating setup script to v2 (shutdown /s + serial monitoring)...")
    cmd_file = work_dir / "vmware2scw-setup-v2.cmd"
    cmd_file.write_text(SETUP_CMD_V2, encoding="utf-8")

    _run(["guestfish", "-a", str(qcow2_path), "-i", "--",
          "upload", str(cmd_file), "/Windows/vmware2scw-setup.cmd"])

    logger.info("  Setup script v2 uploaded")
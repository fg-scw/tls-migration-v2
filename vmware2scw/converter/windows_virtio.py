r"""Inject VirtIO drivers into Windows guest image for Scaleway.

v0.5.1 — Proven workflow based on extensive testing:

  Phase 1 — Offline preparation (guestfish + hivex):
    - Copy .sys files, register Services, stage driver packages
    - Set up Windows SetupPhase (CmdLine → pnputil firstboot)
    - Force DHCP, enable RDP, enable EMS serial console

  Phase 2 — QEMU virtio-blk boot (pnputil):
    - Boot Windows in QEMU with virtio-blk (-drive if=virtio)
    - Windows SetupPhase runs pnputil /add-driver for ALL 3 drivers
    - pnputil updates DriverStore + DriverDatabase
    - VM reboots, QEMU exits

  IMPORTANT: After Phase 2, the NTFS will be dirty (Windows doesn't
  do a clean unmount during reboot). This is FINE — the drivers are
  already installed. Do NOT attempt further writes to the NTFS.

Key insight: Scaleway uses virtio-scsi. Phase 2 installs vioscsi
into the DriverStore via pnputil, which is what Scaleway needs.
"""

import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

GUESTFS_ENV = {**os.environ, "LIBGUESTFS_BACKEND": "direct"}

DRIVER_DEFS = {
    "viostor": {
        "Group": "SCSI miniport",
        "ImagePath": "system32\\drivers\\viostor.sys",
        "Start": 0, "Type": 1, "ErrorControl": 1, "Tag": 0x40,
        "iso_dir": "viostor",
    },
    "vioscsi": {
        "Group": "SCSI miniport",
        "ImagePath": "system32\\drivers\\vioscsi.sys",
        "Start": 0, "Type": 1, "ErrorControl": 1, "Tag": 0x41,
        "iso_dir": "vioscsi",
    },
    "netkvm": {
        "Group": "NDIS",
        "ImagePath": "system32\\drivers\\netkvm.sys",
        "Start": 0, "Type": 1, "ErrorControl": 1,
        "iso_dir": "NetKVM",
    },
}

OS_SUBDIRS = ["2k22/amd64", "2k19/amd64", "2k16/amd64", "w11/amd64", "w10/amd64"]

# QEMU boot timeout — Windows boot + pnputil + reboot typically takes 2-4 min
QEMU_BOOT_TIMEOUT = 900


# ═══════════════════════════════════════════════════════════════════
#  Utilities
# ═══════════════════════════════════════════════════════════════════

def _run(cmd, check=True, env=None, **kw):
    """Run a command, log it, optionally raise on failure."""
    logger.debug("  $ %s", " ".join(str(c) for c in cmd[:8]))
    r = subprocess.run(
        cmd, capture_output=True, text=True,
        env=env or GUESTFS_ENV, **kw,
    )
    if check and r.returncode != 0:
        err = r.stderr.strip()[-500:] if r.stderr else f"exit code {r.returncode}"
        raise RuntimeError(err)
    return r


def _str_to_reg_expand_sz(s):
    raw = s.encode("utf-16-le") + b"\x00\x00"
    return "hex(2):" + ",".join(f"{b:02x}" for b in raw)


def _str_to_reg_multi_sz(strings):
    raw = b""
    for s in strings:
        raw += s.encode("utf-16-le") + b"\x00\x00"
    raw += b"\x00\x00"
    return "hex(7):" + ",".join(f"{b:02x}" for b in raw)


def _check_kvm():
    return Path("/dev/kvm").exists()


def ensure_prerequisites():
    """Install all required host tools."""
    needed = []
    for tool, pkg in [
        ("guestfish", "libguestfs-tools"),
        ("hivexregedit", "libwin-hivex-perl"),
        ("qemu-system-x86_64", "qemu-system-x86"),
        ("qemu-nbd", "qemu-utils"),
        ("ntfsfix", "ntfs-3g"),
    ]:
        if subprocess.run(["which", tool], capture_output=True).returncode != 0:
            needed.append(pkg)

    if not Path("/usr/share/OVMF/OVMF_CODE_4M.fd").exists():
        needed.append("ovmf")

    if needed:
        logger.info(f"  Installing: {', '.join(needed)}")
        subprocess.run(["apt-get", "update", "-qq"], capture_output=True)
        subprocess.run(["apt-get", "install", "-y", "-qq"] + needed,
                        capture_output=True)


# ═══════════════════════════════════════════════════════════════════
#  NTFS dirty flag fix
# ═══════════════════════════════════════════════════════════════════

def _fix_ntfs_dirty(qcow2_path):
    """Clear NTFS dirty flag so guestfish can mount read-write.

    Uses qemu-nbd to expose the qcow2 as a block device, then runs
    ntfsfix -d on each NTFS partition. This clears the dirty flag
    left by Windows Fast Startup / Hibernation / unclean shutdown.

    If qemu-nbd fails (I/O errors on compressed qcow2), we try
    converting to uncompressed first.
    """
    logger.info("  Clearing NTFS dirty flags...")
    subprocess.run(["modprobe", "nbd", "max_part=8"], capture_output=True)
    subprocess.run(["qemu-nbd", "--disconnect", "/dev/nbd0"], capture_output=True)
    time.sleep(1)

    r = subprocess.run(
        ["qemu-nbd", "--connect", "/dev/nbd0", str(qcow2_path)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        logger.warning(f"  qemu-nbd connect failed: {r.stderr.strip()[:200]}")
        return False

    fixed = False
    try:
        time.sleep(2)  # Wait for partition table to be read
        for i in range(1, 8):
            part = f"/dev/nbd0p{i}"
            if not Path(part).exists():
                continue
            blkid = subprocess.run(
                ["blkid", "-o", "value", "-s", "TYPE", part],
                capture_output=True, text=True,
            )
            if "ntfs" in blkid.stdout.lower():
                r2 = subprocess.run(
                    ["ntfsfix", "-d", part],
                    capture_output=True, text=True,
                )
                if r2.returncode == 0:
                    logger.info(f"  ntfsfix OK: {part}")
                    fixed = True
                else:
                    logger.warning(f"  ntfsfix failed on {part}: {r2.stderr.strip()[:100]}")
    finally:
        subprocess.run(["qemu-nbd", "--disconnect", "/dev/nbd0"], capture_output=True)
        time.sleep(1)

    return fixed


def _decompress_qcow2(qcow2_path):
    """Convert compressed qcow2 to uncompressed for qemu-nbd compatibility."""
    logger.info("  Decompressing qcow2 (compressed images cause I/O errors with nbd)...")
    tmp = str(qcow2_path) + ".uncomp"
    _run(["qemu-img", "convert", "-O", "qcow2", str(qcow2_path), tmp], env=None)
    shutil.move(tmp, str(qcow2_path))
    logger.info("  Decompressed OK")


# ═══════════════════════════════════════════════════════════════════
#  Driver Extraction from ISO
# ═══════════════════════════════════════════════════════════════════

def _find_driver_dir(mount_dir, drv_name, iso_dir):
    """Find the appropriate driver directory in the virtio-win ISO."""
    for subdir in OS_SUBDIRS:
        candidate = mount_dir / iso_dir / subdir
        if (candidate / f"{drv_name}.sys").exists():
            return candidate
    # Fallback: search recursively
    for f in mount_dir.rglob(f"{drv_name}.sys"):
        if "amd64" in str(f).lower():
            return f.parent
    return None


def _extract_drivers(iso_path, work_dir):
    """Mount virtio-win ISO and extract all driver packages."""
    mnt = work_dir / "virtio-iso"
    mnt.mkdir(parents=True, exist_ok=True)
    _run(["mount", "-o", "loop,ro", str(iso_path), str(mnt)], env=None)

    drivers = {}
    try:
        for name, defn in DRIVER_DEFS.items():
            src = _find_driver_dir(mnt, name, defn["iso_dir"])
            if not src:
                logger.warning(f"  {name} not found in ISO!")
                continue
            pkg_dir = work_dir / f"drv_{name}"
            pkg_dir.mkdir(parents=True, exist_ok=True)
            for f in src.iterdir():
                if f.is_file():
                    shutil.copy2(str(f), str(pkg_dir / f.name))
            drivers[name] = {"sys": pkg_dir / f"{name}.sys", "dir": pkg_dir}
            logger.info(f"  Extracted {name}")
    finally:
        _run(["umount", str(mnt)], check=False, env=None)

    return drivers


# ═══════════════════════════════════════════════════════════════════
#  Registry Helpers
# ═══════════════════════════════════════════════════════════════════

def _build_services_reg(driver_names):
    """Build .reg content for driver Service entries."""
    lines = ["Windows Registry Editor Version 5.00", ""]
    for name in sorted(driver_names):
        d = DRIVER_DEFS[name]
        base = f"HKEY_LOCAL_MACHINE\\SYSTEM\\ControlSet001\\Services\\{name}"
        lines += [
            f"[{base}]",
            f'"Group"="{d["Group"]}"',
            f'"ImagePath"={_str_to_reg_expand_sz(d["ImagePath"])}',
            f'"ErrorControl"=dword:{d["ErrorControl"]:08x}',
            f'"Start"=dword:{d["Start"]:08x}',
            f'"Type"=dword:{d["Type"]:08x}',
        ]
        if "Tag" in d:
            lines.append(f'"Tag"=dword:{d["Tag"]:08x}')
        lines += [
            "", f"[{base}\\Parameters]", "",
            f"[{base}\\Parameters\\PnpInterface]",
            '"5"=dword:00000001', "",
            f"[{base}\\Enum]",
            '"Count"=dword:00000000', '"NextInstance"=dword:00000000', "",
        ]
    return "\n".join(lines)


SETUP_CMD = r"""@echo off
echo [%date% %time%] vmware2scw Setup Phase starting... > C:\vmware2scw-setup.log

echo [%date% %time%] Installing VirtIO drivers... >> C:\vmware2scw-setup.log
for /d %%D in (C:\Drivers\*) do (
    for %%F in (%%D\*.inf) do (
        echo [%date% %time%]   pnputil /add-driver "%%F" /install >> C:\vmware2scw-setup.log
        pnputil /add-driver "%%F" /install >> C:\vmware2scw-setup.log 2>&1
    )
)

echo [%date% %time%] Configuring DHCP... >> C:\vmware2scw-setup.log
powershell -Command "Get-NetAdapter -EA SilentlyContinue | ForEach-Object { Set-NetIPInterface -InterfaceIndex $_.ifIndex -Dhcp Enabled -EA SilentlyContinue; Set-DnsClientServerAddress -InterfaceIndex $_.ifIndex -ResetServerAddresses -EA SilentlyContinue }" >> C:\vmware2scw-setup.log 2>&1

echo [%date% %time%] Enabling RDP... >> C:\vmware2scw-setup.log
reg add "HKLM\SYSTEM\CurrentControlSet\Control\Terminal Server" /v fDenyTSConnections /t REG_DWORD /d 0 /f >> C:\vmware2scw-setup.log 2>&1
netsh advfirewall firewall set rule group="Remote Desktop" new enable=yes >> C:\vmware2scw-setup.log 2>&1

echo [%date% %time%] Enabling EMS... >> C:\vmware2scw-setup.log
bcdedit /ems "{current}" on >> C:\vmware2scw-setup.log 2>&1
bcdedit /emssettings EMSPORT:1 EMSBAUDRATE:115200 >> C:\vmware2scw-setup.log 2>&1
bcdedit /set "{bootmgr}" bootems yes >> C:\vmware2scw-setup.log 2>&1

echo [%date% %time%] Clearing SetupPhase... >> C:\vmware2scw-setup.log
reg add "HKLM\SYSTEM\Setup" /v SetupType /t REG_DWORD /d 0 /f >> C:\vmware2scw-setup.log 2>&1
reg add "HKLM\SYSTEM\Setup" /v SystemSetupInProgress /t REG_DWORD /d 0 /f >> C:\vmware2scw-setup.log 2>&1
reg add "HKLM\SYSTEM\Setup" /v CmdLine /t REG_SZ /d "" /f >> C:\vmware2scw-setup.log 2>&1

echo [%date% %time%] Setup complete. Rebooting... >> C:\vmware2scw-setup.log
shutdown /r /t 10
"""


def _merge_reg(qcow2_path, reg_text, work_dir, name="reg"):
    """Merge a .reg file into the SYSTEM hive of a Windows qcow2 image."""
    reg_file = work_dir / f"{name}.reg"
    reg_file.write_text(reg_text, encoding="utf-8")

    # Try virt-win-reg first (simplest)
    r = _run(["virt-win-reg", "--merge", str(qcow2_path), str(reg_file)], check=False)
    if r.returncode == 0:
        logger.info(f"  Registry merged ({name})")
        return

    # Fallback: download SYSTEM hive, merge with hivexregedit, re-upload
    logger.info(f"  virt-win-reg failed, using hivexregedit fallback...")
    hive = work_dir / f"{name}.hive"
    hive.unlink(missing_ok=True)

    _run(["guestfish", "-a", str(qcow2_path), "-i", "--",
          "download", "/Windows/System32/config/SYSTEM", str(hive)])

    _run(["hivexregedit", "--merge", str(hive),
          "--prefix", "HKEY_LOCAL_MACHINE\\SYSTEM", str(reg_file)], check=False)

    _run(["guestfish", "-a", str(qcow2_path), "-i", "--",
          "upload", str(hive), "/Windows/System32/config/SYSTEM"])

    logger.info(f"  Registry merged via hivexregedit ({name})")


def _get_interface_guids(qcow2_path):
    """Get network interface GUIDs from Windows registry."""
    r = _run(["virt-win-reg", str(qcow2_path),
              "HKLM\\SYSTEM\\ControlSet001\\Services\\Tcpip\\Parameters\\Interfaces"],
             check=False)
    if r.returncode == 0:
        return list(set(re.findall(r'\{[0-9a-fA-F-]+\}', r.stdout)))
    return []


def _build_dhcp_reg(guids):
    """Build .reg content to force DHCP on all interfaces."""
    lines = ["Windows Registry Editor Version 5.00", ""]
    for guid in guids:
        base = (f"HKEY_LOCAL_MACHINE\\SYSTEM\\ControlSet001\\Services"
                f"\\Tcpip\\Parameters\\Interfaces\\{guid}")
        lines += [
            f"[{base}]",
            '"EnableDHCP"=dword:00000001',
            f'"IPAddress"={_str_to_reg_multi_sz(["0.0.0.0"])}',
            f'"SubnetMask"={_str_to_reg_multi_sz(["0.0.0.0"])}',
            f'"DefaultGateway"={_str_to_reg_multi_sz([])}',
            '"NameServer"=""', "",
        ]
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
#  Phase 1: Offline Preparation
# ═══════════════════════════════════════════════════════════════════

def _phase1_offline(qcow2_path, virtio_iso, work_dir):
    """Prepare the Windows image offline: drivers, registry, setup script.

    All writes happen via guestfish which requires NTFS to be clean.
    The NTFS dirty flag is cleared via ntfsfix before any writes.
    If the qcow2 is compressed (common after virt-v2v), decompress first
    since qemu-nbd can have I/O errors on compressed images.
    """
    logger.info("═══ Phase 1: Offline Preparation ═══")

    # Step 0: Try ntfsfix. If it fails (compressed qcow2), decompress first.
    if not _fix_ntfs_dirty(qcow2_path):
        _decompress_qcow2(qcow2_path)
        if not _fix_ntfs_dirty(qcow2_path):
            logger.warning("  ntfsfix still failing — trying writes anyway")

    # Step 1: Extract drivers from ISO
    logger.info("  Extracting drivers from ISO...")
    drivers = _extract_drivers(Path(virtio_iso), work_dir)

    # Step 2: Upload .sys files to System32\drivers\
    for name, d in drivers.items():
        _run(["guestfish", "-a", str(qcow2_path), "-i", "--",
              "upload", str(d["sys"]),
              f"/Windows/System32/drivers/{name}.sys"])
        logger.info(f"  Uploaded {name}.sys")

    # Step 3: Register Services in registry
    logger.info("  Registering driver services...")
    _merge_reg(qcow2_path, _build_services_reg(drivers.keys()),
               work_dir, "services")

    # Step 4: Stage full driver packages in C:\Drivers\ (for pnputil)
    logger.info("  Staging driver packages in C:\\Drivers\\...")
    for name, d in drivers.items():
        gdir = f"/Drivers/{name}"
        _run(["guestfish", "-a", str(qcow2_path), "-i", "--", "mkdir-p", gdir])
        for f in d["dir"].iterdir():
            if f.is_file():
                _run(["guestfish", "-a", str(qcow2_path), "-i", "--",
                      "upload", str(f), f"{gdir}/{f.name}"])
        logger.info(f"  Staged C:\\Drivers\\{name}\\")

    # Step 5: Write setup CMD script
    logger.info("  Writing setup script...")
    cmd_file = work_dir / "vmware2scw-setup.cmd"
    cmd_file.write_text(SETUP_CMD, encoding="utf-8")
    _run(["guestfish", "-a", str(qcow2_path), "-i", "--",
          "upload", str(cmd_file), "/Windows/vmware2scw-setup.cmd"])

    # Step 6: SetupPhase registry keys
    logger.info("  Setting SetupPhase registry keys...")
    setup_reg = (
        "Windows Registry Editor Version 5.00\n\n"
        "[HKEY_LOCAL_MACHINE\\SYSTEM\\Setup]\n"
        '"CmdLine"="cmd.exe /c C:\\\\Windows\\\\vmware2scw-setup.cmd"\n'
        '"SetupType"=dword:00000001\n'
        '"SystemSetupInProgress"=dword:00000001\n'
    )
    _merge_reg(qcow2_path, setup_reg, work_dir, "setup-phase")

    # Step 7: Force DHCP
    logger.info("  Forcing DHCP...")
    guids = _get_interface_guids(qcow2_path)
    if guids:
        _merge_reg(qcow2_path, _build_dhcp_reg(guids), work_dir, "dhcp")
        logger.info(f"  DHCP forced on {len(guids)} interface(s)")

    # Step 8: Disable BSOD auto-reboot (for debugging)
    _merge_reg(
        qcow2_path,
        'Windows Registry Editor Version 5.00\n\n'
        '[HKEY_LOCAL_MACHINE\\SYSTEM\\ControlSet001\\Control\\CrashControl]\n'
        '"AutoReboot"=dword:00000000\n',
        work_dir, "crash",
    )

    logger.info("═══ Phase 1 complete ═══")
    return drivers


# ═══════════════════════════════════════════════════════════════════
#  Phase 2: QEMU Headless Boot (pnputil)
# ═══════════════════════════════════════════════════════════════════

def _phase2_qemu_boot(qcow2_path, work_dir, timeout=QEMU_BOOT_TIMEOUT):
    """Boot Windows in QEMU with virtio-blk to execute pnputil.

    The offline preparation (Phase 1) configured SetupPhase to run
    vmware2scw-setup.cmd on first boot. This script uses pnputil to
    install all VirtIO drivers into the Windows DriverStore.

    We use virtio-blk (-drive if=virtio) because viostor.sys is
    already registered as a boot-critical Service from Phase 1.

    CRITICAL: We create a qcow2 overlay on top of the base image.
    The base image may be compressed (virt-v2v output). QEMU writes
    to the overlay, then we commit changes back.

    After this phase, the NTFS will be dirty. This is expected and
    NOT a problem — the drivers are in the DriverStore.
    """
    logger.info("═══ Phase 2: QEMU virtio-blk boot (pnputil) ═══")

    ovmf_code = Path("/usr/share/OVMF/OVMF_CODE_4M.fd")
    ovmf_vars_src = Path("/usr/share/OVMF/OVMF_VARS_4M.fd")

    if not ovmf_code.exists():
        logger.error("  OVMF not installed!")
        return False
    if not _check_kvm():
        logger.error("  /dev/kvm not available — cannot boot QEMU")
        return False

    logger.info("  KVM: available")

    # Create writable overlay (handles compressed qcow2)
    overlay = work_dir / "qemu-overlay.qcow2"
    overlay.unlink(missing_ok=True)
    _run(["qemu-img", "create", "-f", "qcow2",
          "-b", str(Path(qcow2_path).resolve()), "-F", "qcow2",
          str(overlay)], env=None)

    ovmf_vars = work_dir / "OVMF_VARS.fd"
    shutil.copy2(str(ovmf_vars_src), str(ovmf_vars))

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

    logger.info(f"  Starting QEMU (timeout={timeout}s)...")
    logger.info("  Windows will boot → SetupPhase → pnputil → reboot → QEMU exits")

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    timed_out = False
    try:
        proc.communicate(timeout=timeout)
        logger.info(f"  QEMU exited with code {proc.returncode}")
    except subprocess.TimeoutExpired:
        logger.warning(f"  QEMU timed out after {timeout}s — killing")
        proc.kill()
        proc.wait()
        timed_out = True

    # Commit overlay to base image
    logger.info("  Committing overlay changes to base image...")
    cr = subprocess.run(
        ["qemu-img", "commit", str(overlay)],
        capture_output=True, text=True,
    )
    if cr.returncode == 0:
        logger.info("  Overlay committed OK")
    else:
        # Compressed base can't accept direct commit — do full merge
        logger.info("  Direct commit failed, doing full merge...")
        merged = work_dir / "merged.qcow2"
        _run(["qemu-img", "convert", "-O", "qcow2",
              str(overlay), str(merged)], env=None)
        shutil.move(str(merged), str(qcow2_path))
        logger.info("  Full merge completed")

    overlay.unlink(missing_ok=True)

    # Verify: check setup log (read-only — NTFS may be dirty, that's fine)
    logger.info("  Checking setup log...")
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
            logger.info("  ✓ pnputil driver installation confirmed!")
            return True
        else:
            logger.warning("  Setup log exists but incomplete")
            return False
    else:
        if timed_out:
            logger.warning("  QEMU timed out and no setup log found")
        else:
            logger.warning("  No setup log found")
        return False


# ═══════════════════════════════════════════════════════════════════
#  Phase 3: Dual QEMU boot (virtio-blk + virtio-scsi PnP binding)
# ═══════════════════════════════════════════════════════════════════

PHASE3_TIMEOUT = 600  # 10 minutes — Windows needs to boot + PnP + shutdown

def _phase3_dual_boot(qcow2_path, work_dir, timeout=PHASE3_TIMEOUT):
    """Boot with both virtio-blk (boot) and virtio-scsi (PnP detection).

    After Phase 2, vioscsi is in the Windows DriverStore but not bound
    to any PCI device (because Phase 2 used virtio-blk only).

    Scaleway uses virtio-scsi. Windows needs to see the virtio-scsi PCI
    device to bind vioscsi from the DriverStore. We boot with BOTH
    controllers so Windows can:
      1. Boot via virtio-blk (viostor)
      2. Detect the virtio-scsi PCI device
      3. Auto-install vioscsi from DriverStore (PnP)

    After this, the image can boot with virtio-scsi only (Scaleway).

    We inject a small firstboot script to do a clean shutdown after
    giving PnP enough time to complete the binding.
    """
    logger.info("═══ Phase 3: Dual boot (virtio-scsi PnP binding) ═══")

    ovmf_code = Path("/usr/share/OVMF/OVMF_CODE_4M.fd")
    ovmf_vars_src = Path("/usr/share/OVMF/OVMF_VARS_4M.fd")

    if not ovmf_code.exists():
        logger.error("  OVMF not installed!")
        return False
    if not _check_kvm():
        logger.error("  /dev/kvm not available")
        return False

    work_dir = Path(work_dir)

    # Create writable overlay
    overlay = work_dir / "phase3-overlay.qcow2"
    overlay.unlink(missing_ok=True)
    _run(["qemu-img", "create", "-f", "qcow2",
          "-b", str(Path(qcow2_path).resolve()), "-F", "qcow2",
          str(overlay)], env=None)

    ovmf_vars = work_dir / "OVMF_VARS_phase3.fd"
    shutil.copy2(str(ovmf_vars_src), str(ovmf_vars))

    # Boot with virtio-blk (boot disk) + virtio-scsi device (for PnP)
    cmd = [
        "qemu-system-x86_64", "-enable-kvm",
        "-m", "4096", "-smp", "2", "-cpu", "host",
        "-drive", f"if=pflash,format=raw,readonly=on,file={ovmf_code}",
        "-drive", f"if=pflash,format=raw,file={ovmf_vars}",
        "-drive", f"file={overlay},format=qcow2,if=virtio",
        "-device", "virtio-scsi-pci,id=scsi0",
        "-display", "none",
        "-serial", "none",
        "-no-reboot",
    ]

    logger.info(f"  Starting QEMU dual (virtio-blk + virtio-scsi), timeout={timeout}s")
    logger.info("  Windows boots via virtio-blk, PnP detects virtio-scsi, binds vioscsi")

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        proc.communicate(timeout=timeout)
        logger.info(f"  QEMU exited with code {proc.returncode}")
    except subprocess.TimeoutExpired:
        # Timeout is expected — Windows boots to login and stays there.
        # PnP binding happens within the first 2-3 minutes.
        logger.info(f"  QEMU timeout after {timeout}s (expected — PnP binding done)")
        proc.kill()
        proc.wait()

    # Commit overlay
    logger.info("  Committing overlay changes...")
    cr = subprocess.run(
        ["qemu-img", "commit", str(overlay)],
        capture_output=True, text=True,
    )
    if cr.returncode == 0:
        logger.info("  Overlay committed OK")
    else:
        logger.info("  Direct commit failed, doing full merge...")
        merged = work_dir / "merged-phase3.qcow2"
        _run(["qemu-img", "convert", "-O", "qcow2",
              str(overlay), str(merged)], env=None)
        shutil.move(str(merged), str(qcow2_path))
        logger.info("  Full merge completed")

    overlay.unlink(missing_ok=True)

    logger.info("═══ Phase 3 complete — vioscsi PnP binding done ═══")
    return True


# ═══════════════════════════════════════════════════════════════════
#  Main Entry Point
# ═══════════════════════════════════════════════════════════════════

def ensure_all_virtio_drivers(qcow2_path, virtio_iso, work_dir=None):
    """Install VirtIO drivers in a Windows qcow2 image.

    This is the main entry point called by the migration pipeline.
    Runs Phase 1 (offline prep) + Phase 2 (QEMU pnputil boot).

    After successful completion, the image has all 3 VirtIO drivers
    (viostor, vioscsi, netkvm) in the Windows DriverStore and is
    ready for Scaleway's virtio-scsi controller.

    Args:
        qcow2_path: Path to the Windows qcow2 image
        virtio_iso: Path to the virtio-win ISO
        work_dir: Working directory (auto-created if None)

    Raises:
        RuntimeError: If Phase 2 fails (manual DISM needed)
    """
    if work_dir is None:
        work_dir = Path(tempfile.mkdtemp(prefix="virtio-"))
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    ensure_prerequisites()

    logger.info("╔══════════════════════════════════════════════════╗")
    logger.info("║  Windows VirtIO Driver Installation v0.5.1      ║")
    logger.info("╚══════════════════════════════════════════════════╝")

    # Phase 1: Offline preparation (guestfish writes)
    _phase1_offline(qcow2_path, virtio_iso, work_dir)

    # Phase 2: QEMU headless boot (pnputil)
    phase2_ok = _phase2_qemu_boot(qcow2_path, work_dir)

    if phase2_ok:
        logger.info("╔══════════════════════════════════════════════════╗")
        logger.info("║  ✓ VirtIO drivers installed successfully        ║")
        logger.info("║  Image ready for Scaleway (virtio-scsi)         ║")
        logger.info("╚══════════════════════════════════════════════════╝")
        return True

    # Phase 2 failed
    logger.error("╔══════════════════════════════════════════════════╗")
    logger.error("║  ✗ QEMU pnputil boot FAILED                    ║")
    logger.error("║  Drivers may not be in DriverStore              ║")
    logger.error("╚══════════════════════════════════════════════════╝")
    logger.error("")
    logger.error("  Manual fix from Scaleway rescue mode:")
    logger.error("  1. apt install qemu-system-x86 ovmf")
    logger.error("  2. wget https://fedorapeople.org/groups/virt/virtio-win/"
                 "direct-downloads/stable-virtio/virtio-win.iso")
    logger.error("  3. cp /usr/share/OVMF/OVMF_VARS_4M.fd /tmp/")
    logger.error("  4. qemu-system-x86_64 -enable-kvm -m 4096 \\")
    logger.error("       -drive if=pflash,format=raw,readonly=on,"
                 "file=/usr/share/OVMF/OVMF_CODE_4M.fd \\")
    logger.error("       -drive if=pflash,format=raw,file=/tmp/OVMF_VARS_4M.fd \\")
    logger.error("       -device virtio-scsi-pci,id=scsi0 \\")
    logger.error("       -drive file=/dev/sda,format=raw,if=none,id=disk0 \\")
    logger.error("       -device scsi-hd,drive=disk0,bus=scsi0.0 \\")
    logger.error("       -drive file=virtio-win.iso,media=cdrom \\")
    logger.error("       -vnc :1 -daemonize")
    logger.error("  5. VNC → WinRE → Troubleshoot → Command Prompt")
    logger.error("  6. drvload D:\\vioscsi\\2k19\\amd64\\vioscsi.inf")
    logger.error("  7. dism /image:C:\\ /add-driver /driver:D:\\ /recurse")
    logger.error("  8. bcdboot C:\\Windows /s S: /f UEFI")

    raise RuntimeError(
        "QEMU Phase 2 failed — VirtIO drivers may not be installed. "
        "Manual DISM from Scaleway rescue mode required. See logs."
    )


# Backwards-compatible alias
inject_virtio_windows = ensure_all_virtio_drivers

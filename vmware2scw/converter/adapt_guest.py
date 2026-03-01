"""Unified guest adaptation for Linux — single virt-customize call.

Replaces the separate clean_tools → inject_virtio → fix_bootloader stages
with a single virt-customize invocation, saving ~15-20s of libguestfs
appliance boot overhead.

v2.0 — Optimized pipeline:
  1 appel virt-customize au lieu de 3-4
  virt-v2v complètement éliminé (fallback direct)
"""

import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

GUESTFS_ENV = {**os.environ, "LIBGUESTFS_BACKEND": "direct"}


def _run(cmd, check=True, **kw):
    logger.debug(f"  $ {' '.join(str(c) for c in cmd[:6])}...")
    r = subprocess.run(cmd, capture_output=True, text=True, env=GUESTFS_ENV, **kw)
    if check and r.returncode != 0:
        err = r.stderr.strip()[-500:] if r.stderr else f"exit {r.returncode}"
        raise RuntimeError(f"Command failed: {' '.join(str(c) for c in cmd[:4])}\n{err}")
    return r


def adapt_linux_guest(boot_disk: str | Path, skip_uefi_fallback: bool = False) -> None:
    """Apply ALL guest adaptations in a single virt-customize call.

    Combines:
      - VMware tools cleanup (was: clean_tools stage)
      - VirtIO module injection (was: inject_virtio stage)
      - Bootloader fix for KVM (was: fix_bootloader stage)
      - Network configuration (was: fix_network stage)
      - UEFI fallback boot path (was: part of fix_bootloader)

    This saves ~15-20s by booting the libguestfs appliance only once
    instead of 3-4 times.

    Args:
        boot_disk: Path to the boot disk qcow2 image
        skip_uefi_fallback: Skip UEFI fallback (if ensure_uefi will handle it)
    """
    boot_disk = str(boot_disk)
    logger.info("Adapting Linux guest (unified virt-customize)...")

    commands = []

    # ═══ 1. Clean VMware tools ═══
    # Try all package managers (only the relevant one will succeed)
    commands += [
        "--run-command",
        "apt-get remove -y open-vm-tools open-vm-tools-desktop 2>/dev/null || true",
        "--run-command",
        "yum remove -y open-vm-tools open-vm-tools-desktop 2>/dev/null || true",
        "--run-command",
        "dnf remove -y open-vm-tools open-vm-tools-desktop 2>/dev/null || true",
        "--run-command",
        "zypper remove -y open-vm-tools open-vm-tools-desktop 2>/dev/null || true",
        # Remove manual VMware tools installations
        "--run-command",
        "rm -rf /etc/vmware-tools /usr/lib/vmware-tools 2>/dev/null || true",
        # Remove VMware udev rules
        "--run-command",
        "rm -f /etc/udev/rules.d/*vmware* /etc/udev/rules.d/99-vmware-scsi-udev.rules 2>/dev/null || true",
        # Disable VMware services
        "--run-command",
        "systemctl disable vmtoolsd.service vmware-tools.service 2>/dev/null || true",
    ]

    # ═══ 2. Inject VirtIO modules into initramfs ═══
    # Debian/Ubuntu path
    commands += [
        "--run-command",
        "if command -v update-initramfs >/dev/null 2>&1; then "
        "  for mod in virtio_blk virtio_scsi virtio_net virtio_pci; do "
        "    grep -q $mod /etc/initramfs-tools/modules 2>/dev/null || "
        "    echo $mod >> /etc/initramfs-tools/modules; "
        "  done; "
        "  update-initramfs -u; "
        "fi",
    ]
    # RHEL/CentOS/Rocky path
    commands += [
        "--run-command",
        "if command -v dracut >/dev/null 2>&1; then "
        "  dracut --force --add-drivers 'virtio_blk virtio_scsi virtio_net virtio_pci' 2>/dev/null || true; "
        "fi",
    ]

    # ═══ 3. Fix bootloader for KVM ═══
    # 3a. Fix fstab: /dev/sd* → /dev/vd*
    commands += [
        "--run-command",
        "if [ -f /etc/fstab ]; then "
        "  cp /etc/fstab /etc/fstab.vmware2scw.bak; "
        "  sed -i 's|/dev/sda|/dev/vda|g; s|/dev/sdb|/dev/vdb|g; s|/dev/sdc|/dev/vdc|g' /etc/fstab; "
        "fi",
    ]
    # 3b. Fix GRUB device references
    commands += [
        "--run-command",
        "if [ -f /etc/default/grub ]; then "
        "  cp /etc/default/grub /etc/default/grub.vmware2scw.bak; "
        "  sed -i 's|/dev/sda|/dev/vda|g' /etc/default/grub; "
        "fi",
    ]
    # 3c. Configure serial console for Scaleway
    commands += [
        "--run-command",
        "if [ -f /etc/default/grub ]; then "
        "  sed -i '/^GRUB_TERMINAL_OUTPUT=/d; /^GRUB_TERMINAL=/d; /^GRUB_SERIAL_COMMAND=/d; "
        "/^GRUB_GFXMODE=/d; /^GRUB_GFXPAYLOAD_LINUX=/d' /etc/default/grub; "
        "  echo 'GRUB_TERMINAL=\"console serial\"' >> /etc/default/grub; "
        "  echo 'GRUB_SERIAL_COMMAND=\"serial --speed=115200 --unit=0 --word=8 --parity=no --stop=1\"' >> /etc/default/grub; "
        "  echo 'GRUB_TERMINAL_OUTPUT=\"console serial\"' >> /etc/default/grub; "
        "  sed -i 's/^GRUB_CMDLINE_LINUX_DEFAULT=.*/GRUB_CMDLINE_LINUX_DEFAULT=\"console=tty1 console=ttyS0,115200n8\"/' /etc/default/grub; "
        "  grep -q 'console=ttyS0' /etc/default/grub || "
        "    sed -i 's/^GRUB_CMDLINE_LINUX=.*/GRUB_CMDLINE_LINUX=\"console=tty1 console=ttyS0,115200n8\"/' /etc/default/grub; "
        "fi",
    ]
    # 3d. Fix device.map
    commands += [
        "--run-command",
        "if [ -f /boot/grub/device.map ]; then "
        "  sed -i 's|/dev/sda|/dev/vda|g' /boot/grub/device.map; "
        "fi",
    ]
    # 3e. Regenerate GRUB config
    commands += [
        "--run-command",
        "if command -v grub-mkconfig >/dev/null 2>&1; then "
        "  grub-mkconfig -o /boot/grub/grub.cfg 2>/dev/null || true; "
        "elif command -v grub2-mkconfig >/dev/null 2>&1; then "
        "  grub2-mkconfig -o /boot/grub2/grub.cfg 2>/dev/null || true; "
        "fi",
    ]

    # ═══ 4. Remove VMware SCSI modprobe configs ═══
    commands += [
        "--run-command",
        "rm -f /etc/modprobe.d/*vmw* /etc/modprobe.d/*vmware* 2>/dev/null || true",
    ]

    # ═══ 5. Clean persistent net rules ═══
    commands += [
        "--run-command",
        "rm -f /etc/udev/rules.d/70-persistent-net.rules "
        "/etc/udev/rules.d/75-persistent-net-generator.rules 2>/dev/null || true",
    ]

    # ═══ 6. Configure network (DHCP) ═══
    commands += [
        "--run-command",
        "if [ -d /etc/netplan ]; then "
        "  cat > /etc/netplan/50-cloud-init.yaml << 'NETPLAN'\n"
        "network:\n"
        "  version: 2\n"
        "  ethernets:\n"
        "    ens2:\n"
        "      dhcp4: true\n"
        "    eth0:\n"
        "      dhcp4: true\n"
        "NETPLAN\n"
        "elif [ -d /etc/sysconfig/network-scripts ]; then "
        "  cat > /etc/sysconfig/network-scripts/ifcfg-eth0 << 'IFCFG'\n"
        "DEVICE=eth0\n"
        "ONBOOT=yes\n"
        "BOOTPROTO=dhcp\n"
        "IFCFG\n"
        "fi",
    ]

    # ═══ 7. UEFI fallback boot path (for VMs already UEFI) ═══
    if not skip_uefi_fallback:
        commands += [
            "--run-command",
            "if [ -d /boot/efi/EFI ]; then "
            "  mkdir -p /boot/efi/EFI/BOOT; "
            "  for src in "
            "    /boot/efi/EFI/ubuntu/shimx64.efi /boot/efi/EFI/ubuntu/grubx64.efi "
            "    /boot/efi/EFI/debian/shimx64.efi /boot/efi/EFI/debian/grubx64.efi "
            "    /boot/efi/EFI/centos/shimx64.efi /boot/efi/EFI/centos/grubx64.efi "
            "    /boot/efi/EFI/fedora/shimx64.efi /boot/efi/EFI/fedora/grubx64.efi "
            "    /boot/efi/EFI/rocky/shimx64.efi /boot/efi/EFI/rocky/grubx64.efi "
            "    /boot/efi/EFI/almalinux/shimx64.efi /boot/efi/EFI/almalinux/grubx64.efi "
            "    /boot/efi/EFI/rhel/shimx64.efi /boot/efi/EFI/rhel/grubx64.efi "
            "    /boot/efi/EFI/sles/grubx64.efi /boot/efi/EFI/opensuse/grubx64.efi; do "
            "    if [ -f \"$src\" ]; then "
            "      cp \"$src\" /boot/efi/EFI/BOOT/BOOTX64.EFI; "
            "      echo \"Copied $src to BOOTX64.EFI\"; "
            "      break; "
            "    fi; "
            "  done; "
            "fi",
        ]

    # ═══ Execute single virt-customize call ═══
    cmd = ["virt-customize", "-a", boot_disk] + commands
    _run(cmd, check=False)  # check=False: some commands may fail (e.g. wrong package manager)

    logger.info("Linux guest adaptation complete (single virt-customize call)")
"""Migration pipeline orchestrator — coordinates all migration stages.

v3.0 — Boot reliability fixes:
  - Windows UEFI fallback: multi-strategy bootmgfw.efi recovery
    (search ESP → NTFS volumes → ESP glob → bcdboot QEMU rebuild)
  - Multi-disk Windows: auto-detect OS disk (Windows\System32) and
    swap to disk-0 position for Scaleway boot ordering
  - Fixes WIN2016-EFI (guestfish -i fails on non-OS boot disk)
  - Fixes WIN2019-AD/SQL (bootmgfw.efi missing after virt-v2v)

v2.0 — Optimized pipeline:
  - Linux: clean_tools + inject_virtio + fix_bootloader + fix_network → single adapt_guest stage
  - Linux: virt-v2v skipped (direct virt-customize, saves ~18s of failed attempts)
  - Windows: Phase 2+3 QEMU merged, serial monitoring, reduced timeouts (~500s saved)
  - fix_network stage removed from pipeline (was NOOP for both Linux and Windows)
  - Bug fix: guestfish --rw/-ro inconsistency in Windows UEFI fallback
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from vmware2scw.config import AppConfig, VMMigrationPlan
from vmware2scw.pipeline.state import MigrationState, MigrationStateStore
from vmware2scw.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class MigrationResult:
    """Result of a migration execution."""
    success: bool
    migration_id: str
    vm_name: str
    instance_id: Optional[str] = None
    image_id: Optional[str] = None
    duration: str = ""
    failed_stage: Optional[str] = None
    error: Optional[str] = None
    completed_stages: list[str] = field(default_factory=list)


class MigrationPipeline:
    """Orchestrates the full VMware → Scaleway migration pipeline.

    v2.0 — Optimized stage lists:

    Linux pipeline (9 stages, was 13):
      1. validate       — Pre-flight compatibility checks
      2. snapshot       — Create VMware snapshot for consistency
      3. export         — Export VMDK disks from VMware
      4. convert        — Convert VMDK → qcow2
      5. adapt_guest    — Unified: clean VMware tools + inject VirtIO + fix bootloader + configure network
      6. ensure_uefi    — Convert BIOS→UEFI if needed
      7. upload_s3      — Upload qcow2 to Scaleway Object Storage
      8. import_scw     — Import image into Scaleway (snapshot → image)
      9. verify         — Post-migration health checks
     10. cleanup        — Remove temporary files, snapshots

    Windows pipeline (10 stages, was 13):
      1. validate       — Pre-flight compatibility checks
      2. snapshot       — Create VMware snapshot for consistency
      3. export         — Export VMDK disks from VMware
      4. convert        — Convert VMDK → qcow2
      5. clean_tools    — Remove VMware tools from guest
      6. inject_virtio  — Phase 1 offline + merged Phase 2+3 QEMU boot (v2: serial monitoring)
      7. fix_bootloader — Adapt bootloader for KVM
      8. ensure_uefi    — Convert BIOS→UEFI if needed (v2: fixed guestfish --rw/-ro bug)
      9. upload_s3      — Upload qcow2 to Scaleway Object Storage
     10. import_scw     — Import image into Scaleway (snapshot → image)
     11. verify         — Post-migration health checks
     12. cleanup        — Remove temporary files, snapshots

    Each stage is idempotent and can be resumed after failure.
    """

    # v2: Separate stage lists per OS family
    STAGES_LINUX = [
        "validate",
        "snapshot",
        "export",
        "convert",
        "adapt_guest",       # NEW v2: replaces clean_tools + inject_virtio + fix_bootloader + fix_network
        "ensure_uefi",
        "upload_s3",
        "import_scw",
        "verify",
        "cleanup",
    ]

    STAGES_WINDOWS = [
        "validate",
        "snapshot",
        "export",
        "convert",
        "clean_tools",
        "inject_virtio",     # v2: merged Phase 2+3 with serial monitoring
        "fix_bootloader",
        "ensure_uefi",       # v2: fixed guestfish --rw/-ro bug
        # fix_network removed (was NOOP)
        "upload_s3",
        "import_scw",
        "verify",
        "cleanup",
    ]

    # Legacy fallback (used before OS is detected)
    STAGES = STAGES_LINUX

    def __init__(self, config: AppConfig):
        self.config = config
        self.state_store = MigrationStateStore(config.conversion.work_dir)

    def _get_stages(self, state: MigrationState) -> list[str]:
        """Get the appropriate stage list based on detected OS family."""
        vm_info = state.artifacts.get("vm_info", {})
        guest_os = vm_info.get("guest_os", "")
        if "win" in guest_os.lower():
            return self.STAGES_WINDOWS
        return self.STAGES_LINUX

    def run(self, plan: VMMigrationPlan) -> MigrationResult:
        """Execute a full migration for a single VM.

        Args:
            plan: Migration plan with VM name, target type, etc.

        Returns:
            MigrationResult with success status and details
        """
        migration_id = str(uuid.uuid4())[:8]
        start_time = time.time()

        state = MigrationState(
            migration_id=migration_id,
            vm_name=plan.vm_name,
            target_type=plan.target_type,
            zone=plan.zone,
            current_stage="",
            completed_stages=[],
            artifacts={},
            started_at=datetime.now(),
        )
        self.state_store.save(state)

        logger.info(f"[bold]Starting migration {migration_id}[/bold]: "
                     f"{plan.vm_name} → {plan.target_type} ({plan.zone})")

        # v2: Start with Linux stages, switch after validate detects OS
        stages_to_run = list(self.STAGES_LINUX)
        if plan.skip_validation:
            stages_to_run = [s for s in stages_to_run if s != "validate"]

        idx = 0
        while idx < len(stages_to_run):
            stage_name = stages_to_run[idx]
            state.current_stage = stage_name
            self.state_store.save(state)

            logger.info(f"[cyan]▶ Stage: {stage_name}[/cyan]")
            try:
                self._execute_stage(stage_name, plan, state)
                state.completed_stages.append(stage_name)
                self.state_store.save(state)
                logger.info(f"[green]✓ Stage {stage_name} complete[/green]")

                # v2: After validate, switch to the correct stage list for the detected OS
                if stage_name == "validate":
                    new_stages = self._get_stages(state)
                    if plan.skip_validation:
                        new_stages = [s for s in new_stages if s != "validate"]
                    # Replace remaining stages but keep completed ones
                    remaining_new = [s for s in new_stages if s not in state.completed_stages]
                    stages_to_run = list(state.completed_stages) + remaining_new
                    idx = len(state.completed_stages) - 1  # Will be incremented

            except Exception as e:
                elapsed = time.time() - start_time
                state.error = str(e)
                self.state_store.save(state)

                logger.error(f"[red]✗ Stage {stage_name} failed: {e}[/red]")
                return MigrationResult(
                    success=False,
                    migration_id=migration_id,
                    vm_name=plan.vm_name,
                    failed_stage=stage_name,
                    error=str(e),
                    duration=f"{elapsed:.0f}s",
                    completed_stages=list(state.completed_stages),
                )

            idx += 1

        elapsed = time.time() - start_time
        logger.info(f"[bold green]Migration {migration_id} complete in {elapsed:.0f}s[/bold green]")

        return MigrationResult(
            success=True,
            migration_id=migration_id,
            vm_name=plan.vm_name,
            instance_id=state.artifacts.get("scaleway_instance_id"),
            image_id=state.artifacts.get("scaleway_image_id"),
            duration=f"{elapsed:.0f}s",
            completed_stages=list(state.completed_stages),
        )

    def resume(self, migration_id: str) -> MigrationResult:
        """Resume a failed migration from the last successful stage."""
        state = self.state_store.load(migration_id)
        if not state:
            raise ValueError(f"Migration '{migration_id}' not found")

        logger.info(f"Resuming migration {migration_id} for VM '{state.vm_name}'")
        logger.info(f"Completed stages: {', '.join(state.completed_stages)}")

        plan = VMMigrationPlan(
            vm_name=state.vm_name,
            target_type=state.target_type,
            zone=state.zone,
        )

        # v2: Use the correct stage list based on detected OS
        all_stages = self._get_stages(state)
        remaining = [s for s in all_stages if s not in state.completed_stages]
        if not remaining:
            return MigrationResult(
                success=True,
                migration_id=migration_id,
                vm_name=state.vm_name,
                completed_stages=list(state.completed_stages),
            )

        start_time = time.time()
        state.error = None

        for stage_name in remaining:
            state.current_stage = stage_name
            self.state_store.save(state)

            logger.info(f"[cyan]▶ Stage: {stage_name}[/cyan] (resumed)")
            try:
                self._execute_stage(stage_name, plan, state)
                state.completed_stages.append(stage_name)
                self.state_store.save(state)
                logger.info(f"[green]✓ Stage {stage_name} complete[/green]")

            except Exception as e:
                state.error = str(e)
                self.state_store.save(state)
                elapsed = time.time() - start_time

                return MigrationResult(
                    success=False,
                    migration_id=migration_id,
                    vm_name=state.vm_name,
                    failed_stage=stage_name,
                    error=str(e),
                    duration=f"{elapsed:.0f}s",
                    completed_stages=list(state.completed_stages),
                )

        elapsed = time.time() - start_time
        return MigrationResult(
            success=True,
            migration_id=migration_id,
            vm_name=state.vm_name,
            instance_id=state.artifacts.get("scaleway_instance_id"),
            image_id=state.artifacts.get("scaleway_image_id"),
            duration=f"{elapsed:.0f}s",
            completed_stages=list(state.completed_stages),
        )

    def dry_run(self, plan: VMMigrationPlan) -> None:
        """Simulate a migration without executing any stages."""
        logger.info(f"[yellow]DRY RUN for VM '{plan.vm_name}'[/yellow]")
        logger.info(f"Target: {plan.target_type} in {plan.zone}")
        logger.info("Linux stages:")
        for i, stage in enumerate(self.STAGES_LINUX, 1):
            if plan.skip_validation and stage == "validate":
                logger.info(f"  {i}. {stage} [dim](skipped)[/dim]")
            else:
                logger.info(f"  {i}. {stage}")
        logger.info("Windows stages:")
        for i, stage in enumerate(self.STAGES_WINDOWS, 1):
            if plan.skip_validation and stage == "validate":
                logger.info(f"  {i}. {stage} [dim](skipped)[/dim]")
            else:
                logger.info(f"  {i}. {stage}")

    def _execute_stage(self, stage: str, plan: VMMigrationPlan, state: MigrationState) -> None:
        """Execute a single pipeline stage.

        Each stage method updates state.artifacts with any intermediate
        results (file paths, IDs, etc.) for use by subsequent stages.
        """
        handler = getattr(self, f"_stage_{stage}", None)
        if handler is None:
            raise NotImplementedError(f"Stage '{stage}' not implemented yet")
        handler(plan, state)

    # ─── Stage implementations ───────────────────────────────────────

    def _stage_validate(self, plan: VMMigrationPlan, state: MigrationState) -> None:
        """Pre-flight validation: check VM compatibility with target type."""
        from vmware2scw.pipeline.validator import MigrationValidator
        from vmware2scw.vmware.client import VSphereClient
        from vmware2scw.vmware.inventory import VMInventory

        client = VSphereClient()
        pw = self.config.vmware.password.get_secret_value() if self.config.vmware.password else ""
        client.connect(
            self.config.vmware.vcenter,
            self.config.vmware.username,
            pw,
            insecure=self.config.vmware.insecure,
        )

        inv = VMInventory(client)
        vm_info = inv.get_vm_info(plan.vm_name)
        state.artifacts["vm_info"] = vm_info.model_dump()

        # Log VM characteristics for debugging
        firmware = vm_info.firmware if hasattr(vm_info, 'firmware') else 'unknown'
        guest_os = vm_info.guest_os if hasattr(vm_info, 'guest_os') else 'unknown'
        logger.info(f"VM '{plan.vm_name}': guest_os={guest_os}, firmware={firmware}, "
                     f"cpu={vm_info.cpu}, ram={vm_info.memory_mb}MB, "
                     f"disks={len(vm_info.disks)}")
        if firmware == "efi":
            logger.info("  Source VM uses UEFI firmware (Scaleway also uses UEFI — good)")
        else:
            logger.info("  Source VM uses BIOS firmware (will convert to UEFI for Scaleway)")

        validator = MigrationValidator()
        report = validator.validate(vm_info, plan.target_type)

        client.disconnect()

        if not report.passed:
            failures = [c for c in report.checks if not c.passed and c.blocking]
            msg = "; ".join(f"{c.name}: {c.message}" for c in failures)
            raise RuntimeError(f"Pre-validation failed: {msg}")

    def _stage_snapshot(self, plan: VMMigrationPlan, state: MigrationState) -> None:
        """Create a VMware snapshot for consistent export."""
        from vmware2scw.vmware.client import VSphereClient
        from vmware2scw.vmware.snapshot import SnapshotManager

        client = VSphereClient()
        pw = self.config.vmware.password.get_secret_value() if self.config.vmware.password else ""
        client.connect(
            self.config.vmware.vcenter,
            self.config.vmware.username,
            pw,
            insecure=self.config.vmware.insecure,
        )

        snap_mgr = SnapshotManager(client)
        snap_name = f"vmware2scw-{state.migration_id}"
        snap_mgr.create_migration_snapshot(plan.vm_name, snap_name)
        state.artifacts["snapshot_name"] = snap_name

        client.disconnect()

    def _stage_export(self, plan: VMMigrationPlan, state: MigrationState) -> None:
        """Export VMDK disks from VMware."""
        from vmware2scw.vmware.client import VSphereClient
        from vmware2scw.vmware.export import VMExporter

        work_dir = self.config.conversion.work_dir / state.migration_id
        work_dir.mkdir(parents=True, exist_ok=True)

        client = VSphereClient()
        pw = self.config.vmware.password.get_secret_value() if self.config.vmware.password else ""
        client.connect(
            self.config.vmware.vcenter,
            self.config.vmware.username,
            pw,
            insecure=self.config.vmware.insecure,
        )

        exporter = VMExporter(client)
        vmdk_paths = exporter.export_vm_disks(plan.vm_name, work_dir)
        state.artifacts["vmdk_paths"] = [str(p) for p in vmdk_paths]

        client.disconnect()

    def _stage_convert(self, plan: VMMigrationPlan, state: MigrationState) -> None:
        """Convert VMDK disks to qcow2 format."""
        from vmware2scw.converter.disk import DiskConverter
        from vmware2scw.scaleway.mapping import ResourceMapper

        converter = DiskConverter()
        qcow2_paths = []

        # Determine OS family for compression decision
        mapper = ResourceMapper()
        vm_info_dict = state.artifacts.get("vm_info", {})
        guest_os = vm_info_dict.get("guest_os", "otherLinux64Guest")
        os_family, _ = mapper.get_os_family(guest_os)

        # Windows: do NOT compress — qemu-nbd has I/O errors on compressed qcow2
        # The image will be compressed later before upload if needed.
        compress = self.config.conversion.compress_qcow2
        if os_family == "windows":
            compress = False
            logger.info("Windows VM: disabling qcow2 compression (required for ntfsfix/qemu-nbd)")

        for vmdk_path in state.artifacts.get("vmdk_paths", []):
            vmdk = Path(vmdk_path)
            qcow2_path = vmdk.with_suffix(".qcow2")

            # Skip if already converted and valid
            if qcow2_path.exists() and converter.check(qcow2_path):
                logger.info(f"Skipping conversion (already exists): {qcow2_path.name}")
                qcow2_paths.append(str(qcow2_path))
                continue

            converter.convert(
                vmdk,
                qcow2_path,
                compress=compress,
            )
            qcow2_paths.append(str(qcow2_path))

        state.artifacts["qcow2_paths"] = qcow2_paths

        # Free disk space: delete VMDK source files after successful conversion
        for vmdk_path in state.artifacts.get("vmdk_paths", []):
            vmdk = Path(vmdk_path)
            if vmdk.exists():
                size_mb = vmdk.stat().st_size / (1024**2)
                vmdk.unlink()
                logger.info(f"Deleted source VMDK: {vmdk.name} ({size_mb:.0f} MB freed)")

    # ─── v2 NEW: adapt_guest (Linux only) ────────────────────────────

    def _stage_adapt_guest(self, plan: VMMigrationPlan, state: MigrationState) -> None:
        """v2 NEW: Unified Linux guest adaptation — single virt-customize call.

        Replaces 4 separate stages: clean_tools + inject_virtio + fix_bootloader + fix_network.
        Saves ~15-20s by booting the libguestfs appliance only once instead of 3-4 times.
        Also skips virt-v2v entirely (saves ~18s of failed attempts on Ubuntu 24.04).
        """
        from vmware2scw.utils.subprocess import run_command

        qcow2_paths = state.artifacts.get("qcow2_paths", [])
        if not qcow2_paths:
            logger.warning("No qcow2 files found — skipping adapt_guest")
            return

        boot_disk = qcow2_paths[0]
        vm_info_dict = state.artifacts.get("vm_info", {})
        firmware = vm_info_dict.get("firmware", "bios")

        logger.info("Adapting Linux guest (unified virt-customize — v2)...")

        commands = []

        # ═══ 1. Clean VMware tools ═══
        commands += [
            "--run-command",
            "apt-get remove -y open-vm-tools open-vm-tools-desktop 2>/dev/null || true",
            "--run-command",
            "yum remove -y open-vm-tools open-vm-tools-desktop 2>/dev/null || true",
            "--run-command",
            "dnf remove -y open-vm-tools open-vm-tools-desktop 2>/dev/null || true",
            "--run-command",
            "zypper remove -y open-vm-tools open-vm-tools-desktop 2>/dev/null || true",
            "--run-command",
            "rm -rf /etc/vmware-tools /usr/lib/vmware-tools 2>/dev/null || true",
            "--run-command",
            "rm -f /etc/udev/rules.d/*vmware* /etc/udev/rules.d/99-vmware-scsi-udev.rules 2>/dev/null || true",
            "--run-command",
            "systemctl disable vmtoolsd.service vmware-tools.service 2>/dev/null || true",
        ]

        # ═══ 2. Inject VirtIO modules into initramfs ═══
        commands += [
            "--run-command",
            "if [ -d /etc/initramfs-tools ]; then "
            "  for mod in virtio_blk virtio_scsi virtio_net virtio_pci; do "
            "    grep -q $mod /etc/initramfs-tools/modules 2>/dev/null || echo $mod >> /etc/initramfs-tools/modules; "
            "  done; "
            "  update-initramfs -u 2>/dev/null || true; "
            "elif command -v dracut >/dev/null 2>&1; then "
            "  dracut --force --add-drivers 'virtio_blk virtio_scsi virtio_net virtio_pci' 2>/dev/null || true; "
            "fi",
        ]

        # ═══ 3. Fix bootloader for KVM ═══
        # 3a. Fix /etc/fstab: replace /dev/sd* with /dev/vd*
        commands += [
            "--run-command",
            "if [ -f /etc/fstab ]; then "
            "  cp /etc/fstab /etc/fstab.vmware2scw.bak; "
            "  sed -i 's|/dev/sda|/dev/vda|g; s|/dev/sdb|/dev/vdb|g; s|/dev/sdc|/dev/vdc|g' /etc/fstab; "
            "fi",
        ]
        # 3b. Fix GRUB config
        commands += [
            "--run-command",
            "if [ -f /etc/default/grub ]; then "
            "  cp /etc/default/grub /etc/default/grub.vmware2scw.bak; "
            "  sed -i 's|/dev/sda|/dev/vda|g' /etc/default/grub; "
            "fi",
        ]
        # 3c. Configure GRUB for serial console (Scaleway has no VGA)
        commands += [
            "--run-command",
            "if [ -f /etc/default/grub ]; then "
            "  sed -i '/^GRUB_TERMINAL_OUTPUT=/d' /etc/default/grub; "
            "  sed -i '/^GRUB_TERMINAL=/d' /etc/default/grub; "
            "  sed -i '/^GRUB_SERIAL_COMMAND=/d' /etc/default/grub; "
            "  sed -i '/^GRUB_GFXMODE=/d' /etc/default/grub; "
            "  sed -i '/^GRUB_GFXPAYLOAD_LINUX=/d' /etc/default/grub; "
            "  echo 'GRUB_TERMINAL=\"console serial\"' >> /etc/default/grub; "
            "  echo 'GRUB_SERIAL_COMMAND=\"serial --speed=115200 --unit=0 --word=8 --parity=no --stop=1\"' >> /etc/default/grub; "
            "  echo 'GRUB_TERMINAL_OUTPUT=\"console serial\"' >> /etc/default/grub; "
            "  sed -i 's/^GRUB_CMDLINE_LINUX_DEFAULT=.*/GRUB_CMDLINE_LINUX_DEFAULT=\"console=tty1 console=ttyS0,115200n8\"/' /etc/default/grub; "
            "  grep -q 'console=ttyS0' /etc/default/grub || "
            "    sed -i 's/^GRUB_CMDLINE_LINUX=.*/GRUB_CMDLINE_LINUX=\"console=tty1 console=ttyS0,115200n8\"/' /etc/default/grub; "
            "fi",
        ]
        # 3d. Fix GRUB device map
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
            "rm -f /etc/modprobe.d/*vmw* 2>/dev/null || true; "
            "rm -f /etc/modprobe.d/*vmware* 2>/dev/null || true",
        ]

        # ═══ 5. Clean persistent net rules ═══
        commands += [
            "--run-command",
            "rm -f /etc/udev/rules.d/70-persistent-net.rules 2>/dev/null || true; "
            "rm -f /etc/udev/rules.d/75-persistent-net-generator.rules 2>/dev/null || true",
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

        # ═══ 7. UEFI fallback boot path (only if source is already UEFI) ═══
        if firmware == "efi":
            commands += [
                "--run-command",
                "if [ -d /boot/efi/EFI ]; then "
                "  mkdir -p /boot/efi/EFI/BOOT; "
                "  for src in /boot/efi/EFI/ubuntu/shimx64.efi /boot/efi/EFI/ubuntu/grubx64.efi "
                "             /boot/efi/EFI/debian/shimx64.efi /boot/efi/EFI/debian/grubx64.efi "
                "             /boot/efi/EFI/centos/shimx64.efi /boot/efi/EFI/centos/grubx64.efi "
                "             /boot/efi/EFI/fedora/shimx64.efi /boot/efi/EFI/fedora/grubx64.efi "
                "             /boot/efi/EFI/rocky/shimx64.efi /boot/efi/EFI/rocky/grubx64.efi "
                "             /boot/efi/EFI/almalinux/shimx64.efi /boot/efi/EFI/almalinux/grubx64.efi "
                "             /boot/efi/EFI/rhel/shimx64.efi /boot/efi/EFI/rhel/grubx64.efi "
                "             /boot/efi/EFI/sles/grubx64.efi /boot/efi/EFI/opensuse/grubx64.efi; do "
                "    if [ -f \"$src\" ]; then "
                "      cp \"$src\" /boot/efi/EFI/BOOT/BOOTX64.EFI; "
                "      echo \"Copied $src to BOOTX64.EFI\"; "
                "      break; "
                "    fi; "
                "  done; "
                "fi",
            ]

        # ═══ Execute single virt-customize call ═══
        cmd = ["virt-customize", "-a", str(boot_disk)] + commands
        run_command(cmd, env={"LIBGUESTFS_BACKEND": "direct"}, check=False)

        if len(qcow2_paths) > 1:
            logger.info(f"Skipping {len(qcow2_paths) - 1} data disk(s) — no OS to adapt")

        logger.info("Linux guest adaptation complete (single virt-customize call — v2)")

    # ─── Windows-only stages (unchanged from v1) ─────────────────────

    def _stage_clean_tools(self, plan: VMMigrationPlan, state: MigrationState) -> None:
        """Clean VMware tools from converted qcow2 disks.

        Only processes the boot disk (first disk). Additional data disks
        don't contain an OS and would fail virt-customize inspection.

        v3: Uses _find_windows_os_disk to detect the actual OS disk on
        multi-disk VMs (e.g. WIN2016-EFI with separate boot/OS disks).
        """
        from vmware2scw.converter.disk import VMwareToolsCleaner
        from vmware2scw.scaleway.mapping import ResourceMapper

        mapper = ResourceMapper()
        vm_info_dict = state.artifacts.get("vm_info", {})
        guest_os = vm_info_dict.get("guest_os", "otherLinux64Guest")
        os_family, _ = mapper.get_os_family(guest_os)

        qcow2_paths = state.artifacts.get("qcow2_paths", [])
        if not qcow2_paths:
            logger.warning("No qcow2 files found — skipping clean_tools")
            return

        cleaner = VMwareToolsCleaner()
        # Only clean the boot disk (first disk)
        boot_disk = qcow2_paths[0]
        logger.info(f"Cleaning boot disk: {Path(boot_disk).name}")
        cleaner.clean(boot_disk, os_family=os_family)

        if len(qcow2_paths) > 1:
            logger.info(f"Skipping {len(qcow2_paths) - 1} data disk(s) — no OS to clean")

    def _stage_inject_virtio(self, plan: VMMigrationPlan, state: MigrationState) -> None:
        """v3: VirtIO driver injection for Windows.

        Linux uses adapt_guest instead (this stage is not in STAGES_LINUX).

        v3 FIX: Auto-detect the actual OS boot disk. When a VM has multiple
        disks (e.g. WIN2016-EFI with 20GB ESP + 90GB OS), disk-0 might NOT
        be the Windows OS volume. guestfish -i and virt-v2v fail on non-OS disks.

        Windows workflow (v3):
          Step 0: Identify which disk contains Windows\System32 (boot disk)
          Step 1: Phase 1 — offline driver staging (guestfish)
          Step 2: virt-v2v — PCI device binding (UEFI only)
          Step 3: Merged Phase 2+3 — single QEMU boot with virtio-blk + virtio-scsi
                  + serial console monitoring for early exit
        """
        import shutil
        import subprocess
        from vmware2scw.scaleway.mapping import ResourceMapper
        from vmware2scw.utils.subprocess import run_command, check_tool_available

        mapper = ResourceMapper()
        vm_info_dict = state.artifacts.get("vm_info", {})
        guest_os = vm_info_dict.get("guest_os", "otherLinux64Guest")
        firmware = vm_info_dict.get("firmware", "efi")
        os_family, _ = mapper.get_os_family(guest_os)

        qcow2_paths = state.artifacts.get("qcow2_paths", [])
        if not qcow2_paths:
            logger.warning("No qcow2 files found — skipping inject_virtio")
            return

        if os_family != "windows":
            # v2: Linux should use adapt_guest, not inject_virtio
            boot_disk = Path(qcow2_paths[0])
            logger.warning("inject_virtio called for Linux — should use adapt_guest stage instead")
            self._inject_virtio_fallback(boot_disk, os_family)
            return

        # ──── v3: Auto-detect the actual Windows OS disk ────
        boot_disk_idx = self._find_windows_os_disk(qcow2_paths)
        boot_disk = Path(qcow2_paths[boot_disk_idx])

        if boot_disk_idx != 0:
            logger.info(f"  Windows OS detected on disk-{boot_disk_idx} ({boot_disk.name}), "
                        f"not disk-0 — swapping disk order for migration")
            # Swap so OS disk is always disk-0 (Scaleway boots from first volume)
            qcow2_paths[0], qcow2_paths[boot_disk_idx] = qcow2_paths[boot_disk_idx], qcow2_paths[0]
            state.artifacts["qcow2_paths"] = qcow2_paths
            boot_disk = Path(qcow2_paths[0])
            logger.info(f"  New disk order: {[Path(p).name for p in qcow2_paths]}")

        # ──── Windows: Phase 1 → virt-v2v → merged Phase 2+3 ────
        virtio_iso = self.config.conversion.virtio_win_iso
        if not virtio_iso or not Path(virtio_iso).exists():
            raise RuntimeError(
                "virtio-win ISO is required for Windows VMs.\n"
                "  wget -O /opt/virtio-win.iso "
                "https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/stable-virtio/virtio-win.iso\n"
                "  Then in migration.yaml: conversion.virtio_win_iso: /opt/virtio-win.iso"
            )

        self._ensure_rhsrvany()

        # Step 1: Phase 1 — offline prep on ORIGINAL writable qcow2
        logger.info("Windows Step 1/3: Offline driver staging (Phase 1)...")
        from vmware2scw.converter.windows_virtio import _phase1_offline, _phase2_qemu_boot, ensure_prerequisites
        import tempfile
        ensure_prerequisites()
        p1_work = boot_disk.parent / "virtio-phase1"
        p1_work.mkdir(parents=True, exist_ok=True)
        _phase1_offline(str(boot_disk), str(virtio_iso), p1_work)

        # Step 2: virt-v2v — PCI device binding
        logger.info("Windows Step 2/3: virt-v2v (PCI device binding)...")
        env = {"LIBGUESTFS_BACKEND": "direct", "VIRTIO_WIN": str(virtio_iso)}
        out_dir = boot_disk.parent / "v2v-out"
        out_dir.mkdir(parents=True, exist_ok=True)
        v2v_name = f"v2v-{boot_disk.stem}"

        v2v_ok = False
        for i, cmd in enumerate([
            ["virt-v2v", "-i", "disk", str(boot_disk),
             "-o", "qemu", "-os", str(out_dir),
             "-on", v2v_name, "-of", "qcow2", "-oc", "qcow2"],
            ["virt-v2v", "-i", "disk", str(boot_disk),
             "-o", "local", "-os", str(out_dir),
             "-on", v2v_name, "-of", "qcow2"],
        ], 1):
            logger.info(f"  Trying virt-v2v syntax {i}/2...")
            try:
                run_command(cmd, env=env, timeout=3600)
                v2v_ok = True
                logger.info(f"  virt-v2v syntax {i} succeeded")
                break
            except Exception as e:
                logger.warning(f"  virt-v2v syntax {i} failed: {e}")
                for f in out_dir.iterdir():
                    f.unlink(missing_ok=True)

        if not v2v_ok:
            raise RuntimeError("virt-v2v failed — cannot prepare Windows for KVM")

        # Find virt-v2v output and replace boot disk
        candidates = sorted(
            [f for f in out_dir.iterdir()
             if f.is_file() and f.stat().st_size > 1024 * 1024
             and f.suffix not in ('.xml', '.sh')],
            key=lambda f: f.stat().st_size, reverse=True,
        )
        if not candidates:
            raise RuntimeError(f"virt-v2v produced no output in {out_dir}")

        converted = candidates[0]
        logger.info(f"  virt-v2v output: {converted.name} ({converted.stat().st_size / (1024**3):.1f} GB)")

        boot_disk.unlink(missing_ok=True)
        import shutil as _shutil
        _shutil.move(str(converted), str(boot_disk))
        _shutil.rmtree(out_dir, ignore_errors=True)
        state.artifacts["qcow2_paths"][0] = str(boot_disk)
        logger.info("  virt-v2v complete — boot disk replaced")

        # Step 3: v2 merged Phase 2+3 — single QEMU boot with both controllers + serial monitoring
        logger.info("Windows Step 3/3: QEMU merged boot (pnputil + vioscsi PnP — v2)...")
        try:
            from vmware2scw.converter.windows_virtio_v2 import (
                _phase2_merged_qemu_boot,
                SETUP_CMD_V2,
            )

            # Upload v2 setup script (shutdown /s + serial output)
            p2_work = boot_disk.parent / "virtio-phase2"
            p2_work.mkdir(parents=True, exist_ok=True)
            cmd_file = p2_work / "vmware2scw-setup-v2.cmd"
            cmd_file.write_text(SETUP_CMD_V2, encoding="utf-8")

            import os as _os
            subprocess.run(
                ["guestfish", "-a", str(boot_disk), "-i", "--",
                 "upload", str(cmd_file), "/Windows/vmware2scw-setup.cmd"],
                capture_output=True, text=True,
                env={**_os.environ, "LIBGUESTFS_BACKEND": "direct"},
            )

            # Merged Phase 2+3 QEMU boot with serial monitoring
            phase_ok = _phase2_merged_qemu_boot(
                str(boot_disk), p2_work, firmware=firmware
            )

            if not phase_ok:
                logger.warning("QEMU merged boot may have timed out — checking if drivers installed anyway")

        except ImportError:
            # Fallback: use original Phase 2 + Phase 3 if v2 module not available
            logger.warning("windows_virtio_v2 not available — falling back to original Phase 2 + Phase 3")

            # Phase 2: QEMU boot for pnputil
            logger.info("  Fallback: QEMU virtio-blk boot (pnputil driver installation)...")
            p2_work = boot_disk.parent / "virtio-phase2"
            p2_work.mkdir(parents=True, exist_ok=True)
            phase2_ok = _phase2_qemu_boot(str(boot_disk), p2_work)

            if not phase2_ok:
                logger.warning("Phase 2 QEMU boot may have timed out")

            # Phase 3: QEMU dual boot (virtio-blk + virtio-scsi PnP binding)
            logger.info("  Fallback: QEMU dual boot (virtio-scsi PnP binding)...")
            from vmware2scw.converter.windows_virtio import _phase3_dual_boot
            p3_work = boot_disk.parent / "virtio-phase3"
            p3_work.mkdir(parents=True, exist_ok=True)
            _phase3_dual_boot(str(boot_disk), p3_work)

    def _stage_fix_bootloader(self, plan: VMMigrationPlan, state: MigrationState) -> None:
        """Fix bootloader for KVM: fstab device names, GRUB config, initramfs.

        VMware uses LSI Logic / PVSCSI controllers → /dev/sd* devices.
        KVM with VirtIO uses /dev/vd* devices.

        If fstab or GRUB reference /dev/sda, the VM won't boot.
        Modern systems use UUID/LABEL which is safe, but we fix both.

        Note: For Linux VMs in v2, this is handled by adapt_guest instead.
        """
        from vmware2scw.scaleway.mapping import ResourceMapper
        from vmware2scw.utils.subprocess import run_command

        mapper = ResourceMapper()
        vm_info_dict = state.artifacts.get("vm_info", {})
        guest_os = vm_info_dict.get("guest_os", "")
        os_family, _ = mapper.get_os_family(guest_os)

        qcow2_paths = state.artifacts.get("qcow2_paths", [])
        if not qcow2_paths:
            return

        boot_disk = qcow2_paths[0]

        if os_family == "windows":
            # EMS, RDP, and DHCP are ALL configured by ensure_all_virtio_drivers
            # (via the SetupPhase script vmware2scw-setup.cmd in inject_virtio).
            # The NTFS is dirty after QEMU Phase 2 — do NOT try to write.
            logger.info("Windows: EMS/RDP/DHCP already configured by inject_virtio — skipping")
            return

        # v2: Linux should use adapt_guest, but if we get here (e.g. resume), handle it
        logger.info("Fixing bootloader for KVM compatibility...")

        # All fixes in a single virt-customize call to avoid multiple guest inspections
        commands = [
            # 1. Fix /etc/fstab: replace /dev/sd* with /dev/vd* (only if not UUID)
            "--run-command",
            "if [ -f /etc/fstab ]; then "
            "  cp /etc/fstab /etc/fstab.vmware2scw.bak; "
            "  sed -i 's|/dev/sda|/dev/vda|g; s|/dev/sdb|/dev/vdb|g; s|/dev/sdc|/dev/vdc|g' /etc/fstab; "
            "fi",

            # 2. Fix GRUB config: replace sd* references with vd*
            "--run-command",
            "if [ -f /etc/default/grub ]; then "
            "  cp /etc/default/grub /etc/default/grub.vmware2scw.bak; "
            "  sed -i 's|/dev/sda|/dev/vda|g' /etc/default/grub; "
            "fi",

            # 2b. Configure GRUB for serial console (Scaleway has no VGA)
            "--run-command",
            "if [ -f /etc/default/grub ]; then "
            "  sed -i '/^GRUB_TERMINAL_OUTPUT=/d' /etc/default/grub; "
            "  sed -i '/^GRUB_TERMINAL=/d' /etc/default/grub; "
            "  sed -i '/^GRUB_SERIAL_COMMAND=/d' /etc/default/grub; "
            "  sed -i '/^GRUB_GFXMODE=/d' /etc/default/grub; "
            "  sed -i '/^GRUB_GFXPAYLOAD_LINUX=/d' /etc/default/grub; "
            "  echo 'GRUB_TERMINAL=\"console serial\"' >> /etc/default/grub; "
            "  echo 'GRUB_SERIAL_COMMAND=\"serial --speed=115200 --unit=0 --word=8 --parity=no --stop=1\"' >> /etc/default/grub; "
            "  echo 'GRUB_TERMINAL_OUTPUT=\"console serial\"' >> /etc/default/grub; "
            "  sed -i 's/^GRUB_CMDLINE_LINUX_DEFAULT=.*/GRUB_CMDLINE_LINUX_DEFAULT=\"console=tty1 console=ttyS0,115200n8\"/' /etc/default/grub; "
            "  grep -q 'console=ttyS0' /etc/default/grub || "
            "    sed -i 's/^GRUB_CMDLINE_LINUX=.*/GRUB_CMDLINE_LINUX=\"console=tty1 console=ttyS0,115200n8\"/' /etc/default/grub; "
            "fi",

            # 3. Fix GRUB device map
            "--run-command",
            "if [ -f /boot/grub/device.map ]; then "
            "  sed -i 's|/dev/sda|/dev/vda|g' /boot/grub/device.map; "
            "fi",

            # 4. Regenerate GRUB config
            "--run-command",
            "if command -v grub-mkconfig >/dev/null 2>&1; then "
            "  grub-mkconfig -o /boot/grub/grub.cfg 2>/dev/null || true; "
            "elif command -v grub2-mkconfig >/dev/null 2>&1; then "
            "  grub2-mkconfig -o /boot/grub2/grub.cfg 2>/dev/null || true; "
            "fi",

            # 5. Ensure VirtIO modules are loaded at boot
            "--run-command",
            "if [ -d /etc/initramfs-tools ]; then "
            "  for mod in virtio_blk virtio_scsi virtio_net virtio_pci; do "
            "    grep -q $mod /etc/initramfs-tools/modules 2>/dev/null || echo $mod >> /etc/initramfs-tools/modules; "
            "  done; "
            "  update-initramfs -u 2>/dev/null || true; "
            "elif command -v dracut >/dev/null 2>&1; then "
            "  dracut --force --add-drivers 'virtio_blk virtio_scsi virtio_net virtio_pci' 2>/dev/null || true; "
            "fi",

            # 6. Remove VMware SCSI driver references that interfere with VirtIO
            "--run-command",
            "rm -f /etc/modprobe.d/*vmw* 2>/dev/null || true; "
            "rm -f /etc/modprobe.d/*vmware* 2>/dev/null || true",

            # 7. Clean persistent net rules (interface names change)
            "--run-command",
            "rm -f /etc/udev/rules.d/70-persistent-net.rules 2>/dev/null || true; "
            "rm -f /etc/udev/rules.d/75-persistent-net-generator.rules 2>/dev/null || true",

            # 8. Enable DHCP on first interface (Scaleway provides IP via DHCP)
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

            # 9. Ensure UEFI fallback boot path exists (Scaleway NVRAM is empty)
            "--run-command",
            "if [ -d /boot/efi/EFI ]; then "
            "  mkdir -p /boot/efi/EFI/BOOT; "
            "  for src in /boot/efi/EFI/ubuntu/shimx64.efi /boot/efi/EFI/ubuntu/grubx64.efi "
            "             /boot/efi/EFI/debian/shimx64.efi /boot/efi/EFI/debian/grubx64.efi "
            "             /boot/efi/EFI/centos/shimx64.efi /boot/efi/EFI/centos/grubx64.efi "
            "             /boot/efi/EFI/fedora/shimx64.efi /boot/efi/EFI/fedora/grubx64.efi "
            "             /boot/efi/EFI/rocky/shimx64.efi /boot/efi/EFI/rocky/grubx64.efi "
            "             /boot/efi/EFI/almalinux/shimx64.efi /boot/efi/EFI/almalinux/grubx64.efi "
            "             /boot/efi/EFI/rhel/shimx64.efi /boot/efi/EFI/rhel/grubx64.efi "
            "             /boot/efi/EFI/sles/grubx64.efi /boot/efi/EFI/opensuse/grubx64.efi; do "
            "    if [ -f \"$src\" ]; then "
            "      cp \"$src\" /boot/efi/EFI/BOOT/BOOTX64.EFI; "
            "      echo \"Copied $src to BOOTX64.EFI\"; "
            "      break; "
            "    fi; "
            "  done; "
            "fi",
        ]

        cmd = ["virt-customize", "-a", str(boot_disk)] + commands
        run_command(cmd, env={"LIBGUESTFS_BACKEND": "direct"}, check=False)
        logger.info("Bootloader and network configuration fixed for KVM")

    def _stage_ensure_uefi(self, plan: VMMigrationPlan, state: MigrationState) -> None:
        """Ensure disk is UEFI-bootable. Scaleway uses UEFI firmware.

        If the source VM is BIOS/MBR (common for VMware), we must:
        - Convert MBR→GPT
        - Create an EFI System Partition (ESP)
        - Install GRUB EFI bootloader

        This is normally handled by virt-v2v, but falls back to manual
        conversion when virt-v2v fails (e.g. Ubuntu 24.04 kernel bug).

        v2: Fixed guestfish --rw/-ro bug for Windows UEFI fallback.
        """
        from vmware2scw.converter.bios2uefi import detect_boot_type, convert_bios_to_uefi
        from vmware2scw.scaleway.mapping import ResourceMapper

        mapper = ResourceMapper()
        vm_info_dict = state.artifacts.get("vm_info", {})
        guest_os = vm_info_dict.get("guest_os", "")
        firmware = vm_info_dict.get("firmware", "bios")
        os_family, _ = mapper.get_os_family(guest_os)

        qcow2_paths = state.artifacts.get("qcow2_paths", [])
        if not qcow2_paths:
            return

        boot_disk = qcow2_paths[0]

        # If virt-v2v succeeded (inject_virtio didn't fall back), disk should already be OK
        # Check anyway to be sure
        boot_type = detect_boot_type(boot_disk)
        logger.info(f"Boot type detection: firmware={firmware}, disk={boot_type}")

        if boot_type == "uefi":
            logger.info("Disk already UEFI-bootable — skipping conversion")
            # v2 FIX: We MUST ensure the fallback bootloader exists for Windows UEFI
            # Using fixed version that doesn't mix --ro and --rw guestfish options
            if os_family == "windows":
                logger.info("Ensuring UEFI fallback bootloader for Windows...")
                try:
                    self._ensure_windows_uefi_fallback_fixed(boot_disk)
                except Exception as e:
                    logger.warning(f"Failed to set Windows UEFI fallback: {e}")
            return

        if os_family == "windows":
            logger.info("Windows BIOS→UEFI: converting MBR→GPT + creating ESP + bcdboot")
            from vmware2scw.converter.bios2uefi_windows import convert_windows_bios_to_uefi
            converted = convert_windows_bios_to_uefi(
                boot_disk,
                work_dir=Path(boot_disk).parent / "bios2uefi",
            )
            if converted:
                logger.info("Windows BIOS → UEFI conversion successful")
            else:
                logger.warning(
                    "Windows BIOS→UEFI conversion failed. The image will not boot on Scaleway. "
                    "Consider using mbr2gpt.exe from WinPE or a RHEL conversion host."
                )
            return

        logger.info("Disk is BIOS — converting to UEFI for Scaleway compatibility")
        converted = convert_bios_to_uefi(boot_disk, os_family=os_family)
        if converted:
            logger.info("BIOS → UEFI conversion successful")
        else:
            logger.warning("BIOS → UEFI conversion was not performed")

    def _ensure_windows_uefi_fallback_fixed(self, qcow2_path: str) -> None:
        """v3.2 FIX: Windows UEFI fallback — robust bootloader recovery.

        After virt-v2v, the ESP layout may be completely different:
          - bootmgfw.efi may be missing from the ESP
          - virt-v2v may have restructured the boot partition
          - The NVRAM on Scaleway is empty → firmware uses fallback
            /EFI/BOOT/BOOTX64.EFI only
          - NTFS journal is dirty after QEMU Phase 3 → guestfish mount-ro fails

        v3.2: Uses inspect-os/inspect-get-mountpoints instead of mount-ro for
        NTFS access. Also uses ntfsfix to clear dirty journal before mounting.

        Strategy (ordered):
          1. Find ESP (FAT32 partition) and Windows OS root via inspect-os
          2. Search for bootmgfw.efi on ESP → copy to BOOTX64.EFI
          3. If not on ESP, use inspect to find Windows volume, ntfsfix it,
             then search for Windows/Boot/EFI/bootmgfw.efi
          4. If still not found, search for ANY .efi file on ESP
          5. Last resort: run bcdboot via QEMU to rebuild the BCD + bootloader
        """
        import os
        import subprocess

        gf_env = {**os.environ, "LIBGUESTFS_BACKEND": "direct"}

        # ── Step 1: Find ESP and Windows root via inspect-os ──
        # Use inspect-os to reliably find the Windows installation
        r_inspect = subprocess.run(
            ["guestfish", "--ro", "-a", qcow2_path, "--",
             "run", ":", "inspect-os"],
            capture_output=True, text=True, env=gf_env,
        )
        os_roots = [l.strip() for l in r_inspect.stdout.strip().split("\n") if l.strip()]

        win_root_dev = None
        for root in os_roots:
            r_type = subprocess.run(
                ["guestfish", "--ro", "-a", qcow2_path, "--",
                 "run", ":", f"inspect-get-type {root}"],
                capture_output=True, text=True, env=gf_env,
            )
            if "windows" in r_type.stdout.lower():
                win_root_dev = root
                break

        if win_root_dev:
            logger.info(f"  inspect-os found Windows root: {win_root_dev}")
        else:
            logger.info("  inspect-os did not find Windows root — using partition scan")

        # Find ESP from partition list
        r = subprocess.run(
            ["guestfish", "--ro", "-a", qcow2_path, "--",
             "run", ":", "list-partitions"],
            capture_output=True, text=True, env=gf_env,
        )
        partitions = [p.strip() for p in r.stdout.strip().split("\n") if p.strip()]

        esp_dev = None
        other_parts = []
        for part in partitions:
            r2 = subprocess.run(
                ["guestfish", "--ro", "-a", qcow2_path, "--",
                 "run", ":", "vfs-type", part],
                capture_output=True, text=True, env=gf_env,
            )
            fstype = r2.stdout.strip().lower()
            if "fat" in fstype:
                esp_dev = part
            else:
                other_parts.append(part)

        if not esp_dev:
            logger.warning("  ESP (FAT32) partition not found — cannot set UEFI fallback")
            return

        logger.info(f"  ESP found: {esp_dev}")

        # ── Step 2: Check if bootmgfw.efi exists on ESP ──
        r3 = subprocess.run(
            ["guestfish", "--ro", "-a", qcow2_path, "--",
             "run", ":",
             f"mount-ro {esp_dev} /", ":",
             "is-file /EFI/Microsoft/Boot/bootmgfw.efi"],
            capture_output=True, text=True, env=gf_env,
        )

        if "true" in r3.stdout.lower():
            logger.info("  bootmgfw.efi found on ESP — copying to fallback path")
            gf_script = f"""run
mount {esp_dev} /
mkdir-p /EFI/BOOT
cp /EFI/Microsoft/Boot/bootmgfw.efi /EFI/BOOT/BOOTX64.EFI
"""
            subprocess.run(
                ["guestfish", "--rw", "-a", qcow2_path, "--"],
                input=gf_script, capture_output=True, text=True, env=gf_env,
            )
            logger.info("  ✓ UEFI fallback bootloader configured (BOOTX64.EFI)")
            return

        logger.info("  bootmgfw.efi not on ESP — searching Windows volume...")

        # ── Step 3: Use inspect-os result or ntfsfix + mount to find bootmgfw.efi ──
        # The key insight: after virt-v2v + QEMU Phase 3, the NTFS journal is dirty.
        # guestfish mount-ro SILENTLY FAILS on dirty NTFS.
        # Solution: use qemu-nbd + ntfsfix to clear the journal first.
        bootmgfw_search_paths = [
            "/Windows/Boot/EFI/bootmgfw.efi",
            "/windows/Boot/EFI/bootmgfw.efi",
            "/WINDOWS/Boot/EFI/bootmgfw.efi",
        ]

        # Determine which partitions to check: prefer win_root_dev from inspect-os
        parts_to_check = []
        if win_root_dev:
            parts_to_check.append(win_root_dev)
        parts_to_check.extend([p for p in other_parts if p != win_root_dev])

        found_src = None
        found_part = None

        for part in parts_to_check:
            # Try ntfsfix first to clear the dirty journal, then mount
            # ntfsfix via guestfish "ntfsfix" command
            r_fix = subprocess.run(
                ["guestfish", "--rw", "-a", qcow2_path, "--",
                 "run", ":",
                 f"ntfsfix {part}"],
                capture_output=True, text=True, env=gf_env,
            )
            if r_fix.returncode == 0:
                logger.info(f"  ntfsfix cleared journal on {part}")
            # Even if ntfsfix fails, try mounting anyway

            for search_path in bootmgfw_search_paths:
                r4 = subprocess.run(
                    ["guestfish", "--ro", "-a", qcow2_path, "--",
                     "run", ":",
                     f"mount-ro {part} /", ":",
                     f"is-file {search_path}"],
                    capture_output=True, text=True, env=gf_env,
                )
                if "true" in r4.stdout.lower():
                    found_src = search_path
                    found_part = part
                    break
            if found_src:
                break

        if found_src and found_part:
            logger.info(f"  Found bootmgfw.efi at {found_part}:{found_src}")
            gf_script = f"""run
mount-ro {found_part} /mnt
mount {esp_dev} /esp
mkdir-p /esp/EFI/Microsoft/Boot
mkdir-p /esp/EFI/BOOT
cp /mnt{found_src} /esp/EFI/Microsoft/Boot/bootmgfw.efi
cp /mnt{found_src} /esp/EFI/BOOT/BOOTX64.EFI
"""
            r5 = subprocess.run(
                ["guestfish", "--rw", "-a", qcow2_path, "--"],
                input=gf_script, capture_output=True, text=True, env=gf_env,
            )
            if r5.returncode == 0:
                logger.info("  ✓ Copied bootmgfw.efi from Windows volume to ESP + fallback")
                return
            else:
                logger.warning(f"  Copy failed: {r5.stderr.strip()}")

        # ── Step 4: Search ESP for any bootable .efi file ──
        logger.info("  Searching ESP for any bootable .efi files...")
        r6 = subprocess.run(
            ["guestfish", "--ro", "-a", qcow2_path, "--",
             "run", ":",
             f"mount-ro {esp_dev} /", ":",
             "find /"],
            capture_output=True, text=True, env=gf_env,
        )
        all_files = [f"/{f.strip()}" for f in r6.stdout.strip().split("\n") if f.strip()]
        efi_files = [f for f in all_files if f.lower().endswith(".efi")]

        if efi_files:
            logger.info(f"  EFI files on ESP: {efi_files}")
            best = None
            for candidate in efi_files:
                if "bootmgfw" in candidate.lower():
                    best = candidate
                    break
                if "bootmgr" in candidate.lower():
                    best = candidate
                    break
                if "boot" in candidate.lower() and not best:
                    best = candidate
            if not best:
                best = efi_files[0]

            logger.info(f"  Using EFI bootloader: {best}")
            gf_script = f"""run
mount {esp_dev} /
mkdir-p /EFI/BOOT
cp {best} /EFI/BOOT/BOOTX64.EFI
"""
            subprocess.run(
                ["guestfish", "--rw", "-a", qcow2_path, "--"],
                input=gf_script, capture_output=True, text=True, env=gf_env,
            )
            logger.info("  ✓ UEFI fallback configured from existing ESP bootloader")
            return
        else:
            logger.info(f"  No .efi files on ESP. All files: {all_files[:20]}")

        # ── Step 5: Last resort — rebuild BCD via QEMU bcdboot ──
        logger.info("  No bootmgfw.efi found anywhere — rebuilding via QEMU bcdboot...")
        self._rebuild_windows_bcd_qemu(qcow2_path, esp_dev, win_root_dev, parts_to_check)

    def _rebuild_windows_bcd_qemu(self, qcow2_path: str, esp_dev: str,
                                   win_root_dev: str | None, parts_to_check: list) -> None:
        """Last resort: boot QEMU briefly to run bcdboot and rebuild the BCD store.

        v3.2: Uses inspect-os result (win_root_dev) instead of mount-ro to find
        the Windows partition. Falls back to ntfsfix + mount if inspect-os failed.
        """
        import os
        import subprocess
        import shutil

        gf_env = {**os.environ, "LIBGUESTFS_BACKEND": "direct"}

        # Find the Windows partition
        win_part = win_root_dev  # From inspect-os (already validated)

        if not win_part:
            # Fallback: try ntfsfix + mount on each partition
            for part in parts_to_check:
                # ntfsfix to clear dirty journal
                subprocess.run(
                    ["guestfish", "--rw", "-a", qcow2_path, "--",
                     "run", ":",
                     f"ntfsfix {part}"],
                    capture_output=True, text=True, env=gf_env,
                )
                r = subprocess.run(
                    ["guestfish", "--ro", "-a", qcow2_path, "--",
                     "run", ":",
                     f"mount-ro {part} /", ":",
                     "is-dir /Windows/System32"],
                    capture_output=True, text=True, env=gf_env,
                )
                if "true" in r.stdout.lower():
                    win_part = part
                    break

        if not win_part:
            logger.warning("  Cannot find Windows partition with System32 — BCD rebuild impossible")
            return

        logger.info(f"  Windows OS partition: {win_part}")

        # ntfsfix the Windows partition before writing
        subprocess.run(
            ["guestfish", "--rw", "-a", qcow2_path, "--",
             "run", ":",
             f"ntfsfix {win_part}"],
            capture_output=True, text=True, env=gf_env,
        )

        # Inject a firstboot cmd script to run bcdboot + shutdown
        bcdboot_script = (
            '@echo off\r\n'
            'echo [vmware2scw] Rebuilding BCD bootloader...\r\n'
            'bcdboot C:\\Windows /s S: /f UEFI /l en-us\r\n'
            'if %errorlevel% equ 0 (\r\n'
            '  echo [vmware2scw] BCD rebuild SUCCESS\r\n'
            ') else (\r\n'
            '  echo [vmware2scw] BCD rebuild FAILED: %errorlevel%\r\n'
            ')\r\n'
            'echo [vmware2scw] BCDBOOT-DONE\r\n'
            'shutdown /s /t 5 /f\r\n'
        )

        work_dir = Path(qcow2_path).parent / "bcdboot-fix"
        work_dir.mkdir(parents=True, exist_ok=True)
        script_file = work_dir / "fix-bcd.cmd"
        script_file.write_text(bcdboot_script, encoding="utf-8")

        # Upload script via guestfish (mount writable after ntfsfix)
        gf_script = f"""run
mount {win_part} /
upload {script_file} /Windows/vmware2scw-fix-bcd.cmd
"""
        r_upload = subprocess.run(
            ["guestfish", "--rw", "-a", qcow2_path, "--"],
            input=gf_script, capture_output=True, text=True, env=gf_env,
        )
        if r_upload.returncode != 0:
            logger.warning(f"  Failed to upload bcdboot script: {r_upload.stderr.strip()}")
            # Don't return — try QEMU anyway, bcdboot may still work

        # Boot QEMU with EFI firmware to run bcdboot
        ovmf_code = None
        for path in [
            "/usr/share/OVMF/OVMF_CODE_4M.fd",
            "/usr/share/OVMF/OVMF_CODE.fd",
            "/usr/share/qemu/OVMF_CODE.fd",
            "/usr/share/edk2/ovmf/OVMF_CODE.fd",
        ]:
            if Path(path).exists():
                ovmf_code = path
                break

        if not ovmf_code:
            logger.warning("  OVMF firmware not found — cannot run QEMU bcdboot")
            return

        ovmf_vars_src = ovmf_code.replace("CODE", "VARS")
        nvram_copy = str(work_dir / "OVMF_VARS.fd")
        if Path(ovmf_vars_src).exists():
            shutil.copy2(ovmf_vars_src, nvram_copy)
        else:
            subprocess.run(["truncate", "-s", "256K", nvram_copy], check=True)

        serial_log = str(work_dir / "serial.log")
        timeout_s = 300

        qemu_cmd = [
            "qemu-system-x86_64",
            "-machine", "q35,accel=kvm",
            "-cpu", "host",
            "-m", "4096",
            "-smp", "2",
            "-drive", f"if=pflash,format=raw,readonly=on,file={ovmf_code}",
            "-drive", f"if=pflash,format=raw,file={nvram_copy}",
            "-drive", f"file={qcow2_path},format=qcow2,if=virtio,cache=writeback",
            "-serial", f"file:{serial_log}",
            "-display", "none",
            "-no-reboot",
        ]

        logger.info(f"  Starting QEMU for bcdboot (timeout: {timeout_s}s)...")
        try:
            subprocess.run(qemu_cmd, timeout=timeout_s, capture_output=True, text=True)
        except subprocess.TimeoutExpired:
            logger.warning(f"  QEMU timed out after {timeout_s}s — killing")

        # Check serial output
        if Path(serial_log).exists():
            serial_content = Path(serial_log).read_text(errors="replace")
            if "BCDBOOT-DONE" in serial_content:
                logger.info("  ✓ bcdboot completed successfully via QEMU")
            elif "BCD rebuild SUCCESS" in serial_content:
                logger.info("  ✓ BCD rebuild confirmed")
            else:
                logger.warning(f"  bcdboot status unclear — serial: {serial_content[-200:]}")

        # Copy whatever bootloader we now have to the fallback path
        r = subprocess.run(
            ["guestfish", "--ro", "-a", qcow2_path, "--",
             "run", ":",
             f"mount-ro {esp_dev} /", ":",
             "find /"],
            capture_output=True, text=True, env=gf_env,
        )
        efi_files = [f"/{f.strip()}" for f in r.stdout.strip().split("\n")
                     if f.strip().lower().endswith(".efi")]
        logger.info(f"  ESP contents after bcdboot: {efi_files}")

        best_efi = None
        for ef in efi_files:
            if "bootmgfw" in ef.lower():
                best_efi = ef
                break
            if "bootmgr" in ef.lower() and not best_efi:
                best_efi = ef
            if ef.lower().endswith(".efi") and "boot" in ef.lower() and not best_efi:
                best_efi = ef

        if best_efi:
            gf_script = f"""run
mount {esp_dev} /
mkdir-p /EFI/BOOT
cp {best_efi} /EFI/BOOT/BOOTX64.EFI
"""
            subprocess.run(
                ["guestfish", "--rw", "-a", qcow2_path, "--"],
                input=gf_script, capture_output=True, text=True, env=gf_env,
            )
            logger.info("  ✓ UEFI fallback configured after bcdboot rebuild")
        else:
            logger.warning("  ⚠ No .efi bootloader found even after bcdboot — boot may fail")

        shutil.rmtree(work_dir, ignore_errors=True)

    # v2: _stage_fix_network REMOVED from pipeline (was NOOP for both Linux and Windows)
    # Kept as method for backwards compatibility with resume() on in-flight migrations
    def _stage_fix_network(self, plan: VMMigrationPlan, state: MigrationState) -> None:
        """Network adaptation for Scaleway — DEPRECATED in v2.

        Linux: handled in adapt_guest (v2) or fix_bootloader (v1).
        Windows: DHCP already forced in inject_virtio (ensure_all_virtio_drivers).
        """
        from vmware2scw.scaleway.mapping import ResourceMapper

        mapper = ResourceMapper()
        vm_info_dict = state.artifacts.get("vm_info", {})
        guest_os = vm_info_dict.get("guest_os", "")
        os_family, _ = mapper.get_os_family(guest_os)

        if os_family != "windows":
            logger.info("Linux network adaptation already handled in adapt_guest/fix_bootloader stage")
            return

        logger.info("Windows network: DHCP already configured by inject_virtio — skipping")

    def _stage_upload_s3(self, plan: VMMigrationPlan, state: MigrationState) -> None:
        """Upload qcow2 images to Scaleway Object Storage."""
        from vmware2scw.scaleway.s3 import ScalewayS3

        scw_secret = self.config.scaleway.secret_key
        s3 = ScalewayS3(
            region=self.config.scaleway.s3_region,
            access_key=self.config.scaleway.access_key or "",
            secret_key=scw_secret.get_secret_value() if scw_secret else "",
        )

        bucket = self.config.scaleway.s3_bucket
        s3.create_bucket_if_not_exists(bucket)

        s3_keys = []
        for qcow2_path in state.artifacts.get("qcow2_paths", []):
            p = Path(qcow2_path)
            key = f"migrations/{state.migration_id}/{p.name}"

            # Skip if already uploaded with same size
            if s3.check_object_exists(bucket, key):
                remote_size = s3.get_object_size(bucket, key)
                local_size = p.stat().st_size
                if remote_size == local_size:
                    logger.info(f"Skipping upload (already exists): {key}")
                    s3_keys.append(key)
                    continue

            s3.upload_image(qcow2_path, bucket, key)
            s3_keys.append(key)

        state.artifacts["s3_keys"] = s3_keys
        state.artifacts["s3_bucket"] = bucket

    def _stage_import_scw(self, plan: VMMigrationPlan, state: MigrationState) -> None:
        """Import qcow2 image(s) into Scaleway: create snapshot(s) → image.

        Handles multi-disk VMs: boot disk becomes root_volume,
        additional disks become extra_volumes.
        """
        from vmware2scw.scaleway.instance import ScalewayInstanceAPI

        api = ScalewayInstanceAPI(
            access_key=self.config.scaleway.access_key or "",
            secret_key=(self.config.scaleway.secret_key.get_secret_value()
                        if self.config.scaleway.secret_key else ""),
            project_id=self.config.scaleway.project_id,
        )

        zone = plan.zone
        bucket = state.artifacts["s3_bucket"]
        s3_keys = state.artifacts.get("s3_keys", [])

        if not s3_keys:
            raise RuntimeError("No S3 keys found — upload stage may have failed")

        # Import ALL disks as snapshots
        snapshot_ids = []
        for i, s3_key in enumerate(s3_keys):
            disk_label = "boot" if i == 0 else f"data-{i}"
            snap_name = f"vmware2scw-{plan.vm_name}-{state.migration_id}-{disk_label}"

            logger.info(f"Creating Scaleway snapshot ({disk_label}) from s3://{bucket}/{s3_key}")
            snapshot = api.create_snapshot_from_s3(
                zone=zone,
                name=snap_name,
                bucket=bucket,
                key=s3_key,
            )
            snap_id = snapshot["id"]
            snapshot_ids.append(snap_id)

            logger.info(f"Waiting for snapshot {snap_id}...")
            api.wait_for_snapshot(zone, snap_id)
            logger.info(f"Snapshot {snap_id} ({disk_label}) is available")

        state.artifacts["scaleway_snapshot_id"] = snapshot_ids[0]
        state.artifacts["scaleway_snapshot_ids"] = snapshot_ids

        # Create image: boot snapshot + extra volumes
        image_name = f"migrated-{plan.vm_name}"
        logger.info(f"Creating Scaleway image '{image_name}'")
        extra_snaps = snapshot_ids[1:] if len(snapshot_ids) > 1 else None
        image = api.create_image(zone, image_name, snapshot_ids[0],
                                  extra_snapshots=extra_snaps)
        state.artifacts["scaleway_image_id"] = image["id"]

        logger.info(f"Image created: {image['id']}"
                     + (f" ({len(snapshot_ids)} volume(s))" if len(snapshot_ids) > 1 else ""))

    def _stage_verify(self, plan: VMMigrationPlan, state: MigrationState) -> None:
        """Post-migration verification.

        Confidence: 75 — SPÉCULATIF. Basic checks only.
        """
        image_id = state.artifacts.get("scaleway_image_id")
        if image_id:
            logger.info(f"✅ Scaleway image created: {image_id}")
        else:
            logger.warning("⚠️  No Scaleway image ID found — verify manually")

        # TODO: Optionally boot a test instance and check connectivity

    def _stage_cleanup(self, plan: VMMigrationPlan, state: MigrationState) -> None:
        """Clean up all temporary resources to free disk space."""
        import shutil

        # 1. Clean local work directory (VMDK + qcow2 intermediate files)
        work_dir = self.config.conversion.work_dir / state.migration_id
        if work_dir.exists():
            size_gb = sum(f.stat().st_size for f in work_dir.rglob("*") if f.is_file()) / (1024**3)
            logger.info(f"Cleaning work directory: {work_dir} ({size_gb:.1f} GB)")
            shutil.rmtree(work_dir, ignore_errors=True)

        # 2. Clean VMware snapshot
        snap_name = state.artifacts.get("snapshot_name")
        if snap_name:
            try:
                from vmware2scw.vmware.client import VSphereClient
                from vmware2scw.vmware.snapshot import SnapshotManager

                client = VSphereClient()
                pw = self.config.vmware.password.get_secret_value() if self.config.vmware.password else ""
                client.connect(
                    self.config.vmware.vcenter,
                    self.config.vmware.username,
                    pw,
                    insecure=self.config.vmware.insecure,
                )
                snap_mgr = SnapshotManager(client)
                snap_mgr.delete_migration_snapshot(plan.vm_name, snap_name)
                client.disconnect()
                logger.info(f"Deleted VMware snapshot: {snap_name}")
            except Exception as e:
                logger.warning(f"Failed to clean VMware snapshot: {e}")

        # 3. Clean S3 transit files (safe now — image is created)
        image_id = state.artifacts.get("scaleway_image_id")
        s3_keys = state.artifacts.get("s3_keys", [])
        bucket = state.artifacts.get("s3_bucket")
        if image_id and s3_keys and bucket:
            try:
                # v2 FIX: Use correct module name (was vmware2scw.scaleway.storage)
                from vmware2scw.scaleway.s3 import ScalewayS3
                scw_secret = self.config.scaleway.secret_key
                s3 = ScalewayS3(
                    region=self.config.scaleway.s3_region,
                    access_key=self.config.scaleway.access_key or "",
                    secret_key=scw_secret.get_secret_value() if scw_secret else "",
                )
                for key in s3_keys:
                    try:
                        s3.delete_object(bucket, key)
                        logger.info(f"Deleted S3 transit: s3://{bucket}/{key}")
                    except Exception as e2:
                        logger.warning(f"Failed to delete {key}: {e2}")
            except Exception as e:
                logger.warning(f"S3 cleanup failed: {e}")
        else:
            logger.info("S3 transit files retained (image not confirmed or no keys)")

        logger.info("Cleanup complete.")

    # ─── Helper methods ──────────────────────────────────────────────

    def _fix_ntfs_dirty_flag(self, qcow2_path):
        """Fix NTFS dirty flag that prevents write access.

        Windows Hibernation and Fast Startup leave the NTFS filesystem in a
        'dirty' state. virt-v2v and guestfish will refuse to mount read-write.

        Uses qemu-nbd + host ntfsfix for maximum reliability.
        """
        import os
        import subprocess
        import time

        logger.info("Checking/fixing NTFS dirty flag (Fast Startup / Hibernation)...")
        gf_env = {**os.environ, "LIBGUESTFS_BACKEND": "direct"}

        # Method 1: qemu-nbd + ntfsfix (most reliable)
        nbd_dev = "/dev/nbd0"
        subprocess.run(["modprobe", "nbd", "max_part=8"], capture_output=True)
        subprocess.run(["qemu-nbd", "--disconnect", nbd_dev], capture_output=True)

        r = subprocess.run(
            ["qemu-nbd", "--connect", nbd_dev, str(qcow2_path)],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            try:
                time.sleep(1)
                for i in range(1, 8):
                    part = f"{nbd_dev}p{i}"
                    if not os.path.exists(part):
                        continue
                    blkid_r = subprocess.run(
                        ["blkid", "-o", "value", "-s", "TYPE", part],
                        capture_output=True, text=True,
                    )
                    if "ntfs" in blkid_r.stdout.lower():
                        logger.info(f"  Running ntfsfix -d on {part}...")
                        fix_r = subprocess.run(
                            ["ntfsfix", "-d", part],
                            capture_output=True, text=True,
                        )
                        if fix_r.returncode == 0:
                            logger.info(f"  ntfsfix succeeded on {part}")
                        else:
                            logger.warning(f"  ntfsfix on {part}: {fix_r.stderr.strip()[:200]}")
            finally:
                subprocess.run(["qemu-nbd", "--disconnect", nbd_dev], capture_output=True)
        else:
            logger.warning(f"  qemu-nbd not available: {r.stderr.strip()[:200]}")

        # Method 2: Disable Fast Startup via hivex
        try:
            r2 = subprocess.run(
                ["guestfish", "-a", str(qcow2_path), "-i", "--",
                 "download", "/Windows/System32/config/SYSTEM", "/tmp/SYSTEM.ntfsfix"],
                capture_output=True, text=True, env=gf_env,
            )
            if r2.returncode == 0:
                from pathlib import Path as P
                reg_content = (
                    'Windows Registry Editor Version 5.00\n\n'
                    '[HKEY_LOCAL_MACHINE\\\\SYSTEM\\\\ControlSet001\\\\Control\\\\Session Manager\\\\Power]\n'
                    '"HiberbootEnabled"=dword:00000000\n'
                )
                P("/tmp/disable-fastboot.reg").write_text(reg_content)
                subprocess.run(
                    ["hivexregedit", "--merge", "/tmp/SYSTEM.ntfsfix",
                     "--prefix", "HKEY_LOCAL_MACHINE\\SYSTEM",
                     "/tmp/disable-fastboot.reg"],
                    capture_output=True, text=True,
                )
                subprocess.run(
                    ["guestfish", "-a", str(qcow2_path), "-i", "--",
                     "upload", "/tmp/SYSTEM.ntfsfix",
                     "/Windows/System32/config/SYSTEM"],
                    capture_output=True, text=True, env=gf_env,
                )
                logger.info("  Disabled Windows Fast Startup (HiberbootEnabled=0)")
        except Exception as e:
            logger.debug(f"  Fast Startup disable attempt: {e}")

    def _find_windows_os_disk(self, qcow2_paths: list[str]) -> int:
        """Detect which disk contains the Windows OS.

        Returns the index of the OS disk in qcow2_paths.
        Falls back to 0 (first disk) if detection fails.

        This solves the WIN2016-EFI problem where disk-0 is a small 20GB
        ESP/boot partition and disk-1 is the actual 90GB Windows OS volume.
        guestfish -i and virt-v2v fail when pointed at a non-OS disk.

        v3.1: Uses multiple detection strategies because guestfish mount-ro
        fails silently on dirty NTFS (common after VMware snapshots):
          1. virt-filesystems: list filesystems without mounting
          2. guestfish inspect-os: OS detection heuristics
          3. qemu-img actual size: largest actual-size disk with NTFS
        """
        import os
        import subprocess
        import json

        if len(qcow2_paths) <= 1:
            return 0

        gf_env = {**os.environ, "LIBGUESTFS_BACKEND": "direct"}
        logger.info(f"  Multi-disk VM ({len(qcow2_paths)} disks) — detecting Windows OS disk...")

        # ── Strategy 1: virt-filesystems to find NTFS partitions ──
        # This doesn't require mounting and works on dirty NTFS
        disk_scores = {}  # idx -> score
        for idx, qcow2 in enumerate(qcow2_paths):
            disk_name = Path(qcow2).name
            disk_scores[idx] = 0

            try:
                r = subprocess.run(
                    ["virt-filesystems", "-a", qcow2, "--filesystems", "--long",
                     "-h", "--no-title"],
                    capture_output=True, text=True, env=gf_env, timeout=60,
                )
                lines = [l.strip() for l in r.stdout.strip().split("\n") if l.strip()]
                has_ntfs = False
                ntfs_size_gb = 0
                for line in lines:
                    parts = line.split()
                    if len(parts) >= 3 and "ntfs" in parts[1].lower():
                        has_ntfs = True
                        # Parse size (e.g. "89G", "20G", "500M")
                        size_str = parts[2] if len(parts) > 2 else "0"
                        try:
                            if "G" in size_str:
                                ntfs_size_gb += float(size_str.replace("G", ""))
                            elif "M" in size_str:
                                ntfs_size_gb += float(size_str.replace("M", "")) / 1024
                            elif "T" in size_str:
                                ntfs_size_gb += float(size_str.replace("T", "")) * 1024
                        except ValueError:
                            pass

                if has_ntfs:
                    # Score based on NTFS partition size (larger = more likely to be OS)
                    disk_scores[idx] += 10 + ntfs_size_gb
                    logger.info(f"  → {disk_name}: NTFS found ({ntfs_size_gb:.0f} GB)")
                else:
                    logger.info(f"  → {disk_name}: no NTFS filesystem")
            except (subprocess.TimeoutExpired, Exception) as e:
                logger.warning(f"  → {disk_name}: virt-filesystems failed: {e}")

        # ── Strategy 2: guestfish inspect-os (works even on dirty NTFS) ──
        for idx, qcow2 in enumerate(qcow2_paths):
            disk_name = Path(qcow2).name
            try:
                r = subprocess.run(
                    ["guestfish", "--ro", "-a", qcow2, "--",
                     "run", ":", "inspect-os"],
                    capture_output=True, text=True, env=gf_env, timeout=60,
                )
                os_roots = [l.strip() for l in r.stdout.strip().split("\n") if l.strip()]
                if os_roots:
                    # inspect-os found an OS root — strong signal
                    disk_scores[idx] += 100
                    logger.info(f"  → {disk_name}: inspect-os found OS root: {os_roots}")
            except (subprocess.TimeoutExpired, Exception) as e:
                logger.debug(f"  → {disk_name}: inspect-os failed: {e}")

        # ── Strategy 3: qemu-img actual size (fallback heuristic) ──
        for idx, qcow2 in enumerate(qcow2_paths):
            disk_name = Path(qcow2).name
            try:
                r = subprocess.run(
                    ["qemu-img", "info", "--output=json", qcow2],
                    capture_output=True, text=True, timeout=10,
                )
                info = json.loads(r.stdout)
                actual_size = info.get("actual-size", 0)
                virtual_size = info.get("virtual-size", 0)
                # An OS disk typically has significant actual data (> 1GB)
                actual_gb = actual_size / (1024**3)
                if actual_gb > 1.0 and disk_scores.get(idx, 0) > 0:
                    # Only boost if we also found NTFS (avoid false positives on data disks)
                    disk_scores[idx] += actual_gb
            except Exception:
                pass

        # ── Pick the best candidate ──
        if disk_scores:
            best_idx = max(disk_scores, key=disk_scores.get)
            best_score = disk_scores[best_idx]
            if best_score > 0:
                logger.info(f"  → OS disk detected: disk-{best_idx} "
                            f"({Path(qcow2_paths[best_idx]).name}, score={best_score:.0f})")
                return best_idx

        logger.warning("  Could not detect Windows OS disk — defaulting to disk-0")
        return 0

    def _inject_virtio_fallback(self, boot_disk, os_family):
        """Fallback VirtIO injection when virt-v2v fails."""
        if os_family == "windows":
            # Use offline driver injection (registry + sys files) for Windows
            from vmware2scw.converter.windows_virtio import inject_virtio_windows
            virtio_iso = self.config.conversion.virtio_win_iso
            if not virtio_iso or not Path(virtio_iso).exists():
                raise RuntimeError(
                    "virtio-win ISO is required for Windows VMs.\n"
                    "  wget -O /opt/virtio-win.iso "
                    "https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/stable-virtio/virtio-win.iso\n"
                    "  Then in migration.yaml: conversion.virtio_win_iso: /opt/virtio-win.iso"
                )
            inject_virtio_windows(
                str(boot_disk),
                str(virtio_iso),
                work_dir=boot_disk.parent / "virtio-work",
            )
        else:
            from vmware2scw.converter.disk import VirtIOInjector
            injector = VirtIOInjector(
                virtio_win_iso=self.config.conversion.virtio_win_iso
            )
            injector.inject(str(boot_disk), os_family=os_family)

    def _ensure_rhsrvany(self):
        """Ensure rhsrvany.exe is installed for Windows virt-v2v conversions.

        On Ubuntu/Debian, virt-v2v requires rhsrvany.exe + pnp_wait.exe
        in /usr/share/virt-tools/ but these are not packaged. We extract
        them from the Fedora mingw32-srvany RPM.

        Ref: https://github.com/rwmjones/rhsrvany
        """
        import subprocess
        virt_tools = Path("/usr/share/virt-tools")
        rhsrvany = virt_tools / "rhsrvany.exe"

        if rhsrvany.exists():
            logger.info(f"rhsrvany.exe already present at {rhsrvany}")
            return

        logger.info("Installing rhsrvany.exe (required by virt-v2v for Windows)...")
        virt_tools.mkdir(parents=True, exist_ok=True)

        # Install rpm2cpio if needed
        subprocess.run(["apt-get", "install", "-y", "-qq", "rpm2cpio"], check=False, capture_output=True)

        rpm_url = "https://kojipkgs.fedoraproject.org//packages/mingw-srvany/1.1/4.fc38/noarch/mingw32-srvany-1.1-4.fc38.noarch.rpm"
        tmp_rpm = Path("/tmp/srvany.rpm")

        try:
            subprocess.run(["wget", "-q", "-O", str(tmp_rpm), rpm_url], check=True)
            # Extract exe files from RPM
            result = subprocess.run(
                f"cd /tmp && rpm2cpio {tmp_rpm} | cpio -idmv 2>&1",
                shell=True, capture_output=True, text=True,
            )
            # Find and copy the exe files
            import glob
            for exe in glob.glob("/tmp/usr/**/bin/*.exe", recursive=True):
                dest = virt_tools / Path(exe).name
                subprocess.run(["cp", exe, str(dest)], check=True)
                logger.info(f"  Installed {dest}")
        except Exception as e:
            logger.warning(f"Failed to install rhsrvany.exe: {e}")
            logger.warning("Windows virt-v2v conversion may fail. Install manually:")
            logger.warning(f"  wget -O /tmp/srvany.rpm {rpm_url}")
            logger.warning("  cd /tmp && rpm2cpio srvany.rpm | cpio -idmv")
            logger.warning("  cp /tmp/usr/*/bin/*.exe /usr/share/virt-tools/")
        finally:
            tmp_rpm.unlink(missing_ok=True)

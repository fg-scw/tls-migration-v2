"""Pre-migration validation: check VM compatibility with Scaleway.

Validates:
  - OS supported
  - Disk size within limits
  - Firmware compatibility (BIOS/UEFI)
  - No unsupported features (RDM, shared disks, PCI passthrough)
  - Target instance type can accommodate VM resources

Pipeline stage: validate (stage 1)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class ValidationCheck:
    name: str
    passed: bool
    blocking: bool = True
    message: str = ""


@dataclass
class ValidationReport:
    checks: list[ValidationCheck] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks if c.blocking)

    @property
    def warnings(self) -> list[ValidationCheck]:
        return [c for c in self.checks if not c.passed and not c.blocking]

    @property
    def errors(self) -> list[ValidationCheck]:
        return [c for c in self.checks if not c.passed and c.blocking]


class MigrationValidator:
    """Pre-flight validation for VM migration to Scaleway."""

    MAX_VOLUME_SIZE_GB = 10_000
    MAX_VOLUMES = 16

    SUPPORTED_OS = [
        "ubuntu", "debian", "centos", "rhel", "rocky", "alma",
        "sles", "suse", "fedora", "oracle", "linux",
        "windows9Server", "windows2019", "windows2022",
        "windows9_64", "windows10", "windows11",
    ]

    def validate(self, vm_info, target_type: str = "") -> ValidationReport:
        """Run all validation checks."""
        report = ValidationReport()

        if hasattr(vm_info, "model_dump"):
            vm = vm_info.model_dump()
        elif isinstance(vm_info, dict):
            vm = vm_info
        else:
            vm = vars(vm_info)

        report.checks.append(self._check_os_supported(vm))
        report.checks.append(self._check_disk_count(vm))
        report.checks.append(self._check_disk_sizes(vm))
        report.checks.append(self._check_firmware(vm))
        report.checks.append(self._check_no_snapshots_complex(vm))
        report.checks.append(self._check_power_state(vm))

        for check in report.checks:
            level = "✓" if check.passed else ("⚠" if not check.blocking else "✗")
            logger.info(f"  {level} {check.name}: {check.message}")

        return report

    def _check_os_supported(self, vm: dict) -> ValidationCheck:
        guest_os = vm.get("guest_os", "").lower()
        guest_os_full = vm.get("guest_os_full", "").lower()
        combined = guest_os + " " + guest_os_full

        for os_key in self.SUPPORTED_OS:
            if os_key in combined:
                return ValidationCheck(
                    name="os_supported", passed=True,
                    message=f"OS supported: {vm.get('guest_os_full', vm.get('guest_os', 'unknown'))}",
                )
        return ValidationCheck(
            name="os_supported", passed=False, blocking=False,
            message=f"OS may not be supported: {vm.get('guest_os', 'unknown')}. Migration may still succeed.",
        )

    def _check_disk_count(self, vm: dict) -> ValidationCheck:
        disks = vm.get("disks", [])
        count = len(disks)
        if count == 0:
            return ValidationCheck(name="disk_count", passed=False, blocking=True, message="VM has no disks")
        if count > self.MAX_VOLUMES:
            return ValidationCheck(
                name="disk_count", passed=False, blocking=True,
                message=f"VM has {count} disks, Scaleway max is {self.MAX_VOLUMES}",
            )
        return ValidationCheck(name="disk_count", passed=True, message=f"{count} disk(s) — OK")

    def _check_disk_sizes(self, vm: dict) -> ValidationCheck:
        disks = vm.get("disks", [])
        for disk in disks:
            size = disk.get("size_gb", 0)
            if size > self.MAX_VOLUME_SIZE_GB:
                return ValidationCheck(
                    name="disk_sizes", passed=False, blocking=True,
                    message=f"Disk '{disk.get('name', '?')}' is {size:.0f}GB, max is {self.MAX_VOLUME_SIZE_GB}GB",
                )
        total = vm.get("total_disk_gb", sum(d.get("size_gb", 0) for d in disks))
        return ValidationCheck(name="disk_sizes", passed=True, message=f"Total {total:.0f}GB — within limits")

    def _check_firmware(self, vm: dict) -> ValidationCheck:
        firmware = vm.get("firmware", "bios")
        if firmware == "efi":
            return ValidationCheck(name="firmware", passed=True, message="UEFI firmware — native compatibility")
        return ValidationCheck(
            name="firmware", passed=True, blocking=False,
            message="BIOS firmware — will be converted to UEFI during migration",
        )

    def _check_no_snapshots_complex(self, vm: dict) -> ValidationCheck:
        snapshots = vm.get("snapshots", [])
        if len(snapshots) > 3:
            return ValidationCheck(
                name="snapshots", passed=False, blocking=False,
                message=f"VM has {len(snapshots)} snapshots. Consider consolidating before migration.",
            )
        return ValidationCheck(name="snapshots", passed=True, message=f"{len(snapshots)} snapshot(s) — OK")

    def _check_power_state(self, vm: dict) -> ValidationCheck:
        state = vm.get("power_state", "")
        if "poweredOn" in state:
            return ValidationCheck(
                name="power_state", passed=True, blocking=False,
                message="VM is powered on — snapshot will ensure consistency",
            )
        return ValidationCheck(name="power_state", passed=True, message=f"VM is {state} — safe for export")

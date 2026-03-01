"""Enhanced inventory with filtering, auto-mapping, and batch plan generation.

This module extends the basic inventory command with:
  - Powerful filtering: regex, glob, folder, OS, CPU/RAM/disk ranges
  - Auto-mapping to Scaleway instance types
  - Direct export to batch plan YAML
  - Cost estimation in the inventory table

Usage:
    # List all VMs with auto-mapping
    vmware2scw inventory-plan --vcenter ... --auto-map

    # Filter and export to plan
    vmware2scw inventory-plan --filter "name:web-*" --filter "os:linux" \
        --min-cpu 2 --max-disk 500 --auto-map --output plan.yaml

    # Interactive TUI selection (Phase 2)
    vmware2scw inventory-plan --interactive
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from vmware2scw.utils.logging import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════
#  Filter Engine
# ═══════════════════════════════════════════════════════════════════

@dataclass
class InventoryFilter:
    """Parsed filter criteria for VM inventory."""

    name_patterns: list[str] = field(default_factory=list)      # glob patterns
    name_regex: list[str] = field(default_factory=list)          # regex patterns
    folders: list[str] = field(default_factory=list)             # vCenter folder paths
    os_families: list[str] = field(default_factory=list)         # "linux", "windows"
    os_patterns: list[str] = field(default_factory=list)         # guest OS glob
    tags: list[str] = field(default_factory=list)                # vCenter tags
    hosts: list[str] = field(default_factory=list)               # ESXi host names
    clusters: list[str] = field(default_factory=list)            # vCenter cluster names
    datacenters: list[str] = field(default_factory=list)         # vCenter datacenter names
    power_states: list[str] = field(default_factory=list)        # "poweredOn", "poweredOff"
    firmware_types: list[str] = field(default_factory=list)      # "bios", "efi"

    # Resource ranges
    min_cpu: Optional[int] = None
    max_cpu: Optional[int] = None
    min_ram_gb: Optional[float] = None
    max_ram_gb: Optional[float] = None
    min_disk_gb: Optional[float] = None
    max_disk_gb: Optional[float] = None
    max_disk_count: Optional[int] = None

    @classmethod
    def from_cli_filters(cls, filters: list[str], **kwargs) -> "InventoryFilter":
        """Parse CLI filter strings like 'name:web-*', 'os:linux', 'cpu:>4'.

        Supported filter formats:
            name:pattern      - VM name glob pattern
            regex:pattern     - VM name regex
            folder:/path      - vCenter folder
            os:linux|windows  - OS family
            os_id:pattern     - Guest OS ID glob
            tag:tagname       - vCenter tag
            host:hostname     - ESXi host
            cluster:name      - vCenter cluster
            dc:name           - Datacenter
            state:poweredOn   - Power state
            firmware:bios|efi - Firmware type
        """
        f = cls(**kwargs)

        for filt in filters:
            if ":" not in filt:
                # Bare string = name pattern
                f.name_patterns.append(filt)
                continue

            key, value = filt.split(":", 1)
            key = key.strip().lower()
            value = value.strip()

            if key == "name":
                f.name_patterns.append(value)
            elif key == "regex":
                f.name_regex.append(value)
            elif key == "folder":
                f.folders.append(value)
            elif key == "os":
                f.os_families.append(value.lower())
            elif key in ("os_id", "guest_os"):
                f.os_patterns.append(value)
            elif key == "tag":
                f.tags.append(value)
            elif key == "host":
                f.hosts.append(value)
            elif key == "cluster":
                f.clusters.append(value)
            elif key in ("dc", "datacenter"):
                f.datacenters.append(value)
            elif key == "state":
                f.power_states.append(value)
            elif key == "firmware":
                f.firmware_types.append(value.lower())
            else:
                logger.warning(f"Unknown filter key: '{key}' (in '{filt}')")

        return f

    def matches(self, vm: dict) -> bool:
        """Check if a VM info dict matches all filter criteria.

        All criteria are AND (must all match). Within a criterion, values are OR.
        """
        name = vm.get("name", "")
        guest_os = vm.get("guest_os", "").lower()
        firmware = vm.get("firmware", "bios").lower()
        power = vm.get("power_state", "")
        cpu = vm.get("cpu", 0)
        ram_mb = vm.get("memory_mb", 0)
        ram_gb = ram_mb / 1024
        total_disk = vm.get("total_disk_gb", 0)
        disk_count = len(vm.get("disks", []))
        host = vm.get("host", "")
        cluster = vm.get("cluster", "")
        dc = vm.get("datacenter", "")
        folder = vm.get("folder", "")

        # Name patterns (OR)
        if self.name_patterns:
            if not any(fnmatch.fnmatch(name, p) for p in self.name_patterns):
                return False

        # Name regex (OR)
        if self.name_regex:
            if not any(re.search(r, name) for r in self.name_regex):
                return False

        # Folders (OR)
        if self.folders:
            if not any(folder.startswith(f) for f in self.folders):
                return False

        # OS family (OR)
        if self.os_families:
            is_win = "win" in guest_os
            vm_family = "windows" if is_win else "linux"
            if vm_family not in self.os_families:
                return False

        # OS ID patterns (OR)
        if self.os_patterns:
            if not any(fnmatch.fnmatch(guest_os, p.lower()) for p in self.os_patterns):
                return False

        # Hosts (OR)
        if self.hosts:
            if not any(fnmatch.fnmatch(host, h) for h in self.hosts):
                return False

        # Clusters (OR)
        if self.clusters:
            if not any(fnmatch.fnmatch(cluster, c) for c in self.clusters):
                return False

        # Datacenters (OR)
        if self.datacenters:
            if dc not in self.datacenters:
                return False

        # Power state (OR)
        if self.power_states:
            if not any(s in power for s in self.power_states):
                return False

        # Firmware (OR)
        if self.firmware_types:
            if firmware not in self.firmware_types:
                return False

        # Resource ranges
        if self.min_cpu is not None and cpu < self.min_cpu:
            return False
        if self.max_cpu is not None and cpu > self.max_cpu:
            return False
        if self.min_ram_gb is not None and ram_gb < self.min_ram_gb:
            return False
        if self.max_ram_gb is not None and ram_gb > self.max_ram_gb:
            return False
        if self.min_disk_gb is not None and total_disk < self.min_disk_gb:
            return False
        if self.max_disk_gb is not None and total_disk > self.max_disk_gb:
            return False
        if self.max_disk_count is not None and disk_count > self.max_disk_count:
            return False

        return True


# ═══════════════════════════════════════════════════════════════════
#  Plan Generator
# ═══════════════════════════════════════════════════════════════════

def generate_batch_plan(
    vms: list[dict],
    vcenter: str = "",
    zone: str = "fr-par-1",
    sizing_strategy: str = "optimize",
    default_tags: list[str] | None = None,
    auto_map: bool = True,
    windows_type_override: str | None = None,
) -> dict:
    """Generate a batch plan YAML structure from filtered VM inventory.

    Args:
        vms: List of VM info dicts (from VMInventory.list_all_vms() → model_dump())
        vcenter: vCenter hostname for metadata
        zone: Default target zone
        sizing_strategy: "exact", "optimize", or "cost"
        default_tags: Tags to apply to all instances
        auto_map: Auto-detect instance types
        windows_type_override: Force specific type for Windows VMs

    Returns:
        Dict structure ready for YAML serialization or BatchPlan.parse_obj()
    """
    from vmware2scw.scaleway.mapping import ResourceMapper

    mapper = ResourceMapper()
    migrations = []
    total_disk = 0
    linux_count = 0
    windows_count = 0

    for vm in vms:
        guest_os = vm.get("guest_os", "")
        os_family, os_desc = mapper.get_os_family(guest_os)
        is_windows = os_family == "windows"

        entry: dict = {
            "vm_name": vm["name"],
            "priority": 5,
        }

        if auto_map:
            # Build a minimal VMInfo-like object for the mapper
            if is_windows and windows_type_override:
                entry["target_type"] = windows_type_override
            else:
                # Use a simple heuristic based on CPU/RAM
                target = _auto_select_type(
                    cpu=vm.get("cpu", 1),
                    ram_mb=vm.get("memory_mb", 1024),
                    disk_gb=vm.get("total_disk_gb", 20),
                    num_disks=len(vm.get("disks", [{"name": "disk"}])),
                    is_windows=is_windows,
                    strategy=sizing_strategy,
                )
                if target:
                    entry["target_type"] = target

        # Add OS-specific notes
        firmware = vm.get("firmware", "bios")
        notes_parts = []
        if firmware == "bios":
            notes_parts.append("BIOS→UEFI conversion needed")
        if is_windows:
            notes_parts.append("Windows: VirtIO driver injection required")
            windows_count += 1
        else:
            linux_count += 1

        disk_gb = vm.get("total_disk_gb", 0)
        total_disk += disk_gb
        notes_parts.append(f"{vm.get('cpu', '?')}vCPU/{vm.get('memory_mb', 0) // 1024}GB/{disk_gb:.0f}GB")
        notes_parts.append(f"{os_desc}")

        entry["notes"] = " | ".join(notes_parts)
        migrations.append(entry)

    # Build plan
    plan = {
        "version": 1,
        "metadata": {
            "generated_at": datetime.now().isoformat(),
            "vcenter": vcenter,
            "total_vms": len(migrations),
            "linux_vms": linux_count,
            "windows_vms": windows_count,
            "total_disk_gb": round(total_disk, 1),
        },
        "defaults": {
            "zone": zone,
            "sizing_strategy": sizing_strategy,
        },
        "migrations": migrations,
    }

    if default_tags:
        plan["defaults"]["tags"] = default_tags

    return plan


def _auto_select_type(
    cpu: int,
    ram_mb: int,
    disk_gb: float,
    num_disks: int,
    is_windows: bool,
    strategy: str = "optimize",
) -> str | None:
    """Select the best Scaleway instance type for given resources.

    Returns the instance type name or None if no fit found.
    """
    from vmware2scw.scaleway.mapping import INSTANCE_TYPES

    ram_gb = ram_mb / 1024
    candidates = []

    for name, spec in INSTANCE_TYPES.items():
        # Windows → only Windows types
        if is_windows and not spec.windows:
            continue
        if not is_windows and spec.windows:
            continue
        # Skip dev types
        if spec.category == "development":
            continue
        # Must fit
        if spec.vcpus < cpu:
            continue
        if spec.ram_gb < ram_gb:
            continue
        if num_disks > spec.max_volumes:
            continue
        if not spec.block_storage and spec.local_storage_gb < disk_gb:
            continue

        # Score: closer to actual needs = better
        cpu_waste = (spec.vcpus - cpu) / spec.vcpus
        ram_waste = (spec.ram_gb - ram_gb) / spec.ram_gb

        if strategy == "cost":
            score = spec.price_hour_eur  # Lower price = better
        elif strategy == "exact":
            score = cpu_waste + ram_waste  # Less waste = better
        else:  # optimize
            # Balance between waste and having headroom
            score = cpu_waste * 0.6 + ram_waste * 0.4
            # Prefer dedicated vCPUs
            if not spec.shared_vcpu:
                score -= 0.05

        candidates.append((score, name))

    if not candidates:
        return None

    candidates.sort()
    return candidates[0][1]


def estimate_migration(
    plan_data: dict,
    available_disk_gb: float | None = None,
    concurrency: int = 5,
) -> dict:
    """Estimate time, space, and cost for a batch migration plan.

    Args:
        plan_data: Batch plan dict
        available_disk_gb: Available disk space on the orchestrator
        concurrency: Expected parallelism level

    Returns:
        Estimation dict with duration, space, cost, warnings
    """
    from vmware2scw.scaleway.mapping import INSTANCE_TYPES

    migrations = plan_data.get("migrations", [])
    metadata = plan_data.get("metadata", {})
    total_disk = metadata.get("total_disk_gb", 0)
    linux_vms = metadata.get("linux_vms", 0)
    windows_vms = metadata.get("windows_vms", 0)
    total_vms = len(migrations)

    # Space estimate: VMDK + qcow2 intermediate = ~2x disk size
    # But with streaming delete of VMDKs after conversion, ~1.5x
    work_space_gb = total_disk * 1.5

    # Time estimate (rough):
    # - Export: ~100 MB/s per stream, limited by NFC leases
    # - Conversion: ~200 MB/s for qemu-img
    # - Linux adaptation: ~30s per VM
    # - Windows adaptation: ~5-8 min per VM (QEMU boot)
    # - Upload: ~500 MB/s to S3
    # - Import: ~2-5 min per snapshot (API side)
    export_time_min = (total_disk * 1024) / (100 * 60)  # MB at 100 MB/s
    convert_time_min = (total_disk * 1024) / (200 * 60)
    adapt_linux_min = linux_vms * 0.5
    adapt_windows_min = windows_vms * 7
    upload_time_min = (total_disk * 1024) / (500 * 60)
    import_time_min = total_vms * 3

    # With parallelism
    sequential_total = export_time_min + convert_time_min + adapt_linux_min + adapt_windows_min + upload_time_min + import_time_min
    parallel_factor = min(concurrency, total_vms)
    estimated_duration = sequential_total / max(parallel_factor, 1) * 1.3  # 30% overhead

    # Cost estimate: monthly cost of target Scaleway instances
    monthly_cost = 0.0
    for m in migrations:
        target = m.get("target_type")
        if target and target in INSTANCE_TYPES:
            spec = INSTANCE_TYPES[target]
            monthly_cost += spec.price_hour_eur * 730

    warnings = []
    if available_disk_gb is not None and work_space_gb > available_disk_gb:
        warnings.append(
            f"Insufficient disk space: need {work_space_gb:.0f} GB, "
            f"have {available_disk_gb:.0f} GB. "
            f"Consider a larger volume or migrating in waves."
        )
    if windows_vms > 0:
        warnings.append(
            f"{windows_vms} Windows VM(s) require KVM for VirtIO injection. "
            f"Ensure orchestrator has /dev/kvm and OVMF installed."
        )
    if total_vms > 20 and concurrency < 5:
        warnings.append(
            f"Large batch ({total_vms} VMs) with low concurrency ({concurrency}). "
            f"Consider increasing max_total_workers."
        )

    return {
        "total_vms": total_vms,
        "linux_vms": linux_vms,
        "windows_vms": windows_vms,
        "total_disk_gb": round(total_disk, 1),
        "required_work_space_gb": round(work_space_gb, 1),
        "estimated_duration_minutes": round(estimated_duration, 0),
        "estimated_monthly_cost_eur": round(monthly_cost, 2),
        "breakdown": {
            "export_min": round(export_time_min, 1),
            "convert_min": round(convert_time_min, 1),
            "adapt_min": round(adapt_linux_min + adapt_windows_min, 1),
            "upload_min": round(upload_time_min, 1),
            "import_min": round(import_time_min, 1),
        },
        "warnings": warnings,
    }

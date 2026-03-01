"""Scaleway instance type catalog and resource mapping.

Provides:
  - INSTANCE_TYPES: Complete catalog of Scaleway instance types with specs
  - ResourceMapper: Maps VMware VM specs to optimal Scaleway instance types

The catalog is used by:
  - inventory.py: Auto-mapping during plan generation
  - batch_plan.py: Cost estimation
  - batch_orchestrator.py: Validation before migration
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class InstanceTypeSpec:
    """Specification for a Scaleway instance type."""
    vcpus: int
    ram_gb: float
    category: str              # "general", "compute", "memory", "gpu", "development"
    price_hour_eur: float
    block_storage: bool        # Supports SBS block storage
    local_storage_gb: float    # Local NVMe if any (0 = SBS only)
    max_volumes: int           # Max additional block volumes
    max_volume_size_gb: int    # Max size per block volume
    bandwidth_gbps: float
    shared_vcpu: bool = False  # Shared vs dedicated vCPUs
    windows: bool = False      # Windows-compatible (UEFI+VirtIO preinstalled)
    gpu: str = ""              # GPU model if any
    arch: str = "x86_64"


# ═══════════════════════════════════════════════════════════════════
#  Instance Type Catalog (February 2026)
# ═══════════════════════════════════════════════════════════════════
# Source: https://www.scaleway.com/en/pricing/?tags=compute
# Updated manually — sync with `vmware2scw catalog update` if needed

INSTANCE_TYPES: dict[str, InstanceTypeSpec] = {
    # ── PLAY2 (Development / Shared vCPU) ────────────────────────
    "PLAY2-NANO": InstanceTypeSpec(
        vcpus=1, ram_gb=1, category="development",
        price_hour_eur=0.0070, block_storage=True,
        local_storage_gb=0, max_volumes=1, max_volume_size_gb=400,
        bandwidth_gbps=0.1, shared_vcpu=True,
    ),
    "PLAY2-MICRO": InstanceTypeSpec(
        vcpus=2, ram_gb=2, category="development",
        price_hour_eur=0.0140, block_storage=True,
        local_storage_gb=0, max_volumes=2, max_volume_size_gb=400,
        bandwidth_gbps=0.2, shared_vcpu=True,
    ),
    "PLAY2-SMALL": InstanceTypeSpec(
        vcpus=2, ram_gb=4, category="development",
        price_hour_eur=0.0280, block_storage=True,
        local_storage_gb=0, max_volumes=4, max_volume_size_gb=400,
        bandwidth_gbps=0.4, shared_vcpu=True,
    ),
    "PLAY2-MEDIUM": InstanceTypeSpec(
        vcpus=4, ram_gb=8, category="development",
        price_hour_eur=0.0560, block_storage=True,
        local_storage_gb=0, max_volumes=4, max_volume_size_gb=400,
        bandwidth_gbps=0.8, shared_vcpu=True,
    ),

    # ── PRO2 (General Purpose) ───────────────────────────────────
    "PRO2-XXS": InstanceTypeSpec(
        vcpus=2, ram_gb=8, category="general",
        price_hour_eur=0.0660, block_storage=True,
        local_storage_gb=0, max_volumes=16, max_volume_size_gb=10000,
        bandwidth_gbps=0.5,
    ),
    "PRO2-XS": InstanceTypeSpec(
        vcpus=4, ram_gb=16, category="general",
        price_hour_eur=0.1320, block_storage=True,
        local_storage_gb=0, max_volumes=16, max_volume_size_gb=10000,
        bandwidth_gbps=1.0,
    ),
    "PRO2-S": InstanceTypeSpec(
        vcpus=8, ram_gb=32, category="general",
        price_hour_eur=0.2640, block_storage=True,
        local_storage_gb=0, max_volumes=16, max_volume_size_gb=10000,
        bandwidth_gbps=2.0,
    ),
    "PRO2-M": InstanceTypeSpec(
        vcpus=16, ram_gb=64, category="general",
        price_hour_eur=0.5280, block_storage=True,
        local_storage_gb=0, max_volumes=16, max_volume_size_gb=10000,
        bandwidth_gbps=4.0,
    ),
    "PRO2-L": InstanceTypeSpec(
        vcpus=32, ram_gb=128, category="general",
        price_hour_eur=1.0560, block_storage=True,
        local_storage_gb=0, max_volumes=16, max_volume_size_gb=10000,
        bandwidth_gbps=8.0,
    ),

    # ── POP2 (Performance / Local NVMe) ──────────────────────────
    "POP2-2C-8G": InstanceTypeSpec(
        vcpus=2, ram_gb=8, category="compute",
        price_hour_eur=0.0770, block_storage=True,
        local_storage_gb=50, max_volumes=16, max_volume_size_gb=10000,
        bandwidth_gbps=0.4,
    ),
    "POP2-4C-16G": InstanceTypeSpec(
        vcpus=4, ram_gb=16, category="compute",
        price_hour_eur=0.1540, block_storage=True,
        local_storage_gb=100, max_volumes=16, max_volume_size_gb=10000,
        bandwidth_gbps=0.8,
    ),
    "POP2-8C-32G": InstanceTypeSpec(
        vcpus=8, ram_gb=32, category="compute",
        price_hour_eur=0.3080, block_storage=True,
        local_storage_gb=200, max_volumes=16, max_volume_size_gb=10000,
        bandwidth_gbps=1.6,
    ),
    "POP2-16C-64G": InstanceTypeSpec(
        vcpus=16, ram_gb=64, category="compute",
        price_hour_eur=0.6160, block_storage=True,
        local_storage_gb=400, max_volumes=16, max_volume_size_gb=10000,
        bandwidth_gbps=3.2,
    ),
    "POP2-32C-128G": InstanceTypeSpec(
        vcpus=32, ram_gb=128, category="compute",
        price_hour_eur=1.2320, block_storage=True,
        local_storage_gb=800, max_volumes=16, max_volume_size_gb=10000,
        bandwidth_gbps=6.4,
    ),

    # ── POP2 High-Memory ─────────────────────────────────────────
    "POP2-HM-2C-16G": InstanceTypeSpec(
        vcpus=2, ram_gb=16, category="memory",
        price_hour_eur=0.0990, block_storage=True,
        local_storage_gb=50, max_volumes=16, max_volume_size_gb=10000,
        bandwidth_gbps=0.4,
    ),
    "POP2-HM-4C-32G": InstanceTypeSpec(
        vcpus=4, ram_gb=32, category="memory",
        price_hour_eur=0.1980, block_storage=True,
        local_storage_gb=100, max_volumes=16, max_volume_size_gb=10000,
        bandwidth_gbps=0.8,
    ),
    "POP2-HM-8C-64G": InstanceTypeSpec(
        vcpus=8, ram_gb=64, category="memory",
        price_hour_eur=0.3960, block_storage=True,
        local_storage_gb=200, max_volumes=16, max_volume_size_gb=10000,
        bandwidth_gbps=1.6,
    ),
    "POP2-HM-16C-128G": InstanceTypeSpec(
        vcpus=16, ram_gb=128, category="memory",
        price_hour_eur=0.7920, block_storage=True,
        local_storage_gb=400, max_volumes=16, max_volume_size_gb=10000,
        bandwidth_gbps=3.2,
    ),
    "POP2-HM-32C-256G": InstanceTypeSpec(
        vcpus=32, ram_gb=256, category="memory",
        price_hour_eur=1.5840, block_storage=True,
        local_storage_gb=800, max_volumes=16, max_volume_size_gb=10000,
        bandwidth_gbps=6.4,
    ),
    "POP2-HM-64C-512G": InstanceTypeSpec(
        vcpus=64, ram_gb=512, category="memory",
        price_hour_eur=3.1680, block_storage=True,
        local_storage_gb=1600, max_volumes=16, max_volume_size_gb=10000,
        bandwidth_gbps=12.8,
    ),

    # ── POP2 Windows ─────────────────────────────────────────────
    "POP2-4C-16G-WIN": InstanceTypeSpec(
        vcpus=4, ram_gb=16, category="compute",
        price_hour_eur=0.2200, block_storage=True,
        local_storage_gb=100, max_volumes=16, max_volume_size_gb=10000,
        bandwidth_gbps=0.8, windows=True,
    ),
    "POP2-8C-32G-WIN": InstanceTypeSpec(
        vcpus=8, ram_gb=32, category="compute",
        price_hour_eur=0.4400, block_storage=True,
        local_storage_gb=200, max_volumes=16, max_volume_size_gb=10000,
        bandwidth_gbps=1.6, windows=True,
    ),
    "POP2-16C-64G-WIN": InstanceTypeSpec(
        vcpus=16, ram_gb=64, category="compute",
        price_hour_eur=0.8800, block_storage=True,
        local_storage_gb=400, max_volumes=16, max_volume_size_gb=10000,
        bandwidth_gbps=3.2, windows=True,
    ),
    "POP2-32C-128G-WIN": InstanceTypeSpec(
        vcpus=32, ram_gb=128, category="compute",
        price_hour_eur=1.7600, block_storage=True,
        local_storage_gb=800, max_volumes=16, max_volume_size_gb=10000,
        bandwidth_gbps=6.4, windows=True,
    ),

    # ── POP2-HM Windows ─────────────────────────────────────────
    "POP2-HM-4C-32G-WIN": InstanceTypeSpec(
        vcpus=4, ram_gb=32, category="memory",
        price_hour_eur=0.2860, block_storage=True,
        local_storage_gb=100, max_volumes=16, max_volume_size_gb=10000,
        bandwidth_gbps=0.8, windows=True,
    ),
    "POP2-HM-8C-64G-WIN": InstanceTypeSpec(
        vcpus=8, ram_gb=64, category="memory",
        price_hour_eur=0.5720, block_storage=True,
        local_storage_gb=200, max_volumes=16, max_volume_size_gb=10000,
        bandwidth_gbps=1.6, windows=True,
    ),
    "POP2-HM-16C-128G-WIN": InstanceTypeSpec(
        vcpus=16, ram_gb=128, category="memory",
        price_hour_eur=1.1440, block_storage=True,
        local_storage_gb=400, max_volumes=16, max_volume_size_gb=10000,
        bandwidth_gbps=3.2, windows=True,
    ),
}


# ═══════════════════════════════════════════════════════════════════
#  Guest OS Mapping
# ═══════════════════════════════════════════════════════════════════

# Maps VMware guestId fragments to (os_family, description)
GUEST_OS_MAP: dict[str, tuple[str, str]] = {
    # Windows
    "windows9Server64Guest": ("windows", "Windows Server 2016+"),
    "windows2019srv_64Guest": ("windows", "Windows Server 2019"),
    "windows2019srvNext_64Guest": ("windows", "Windows Server 2022"),
    "windows9_64Guest": ("windows", "Windows 10"),
    "windows11_64Guest": ("windows", "Windows 11"),
    # Linux - Debian family
    "debian10_64Guest": ("linux", "Debian 10"),
    "debian11_64Guest": ("linux", "Debian 11"),
    "debian12_64Guest": ("linux", "Debian 12"),
    "ubuntu64Guest": ("linux", "Ubuntu"),
    # Linux - RHEL family
    "rhel7_64Guest": ("linux", "RHEL 7"),
    "rhel8_64Guest": ("linux", "RHEL 8"),
    "rhel9_64Guest": ("linux", "RHEL 9"),
    "centos7_64Guest": ("linux", "CentOS 7"),
    "centos8_64Guest": ("linux", "CentOS 8"),
    "centos9_64Guest": ("linux", "CentOS Stream 9"),
    "rockylinux_64Guest": ("linux", "Rocky Linux"),
    "almalinux_64Guest": ("linux", "AlmaLinux"),
    # Linux - Other
    "sles15_64Guest": ("linux", "SLES 15"),
    "amazonlinux3_64Guest": ("linux", "Amazon Linux"),
    "other3xLinux64Guest": ("linux", "Linux (generic 3.x)"),
    "other4xLinux64Guest": ("linux", "Linux (generic 4.x)"),
    "other5xLinux64Guest": ("linux", "Linux (generic 5.x)"),
    "otherLinux64Guest": ("linux", "Linux (generic)"),
    "otherGuest64": ("linux", "Other 64-bit"),
}


class ResourceMapper:
    """Maps VMware VM resources to Scaleway instance types.

    Supports multiple sizing strategies:
      - exact: Closest match to source CPU/RAM
      - optimize: Right-size with moderate headroom
      - cost: Smallest viable type
    """

    def __init__(self, catalog: dict[str, InstanceTypeSpec] | None = None):
        self.catalog = catalog or INSTANCE_TYPES

    def get_os_family(self, guest_os_id: str) -> tuple[str, str]:
        """Detect OS family from VMware guestId.

        Returns:
            (os_family, description) tuple — e.g. ("linux", "Ubuntu 22.04")
        """
        # Direct match
        if guest_os_id in GUEST_OS_MAP:
            return GUEST_OS_MAP[guest_os_id]

        # Fuzzy match by prefix
        guest_lower = guest_os_id.lower()
        if "win" in guest_lower:
            return ("windows", f"Windows ({guest_os_id})")
        if any(k in guest_lower for k in ("linux", "ubuntu", "debian", "centos", "rhel", "suse", "rocky", "alma")):
            return ("linux", f"Linux ({guest_os_id})")

        return ("linux", f"Unknown ({guest_os_id})")

    def suggest_instance_type(
        self,
        cpu: int,
        ram_mb: int,
        disk_gb: float,
        num_disks: int = 1,
        is_windows: bool = False,
        strategy: str = "optimize",
    ) -> str | None:
        """Suggest the best Scaleway instance type for given VM specs.

        Args:
            cpu: Number of vCPUs
            ram_mb: Memory in MB
            disk_gb: Total disk in GB
            num_disks: Number of disks
            is_windows: Windows OS
            strategy: "exact", "optimize", or "cost"

        Returns:
            Instance type name or None if no match
        """
        ram_gb = ram_mb / 1024
        candidates = []

        for name, spec in self.catalog.items():
            # Windows → only Windows types
            if is_windows and not spec.windows:
                continue
            # Linux → skip Windows types
            if not is_windows and spec.windows:
                continue
            # Skip dev types for production sizing
            if spec.category == "development" and strategy != "cost":
                continue
            # Must fit resources
            if spec.vcpus < cpu:
                continue
            if spec.ram_gb < ram_gb:
                continue
            # Volume count check (+1 for boot)
            if num_disks > spec.max_volumes:
                continue
            # Disk size check (SBS)
            if not spec.block_storage and spec.local_storage_gb < disk_gb:
                continue

            # Score by strategy
            cpu_waste = (spec.vcpus - cpu) / max(spec.vcpus, 1)
            ram_waste = (spec.ram_gb - ram_gb) / max(spec.ram_gb, 1)

            if strategy == "cost":
                score = spec.price_hour_eur
            elif strategy == "exact":
                score = cpu_waste + ram_waste
            else:  # optimize
                score = cpu_waste * 0.6 + ram_waste * 0.4
                if not spec.shared_vcpu:
                    score -= 0.05  # Prefer dedicated vCPUs
                if spec.category == "memory" and ram_gb / max(cpu, 1) > 6:
                    score -= 0.03  # Prefer HM for memory-heavy VMs

            candidates.append((score, name))

        if not candidates:
            return None

        candidates.sort()
        return candidates[0][1]

    def validate_mapping(
        self,
        target_type: str,
        cpu: int,
        ram_mb: int,
        disk_gb: float,
        num_disks: int = 1,
        is_windows: bool = False,
    ) -> list[str]:
        """Validate that a target instance type can run the VM.

        Returns:
            List of warning/error strings. Empty = valid.
        """
        issues = []

        if target_type not in self.catalog:
            issues.append(f"Unknown instance type: {target_type}")
            return issues

        spec = self.catalog[target_type]
        ram_gb = ram_mb / 1024

        if spec.vcpus < cpu:
            issues.append(f"Insufficient vCPUs: {spec.vcpus} < {cpu}")
        if spec.ram_gb < ram_gb:
            issues.append(f"Insufficient RAM: {spec.ram_gb}GB < {ram_gb:.1f}GB")
        if num_disks > spec.max_volumes:
            issues.append(f"Too many disks: {num_disks} > {spec.max_volumes} max volumes")
        if is_windows and not spec.windows:
            issues.append(f"{target_type} is not a Windows-compatible type")
        if not is_windows and spec.windows:
            issues.append(f"{target_type} is Windows-only; use non-WIN variant")

        return issues

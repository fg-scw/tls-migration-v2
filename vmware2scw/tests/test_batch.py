"""Tests for vmware2scw batch migration features.

Covers:
  - Inventory filter matching
  - Batch plan YAML load/save
  - VM resolution and exclusion
  - Wave grouping
  - Auto-mapping
  - Cost estimation
  - Batch state persistence and resume
"""

import json
import tempfile
from pathlib import Path

import pytest
import yaml


# ═══════════════════════════════════════════════════════════════════
#  Test Data Fixtures
# ═══════════════════════════════════════════════════════════════════

SAMPLE_VMS = [
    {
        "name": "web-prod-01",
        "cpu": 4,
        "memory_mb": 16384,
        "guest_os": "ubuntu64Guest",
        "firmware": "efi",
        "total_disk_gb": 120,
        "disks": [{"name": "disk0", "size_gb": 80}, {"name": "disk1", "size_gb": 40}],
        "host": "esxi-01.local",
        "cluster": "prod-cluster",
        "datacenter": "DC1",
        "folder": "/DC1/vm/Production/Web",
        "power_state": "poweredOn",
        "tags": ["production", "web"],
    },
    {
        "name": "web-prod-02",
        "cpu": 4,
        "memory_mb": 16384,
        "guest_os": "ubuntu64Guest",
        "firmware": "bios",
        "total_disk_gb": 120,
        "disks": [{"name": "disk0", "size_gb": 120}],
        "host": "esxi-01.local",
        "cluster": "prod-cluster",
        "datacenter": "DC1",
        "folder": "/DC1/vm/Production/Web",
        "power_state": "poweredOn",
        "tags": ["production", "web"],
    },
    {
        "name": "db-prod-01",
        "cpu": 8,
        "memory_mb": 65536,
        "guest_os": "debian12_64Guest",
        "firmware": "efi",
        "total_disk_gb": 500,
        "disks": [{"name": "disk0", "size_gb": 50}, {"name": "data", "size_gb": 450}],
        "host": "esxi-02.local",
        "cluster": "prod-cluster",
        "datacenter": "DC1",
        "folder": "/DC1/vm/Production/DB",
        "power_state": "poweredOn",
        "tags": ["production", "database"],
    },
    {
        "name": "win-ad-01",
        "cpu": 4,
        "memory_mb": 16384,
        "guest_os": "windows2019srv_64Guest",
        "firmware": "bios",
        "total_disk_gb": 80,
        "disks": [{"name": "disk0", "size_gb": 80}],
        "host": "esxi-03.local",
        "cluster": "prod-cluster",
        "datacenter": "DC1",
        "folder": "/DC1/vm/Production/Windows",
        "power_state": "poweredOn",
        "tags": ["production", "windows", "ad"],
    },
    {
        "name": "dev-app-01",
        "cpu": 2,
        "memory_mb": 4096,
        "guest_os": "ubuntu64Guest",
        "firmware": "bios",
        "total_disk_gb": 40,
        "disks": [{"name": "disk0", "size_gb": 40}],
        "host": "esxi-01.local",
        "cluster": "dev-cluster",
        "datacenter": "DC1",
        "folder": "/DC1/vm/Development",
        "power_state": "poweredOff",
        "tags": ["development"],
    },
    {
        "name": "template-ubuntu-22",
        "cpu": 2,
        "memory_mb": 2048,
        "guest_os": "ubuntu64Guest",
        "firmware": "bios",
        "total_disk_gb": 20,
        "disks": [{"name": "disk0", "size_gb": 20}],
        "host": "esxi-01.local",
        "cluster": "dev-cluster",
        "datacenter": "DC1",
        "folder": "/DC1/vm/Templates",
        "power_state": "poweredOff",
        "tags": ["template"],
    },
]


# ═══════════════════════════════════════════════════════════════════
#  Inventory Filter Tests
# ═══════════════════════════════════════════════════════════════════

class TestInventoryFilter:
    def test_name_pattern(self):
        from vmware2scw.pipeline.inventory import InventoryFilter
        f = InventoryFilter.from_cli_filters(["name:web-*"])
        assert f.matches(SAMPLE_VMS[0])  # web-prod-01
        assert f.matches(SAMPLE_VMS[1])  # web-prod-02
        assert not f.matches(SAMPLE_VMS[2])  # db-prod-01

    def test_os_filter_linux(self):
        from vmware2scw.pipeline.inventory import InventoryFilter
        f = InventoryFilter.from_cli_filters(["os:linux"])
        assert f.matches(SAMPLE_VMS[0])  # ubuntu
        assert f.matches(SAMPLE_VMS[2])  # debian
        assert not f.matches(SAMPLE_VMS[3])  # windows

    def test_os_filter_windows(self):
        from vmware2scw.pipeline.inventory import InventoryFilter
        f = InventoryFilter.from_cli_filters(["os:windows"])
        assert not f.matches(SAMPLE_VMS[0])
        assert f.matches(SAMPLE_VMS[3])

    def test_folder_filter(self):
        from vmware2scw.pipeline.inventory import InventoryFilter
        f = InventoryFilter.from_cli_filters(["folder:/DC1/vm/Production"])
        assert f.matches(SAMPLE_VMS[0])  # /Production/Web
        assert f.matches(SAMPLE_VMS[3])  # /Production/Windows
        assert not f.matches(SAMPLE_VMS[4])  # /Development

    def test_host_filter(self):
        from vmware2scw.pipeline.inventory import InventoryFilter
        f = InventoryFilter.from_cli_filters(["host:esxi-01*"])
        assert f.matches(SAMPLE_VMS[0])  # esxi-01
        assert not f.matches(SAMPLE_VMS[2])  # esxi-02

    def test_firmware_filter(self):
        from vmware2scw.pipeline.inventory import InventoryFilter
        f = InventoryFilter.from_cli_filters(["firmware:bios"])
        assert not f.matches(SAMPLE_VMS[0])  # efi
        assert f.matches(SAMPLE_VMS[1])  # bios

    def test_cpu_range(self):
        from vmware2scw.pipeline.inventory import InventoryFilter
        f = InventoryFilter.from_cli_filters([], min_cpu=4, max_cpu=8)
        assert f.matches(SAMPLE_VMS[0])  # 4 cpu
        assert f.matches(SAMPLE_VMS[2])  # 8 cpu
        assert not f.matches(SAMPLE_VMS[4])  # 2 cpu

    def test_disk_range(self):
        from vmware2scw.pipeline.inventory import InventoryFilter
        f = InventoryFilter.from_cli_filters([], max_disk_gb=100)
        assert not f.matches(SAMPLE_VMS[0])  # 120 GB
        assert f.matches(SAMPLE_VMS[3])  # 80 GB
        assert f.matches(SAMPLE_VMS[4])  # 40 GB

    def test_combined_filters(self):
        """Multiple filters are AND'd together."""
        from vmware2scw.pipeline.inventory import InventoryFilter
        f = InventoryFilter.from_cli_filters(["os:linux", "name:*prod*"], min_cpu=4)
        assert f.matches(SAMPLE_VMS[0])  # linux, web-prod-01, 4 cpu
        assert f.matches(SAMPLE_VMS[2])  # linux, db-prod-01, 8 cpu
        assert not f.matches(SAMPLE_VMS[3])  # windows
        assert not f.matches(SAMPLE_VMS[4])  # dev, 2 cpu

    def test_power_state_filter(self):
        from vmware2scw.pipeline.inventory import InventoryFilter
        f = InventoryFilter.from_cli_filters(["state:poweredOn"])
        assert f.matches(SAMPLE_VMS[0])
        assert not f.matches(SAMPLE_VMS[4])  # poweredOff

    def test_bare_string_as_name_pattern(self):
        from vmware2scw.pipeline.inventory import InventoryFilter
        f = InventoryFilter.from_cli_filters(["web-*"])
        assert f.matches(SAMPLE_VMS[0])
        assert not f.matches(SAMPLE_VMS[2])


# ═══════════════════════════════════════════════════════════════════
#  Batch Plan Tests
# ═══════════════════════════════════════════════════════════════════

class TestBatchPlan:
    def _sample_plan_dict(self):
        return {
            "version": 1,
            "metadata": {"vcenter": "test", "total_vms": 3},
            "defaults": {"zone": "fr-par-1", "sizing_strategy": "optimize"},
            "migrations": [
                {"vm_name": "web-prod-01", "target_type": "POP2-4C-16G", "priority": 1},
                {"vm_pattern": "dev-*", "target_type": "PLAY2-MICRO", "priority": 5},
                {"vm_name": "db-prod-01", "target_type": "POP2-HM-8C-64G", "priority": 2},
            ],
            "exclude": [
                {"vm_pattern": "template-*"},
            ],
        }

    def test_load_from_yaml(self):
        from vmware2scw.pipeline.batch_plan import BatchPlan
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(self._sample_plan_dict(), f)
            f.flush()
            plan = BatchPlan.from_yaml(f.name)

        assert len(plan.migrations) == 3
        assert plan.migrations[0].vm_name == "web-prod-01"
        assert plan.migrations[1].vm_pattern == "dev-*"
        assert plan.defaults.zone == "fr-par-1"

    def test_save_to_yaml(self):
        from vmware2scw.pipeline.batch_plan import BatchPlan
        plan = BatchPlan(**self._sample_plan_dict())

        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as f:
            plan.to_yaml(f.name)
            with open(f.name) as rf:
                loaded = yaml.safe_load(rf)

        assert loaded["version"] == 1
        assert len(loaded["migrations"]) == 3

    def test_resolve_vms(self):
        from vmware2scw.pipeline.batch_plan import BatchPlan
        plan = BatchPlan(**self._sample_plan_dict())
        resolved = plan.resolve_vms(SAMPLE_VMS)

        names = [r.vm_name for r in resolved]
        assert "web-prod-01" in names
        assert "db-prod-01" in names
        assert "dev-app-01" in names
        assert "template-ubuntu-22" not in names  # Excluded

    def test_exclusion_by_pattern(self):
        from vmware2scw.pipeline.batch_plan import BatchPlan
        plan = BatchPlan(**self._sample_plan_dict())
        resolved = plan.resolve_vms(SAMPLE_VMS)

        names = [r.vm_name for r in resolved]
        assert "template-ubuntu-22" not in names

    def test_priority_sorting(self):
        from vmware2scw.pipeline.batch_plan import BatchPlan
        plan = BatchPlan(**self._sample_plan_dict())
        resolved = plan.resolve_vms(SAMPLE_VMS)

        # Priority 1 should come before priority 5
        web_idx = next(i for i, r in enumerate(resolved) if r.vm_name == "web-prod-01")
        dev_idx = next(i for i, r in enumerate(resolved) if r.vm_name == "dev-app-01")
        assert web_idx < dev_idx

    def test_wave_grouping(self):
        from vmware2scw.pipeline.batch_plan import BatchPlan
        plan_data = self._sample_plan_dict()
        plan_data["migrations"][0]["wave"] = "canary"
        plan_data["migrations"][2]["wave"] = "production"
        plan_data["waves"] = [
            {"name": "canary", "vms": ["web-prod-01"]},
            {"name": "production", "vms": ["db-prod-01"]},
        ]
        plan = BatchPlan(**plan_data)
        resolved = plan.resolve_vms(SAMPLE_VMS)
        waves = plan.get_waves(resolved)

        assert len(waves) >= 2
        wave1_names = [r.vm_name for r in waves[0]]
        assert "web-prod-01" in wave1_names

    def test_priority_based_waves(self):
        """Without explicit waves, group by priority."""
        from vmware2scw.pipeline.batch_plan import BatchPlan
        plan = BatchPlan(**self._sample_plan_dict())
        resolved = plan.resolve_vms(SAMPLE_VMS)
        waves = plan.get_waves(resolved)

        # Should have groups for priority 1, 2, and 5
        assert len(waves) >= 2


# ═══════════════════════════════════════════════════════════════════
#  Resource Mapping Tests
# ═══════════════════════════════════════════════════════════════════

class TestResourceMapper:
    def test_linux_mapping(self):
        from vmware2scw.scaleway.mapping import ResourceMapper
        mapper = ResourceMapper()
        result = mapper.suggest_instance_type(
            cpu=4, ram_mb=16384, disk_gb=120, num_disks=2,
            is_windows=False, strategy="optimize",
        )
        assert result is not None
        assert "WIN" not in result

    def test_windows_mapping(self):
        from vmware2scw.scaleway.mapping import ResourceMapper
        mapper = ResourceMapper()
        result = mapper.suggest_instance_type(
            cpu=4, ram_mb=16384, disk_gb=80, num_disks=1,
            is_windows=True, strategy="optimize",
        )
        assert result is not None
        assert "WIN" in result

    def test_cost_strategy(self):
        from vmware2scw.scaleway.mapping import ResourceMapper
        mapper = ResourceMapper()
        cost_type = mapper.suggest_instance_type(
            cpu=2, ram_mb=4096, disk_gb=40,
            is_windows=False, strategy="cost",
        )
        exact_type = mapper.suggest_instance_type(
            cpu=2, ram_mb=4096, disk_gb=40,
            is_windows=False, strategy="exact",
        )
        # Cost strategy should pick a cheaper (possibly shared) type
        assert cost_type is not None
        assert exact_type is not None

    def test_os_family_detection(self):
        from vmware2scw.scaleway.mapping import ResourceMapper
        mapper = ResourceMapper()
        family, desc = mapper.get_os_family("ubuntu64Guest")
        assert family == "linux"
        family, desc = mapper.get_os_family("windows2019srv_64Guest")
        assert family == "windows"
        family, desc = mapper.get_os_family("unknownGuest")
        assert family == "linux"  # Default to linux

    def test_validate_mapping_ok(self):
        from vmware2scw.scaleway.mapping import ResourceMapper
        mapper = ResourceMapper()
        issues = mapper.validate_mapping(
            "POP2-4C-16G", cpu=4, ram_mb=16384, disk_gb=120,
            num_disks=2, is_windows=False,
        )
        assert len(issues) == 0

    def test_validate_mapping_insufficient_cpu(self):
        from vmware2scw.scaleway.mapping import ResourceMapper
        mapper = ResourceMapper()
        issues = mapper.validate_mapping(
            "POP2-2C-8G", cpu=4, ram_mb=16384, disk_gb=120,
        )
        assert any("vCPU" in i for i in issues)

    def test_validate_mapping_windows_on_linux_type(self):
        from vmware2scw.scaleway.mapping import ResourceMapper
        mapper = ResourceMapper()
        issues = mapper.validate_mapping(
            "POP2-4C-16G", cpu=4, ram_mb=16384, disk_gb=80,
            is_windows=True,
        )
        assert any("Windows" in i for i in issues)

    def test_memory_heavy_prefers_hm(self):
        """VMs with high RAM/CPU ratio should prefer HM types."""
        from vmware2scw.scaleway.mapping import ResourceMapper
        mapper = ResourceMapper()
        result = mapper.suggest_instance_type(
            cpu=4, ram_mb=65536, disk_gb=100,  # 4 CPU, 64GB RAM
            is_windows=False, strategy="optimize",
        )
        assert result is not None
        assert "HM" in result


# ═══════════════════════════════════════════════════════════════════
#  Estimation Tests
# ═══════════════════════════════════════════════════════════════════

class TestEstimation:
    def test_basic_estimate(self):
        from vmware2scw.pipeline.inventory import estimate_migration
        plan_data = {
            "metadata": {
                "total_disk_gb": 500,
                "linux_vms": 3,
                "windows_vms": 1,
            },
            "migrations": [
                {"vm_name": "vm1", "target_type": "POP2-4C-16G"},
                {"vm_name": "vm2", "target_type": "POP2-4C-16G"},
                {"vm_name": "vm3", "target_type": "POP2-2C-8G"},
                {"vm_name": "vm4", "target_type": "POP2-4C-16G-WIN"},
            ],
        }
        estimate = estimate_migration(plan_data, available_disk_gb=2000, concurrency=5)
        assert estimate["total_vms"] == 4
        assert estimate["required_work_space_gb"] > 0
        assert estimate["estimated_duration_minutes"] > 0
        assert estimate["estimated_monthly_cost_eur"] > 0
        assert len(estimate["warnings"]) == 1  # Windows KVM warning

    def test_disk_space_warning(self):
        from vmware2scw.pipeline.inventory import estimate_migration
        plan_data = {
            "metadata": {"total_disk_gb": 5000, "linux_vms": 10, "windows_vms": 0},
            "migrations": [{"vm_name": f"vm{i}"} for i in range(10)],
        }
        estimate = estimate_migration(plan_data, available_disk_gb=100)
        assert any("Insufficient disk" in w for w in estimate["warnings"])


# ═══════════════════════════════════════════════════════════════════
#  Batch State Persistence Tests
# ═══════════════════════════════════════════════════════════════════

class TestBatchState:
    def test_save_and_load(self):
        from vmware2scw.pipeline.batch_orchestrator import BatchState, VMJob, VMStatus, BatchStatus
        import time

        state = BatchState(
            batch_id="test-123",
            status=BatchStatus.PARTIAL,
            started_at=time.time() - 600,
            completed_at=time.time(),
            current_wave=2,
            total_waves=3,
        )
        state.jobs.append(VMJob(
            vm_name="web-01",
            target_type="POP2-4C-16G",
            status=VMStatus.COMPLETE,
            os_family="linux",
            started_at=time.time() - 300,
            completed_at=time.time() - 60,
            completed_stages=["validate", "export", "convert"],
            stage_timings={"validate": 5, "export": 120, "convert": 60},
        ))
        state.jobs.append(VMJob(
            vm_name="web-02",
            target_type="POP2-4C-16G",
            status=VMStatus.FAILED,
            error="Connection timeout",
            error_stage="export",
        ))

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "batch-test-123.json"
            state.save(path)
            assert path.exists()

            loaded = BatchState.load(path)
            assert loaded.batch_id == "test-123"
            assert loaded.status == BatchStatus.PARTIAL
            assert len(loaded.jobs) == 2
            assert loaded.jobs[0].vm_name == "web-01"
            assert loaded.jobs[0].status == VMStatus.COMPLETE
            assert loaded.jobs[1].status == VMStatus.FAILED
            assert loaded.jobs[1].error == "Connection timeout"

    def test_succeeded_failed_properties(self):
        from vmware2scw.pipeline.batch_orchestrator import BatchState, VMJob, VMStatus
        state = BatchState(batch_id="test")
        state.jobs = [
            VMJob(vm_name="ok1", status=VMStatus.COMPLETE),
            VMJob(vm_name="ok2", status=VMStatus.COMPLETE),
            VMJob(vm_name="fail1", status=VMStatus.FAILED),
            VMJob(vm_name="pending1", status=VMStatus.PENDING),
        ]
        assert len(state.succeeded) == 2
        assert len(state.failed) == 1


# ═══════════════════════════════════════════════════════════════════
#  Report Generation Tests
# ═══════════════════════════════════════════════════════════════════

class TestReportGeneration:
    def test_generate_report_markdown(self):
        from vmware2scw.pipeline.batch_orchestrator import (
            BatchState, VMJob, VMStatus, BatchStatus, generate_report,
        )
        import time

        state = BatchState(
            batch_id="rpt-001",
            status=BatchStatus.PARTIAL,
            started_at=time.time() - 300,
            completed_at=time.time(),
        )
        state.jobs = [
            VMJob(
                vm_name="web-01", target_type="POP2-4C-16G",
                status=VMStatus.COMPLETE, os_family="linux",
                started_at=time.time() - 200, completed_at=time.time() - 100,
                stage_timings={"validate": 5, "export": 60, "convert": 30},
                artifacts={"scaleway_image_id": "img-abc123"},
            ),
            VMJob(
                vm_name="web-02", target_type="POP2-4C-16G",
                status=VMStatus.FAILED, error="Disk full",
                error_stage="convert",
            ),
        ]

        report = generate_report(state)
        assert "rpt-001" in report
        assert "web-01" in report
        assert "web-02" in report
        assert "img-abc123" in report
        assert "Disk full" in report
        assert "Stage Timing" in report

    def test_report_save_to_file(self):
        from vmware2scw.pipeline.batch_orchestrator import (
            BatchState, BatchStatus, generate_report,
        )
        import time

        state = BatchState(
            batch_id="rpt-002",
            status=BatchStatus.COMPLETE,
            started_at=time.time() - 60,
            completed_at=time.time(),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "report.md"
            report = generate_report(state, path)
            assert path.exists()
            assert path.read_text() == report


# ═══════════════════════════════════════════════════════════════════
#  Example Plan YAML Validation
# ═══════════════════════════════════════════════════════════════════

class TestExamplePlan:
    def test_example_plan_is_valid(self):
        """Ensure the example batch plan YAML loads without errors."""
        from vmware2scw.pipeline.batch_plan import BatchPlan
        # Load from the configs directory
        example_path = Path(__file__).parent.parent / "configs" / "example_batch_plan.yaml"
        if example_path.exists():
            plan = BatchPlan.from_yaml(example_path)
            assert len(plan.migrations) == 8
            assert len(plan.waves) == 4
            assert len(plan.exclude) == 3
            assert plan.concurrency.max_total_workers == 10


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

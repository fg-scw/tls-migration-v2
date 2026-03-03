"""Async batch orchestrator for parallel VM migrations.

Architecture:
  - Runs on a single control instance (POP2-4C-16G or POP2-8C-32G)
  - Volume SBS 10TB at /var/lib/vmware2scw/work
  - All stages execute locally (export, convert, adapt, upload, import)
  - Concurrency bounded by per-resource semaphores

Pipeline per VM (DAG — not flat sequence):
  validate ──► snapshot ──► export_disk_1 ──► convert_disk_1 ──► upload_disk_1 ──┐
                         └► export_disk_2 ──► convert_disk_2 ──► upload_disk_2 ──┤
                                                                                  ├──► adapt_guest ──► ensure_uefi ──► import_scw ──► verify ──► cleanup
                                                                                  │
  Note: adapt_guest runs only on boot disk, after convert, before upload.
  In practice we serialize per-VM for simplicity, but parallelize across VMs.

Semaphores:
  - per_esxi_host: 4 (NFC lease limit per ESXi host)
  - disk_io: 3 (conversions, limited by volume IOPS)
  - s3_upload: 6 (S3 bandwidth ~10Gbps shared)
  - scw_api: 5 (rate limit ~50 req/min)
  - global: 10 (total concurrent VMs)

Wave execution:
  - VMs are grouped into waves (explicit or by priority)
  - Each wave runs in parallel (bounded by semaphores)
  - Between waves: pause, auto-continue, or pause-on-failure

State persistence:
  - Batch state saved to batch-{id}.json after each VM completion
  - Resume with `vmware2scw batch resume --batch-id <id>`

v4 FIX: Replaced `for stage_name in stages:` with `while` loop so that
rebuilding the stage list after validate (Linux -> Windows) actually takes
effect. The Python `for` iterates over a snapshot of the original list
and reassigning the variable does NOT change the iteration.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
#  Data Models
# ═══════════════════════════════════════════════════════════════════

class VMStatus(str, Enum):
    PENDING = "pending"
    VALIDATING = "validating"
    EXPORTING = "exporting"
    CONVERTING = "converting"
    ADAPTING = "adapting"
    UPLOADING = "uploading"
    IMPORTING = "importing"
    VERIFYING = "verifying"
    CLEANING = "cleaning"
    COMPLETE = "complete"
    FAILED = "failed"
    SKIPPED = "skipped"


class BatchStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"         # Waiting for operator confirmation between waves
    COMPLETE = "complete"
    FAILED = "failed"         # Fatal error, batch aborted
    PARTIAL = "partial"       # Some VMs failed, others succeeded


@dataclass
class VMJob:
    """Tracks a single VM migration within the batch."""
    vm_name: str
    target_type: str = ""
    zone: str = "fr-par-1"
    os_family: str = ""        # Set after validate
    esxi_host: str = ""        # Set after validate
    firmware: str = ""         # Set after validate
    total_disk_gb: float = 0   # Set after validate
    wave: str = ""
    priority: int = 5
    skip_validation: bool = False
    tags: list[str] = field(default_factory=list)
    network_mapping: dict[str, str] = field(default_factory=dict)

    # Runtime state
    status: VMStatus = VMStatus.PENDING
    migration_id: str = ""
    error: Optional[str] = None
    error_stage: Optional[str] = None
    started_at: float = 0
    completed_at: float = 0
    retry_count: int = 0

    # Artifacts from pipeline stages
    artifacts: dict[str, Any] = field(default_factory=dict)

    # Progress tracking
    current_stage: str = ""
    completed_stages: list[str] = field(default_factory=list)
    stage_timings: dict[str, float] = field(default_factory=dict)

    @property
    def duration_s(self) -> float:
        if self.completed_at and self.started_at:
            return self.completed_at - self.started_at
        if self.started_at:
            return time.time() - self.started_at
        return 0

    @property
    def duration_str(self) -> str:
        d = self.duration_s
        if d < 60:
            return f"{d:.0f}s"
        return f"{d / 60:.1f}m"

    def to_dict(self) -> dict:
        return {
            "vm_name": self.vm_name,
            "target_type": self.target_type,
            "zone": self.zone,
            "os_family": self.os_family,
            "esxi_host": self.esxi_host,
            "firmware": self.firmware,
            "status": self.status.value,
            "migration_id": self.migration_id,
            "error": self.error,
            "error_stage": self.error_stage,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "completed_stages": self.completed_stages,
            "stage_timings": self.stage_timings,
            "artifacts": {k: v for k, v in self.artifacts.items()
                          if k in ("scaleway_image_id", "scaleway_instance_id", "s3_keys")},
        }


@dataclass
class BatchState:
    """Persistent state for the entire batch migration."""
    batch_id: str
    status: BatchStatus = BatchStatus.PENDING
    plan_path: str = ""
    config_path: str = ""
    started_at: float = 0
    completed_at: float = 0
    current_wave: int = 0
    total_waves: int = 0
    jobs: list[VMJob] = field(default_factory=list)

    @property
    def succeeded(self) -> list[VMJob]:
        return [j for j in self.jobs if j.status == VMStatus.COMPLETE]

    @property
    def failed(self) -> list[VMJob]:
        return [j for j in self.jobs if j.status == VMStatus.FAILED]

    @property
    def in_progress(self) -> list[VMJob]:
        return [j for j in self.jobs
                if j.status not in (VMStatus.PENDING, VMStatus.COMPLETE,
                                     VMStatus.FAILED, VMStatus.SKIPPED)]

    @property
    def duration_s(self) -> float:
        if self.completed_at and self.started_at:
            return self.completed_at - self.started_at
        if self.started_at:
            return time.time() - self.started_at
        return 0

    def save(self, path: Path) -> None:
        data = {
            "batch_id": self.batch_id,
            "status": self.status.value,
            "plan_path": self.plan_path,
            "config_path": self.config_path,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "current_wave": self.current_wave,
            "total_waves": self.total_waves,
            "jobs": [j.to_dict() for j in self.jobs],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)

    @classmethod
    def load(cls, path: Path) -> "BatchState":
        with open(path) as f:
            data = json.load(f)
        state = cls(
            batch_id=data["batch_id"],
            status=BatchStatus(data["status"]),
            plan_path=data.get("plan_path", ""),
            config_path=data.get("config_path", ""),
            started_at=data.get("started_at", 0),
            completed_at=data.get("completed_at", 0),
            current_wave=data.get("current_wave", 0),
            total_waves=data.get("total_waves", 0),
        )
        for jd in data.get("jobs", []):
            job = VMJob(
                vm_name=jd["vm_name"],
                target_type=jd.get("target_type", ""),
                zone=jd.get("zone", "fr-par-1"),
                os_family=jd.get("os_family", ""),
                esxi_host=jd.get("esxi_host", ""),
                firmware=jd.get("firmware", ""),
                status=VMStatus(jd.get("status", "pending")),
                migration_id=jd.get("migration_id", ""),
                error=jd.get("error"),
                error_stage=jd.get("error_stage"),
                started_at=jd.get("started_at", 0),
                completed_at=jd.get("completed_at", 0),
                completed_stages=jd.get("completed_stages", []),
                stage_timings=jd.get("stage_timings", {}),
            )
            state.jobs.append(job)
        return state


# ═══════════════════════════════════════════════════════════════════
#  Semaphore Manager
# ═══════════════════════════════════════════════════════════════════

class SemaphoreManager:
    """Manages per-resource semaphores for bounded concurrency.

    Resources:
      - global: Total concurrent VM pipelines
      - host:{hostname}: NFC exports per ESXi host
      - disk_io: Concurrent disk conversions
      - s3_upload: Concurrent S3 uploads
      - scw_api: Concurrent Scaleway API calls
    """

    def __init__(
        self,
        max_global: int = 10,
        max_per_host: int = 4,
        max_disk_io: int = 3,
        max_s3_upload: int = 6,
        max_scw_api: int = 5,
    ):
        self._global = asyncio.Semaphore(max_global)
        self._disk_io = asyncio.Semaphore(max_disk_io)
        self._s3_upload = asyncio.Semaphore(max_s3_upload)
        self._scw_api = asyncio.Semaphore(max_scw_api)
        self._max_per_host = max_per_host
        self._host_semaphores: dict[str, asyncio.Semaphore] = {}

    def get_host_semaphore(self, host: str) -> asyncio.Semaphore:
        if host not in self._host_semaphores:
            self._host_semaphores[host] = asyncio.Semaphore(self._max_per_host)
        return self._host_semaphores[host]

    @property
    def global_sem(self) -> asyncio.Semaphore:
        return self._global

    @property
    def disk_io(self) -> asyncio.Semaphore:
        return self._disk_io

    @property
    def s3_upload(self) -> asyncio.Semaphore:
        return self._s3_upload

    @property
    def scw_api(self) -> asyncio.Semaphore:
        return self._scw_api


# ═══════════════════════════════════════════════════════════════════
#  Progress Callback Protocol
# ═══════════════════════════════════════════════════════════════════

class BatchProgressCallback:
    """Interface for progress reporting (implemented by dashboard)."""

    def on_batch_start(self, state: BatchState) -> None:
        pass

    def on_wave_start(self, wave_index: int, wave_name: str, vm_count: int) -> None:
        pass

    def on_wave_complete(self, wave_index: int, succeeded: int, failed: int) -> None:
        pass

    def on_vm_stage_start(self, job: VMJob, stage: str) -> None:
        pass

    def on_vm_stage_complete(self, job: VMJob, stage: str, duration_s: float) -> None:
        pass

    def on_vm_complete(self, job: VMJob) -> None:
        pass

    def on_vm_failed(self, job: VMJob, error: str) -> None:
        pass

    def on_batch_complete(self, state: BatchState) -> None:
        pass

    def on_wave_pause(self, wave_index: int, reason: str) -> None:
        """Called when batch pauses between waves. Must resolve to continue."""
        pass


# ═══════════════════════════════════════════════════════════════════
#  Batch Orchestrator
# ═══════════════════════════════════════════════════════════════════

class BatchOrchestrator:
    """Orchestrates parallel migration of 1-100 VMs.

    Executes the migration pipeline for each VM in parallel, bounded by
    per-resource semaphores. Supports wave-based execution with pauses.

    Usage:
        config = AppConfig.from_yaml("migration.yaml")
        plan = BatchPlan.from_yaml("plan.yaml")
        orchestrator = BatchOrchestrator(config, plan)

        # Optional: attach a dashboard for live progress
        orchestrator.set_progress_callback(RichDashboard())

        # Run
        state = await orchestrator.run()

        # Or resume
        state = await orchestrator.resume("batch-abc123")
    """

    def __init__(self, config, plan=None):
        """
        Args:
            config: AppConfig instance
            plan: BatchPlan instance (optional for resume)
        """
        self.config = config
        self.plan = plan
        self.semaphores: Optional[SemaphoreManager] = None
        self.state: Optional[BatchState] = None
        self._progress: BatchProgressCallback = BatchProgressCallback()
        self._pause_event: Optional[asyncio.Event] = None
        self._cancel_event: Optional[asyncio.Event] = None

    def set_progress_callback(self, callback: BatchProgressCallback) -> None:
        self._progress = callback

    @property
    def state_dir(self) -> Path:
        return self.config.conversion.work_dir / "batch-state"

    def _state_path(self, batch_id: str) -> Path:
        return self.state_dir / f"batch-{batch_id}.json"

    async def run(self, resolved_vms: list | None = None, waves: list | None = None) -> BatchState:
        """Execute a batch migration.

        Args:
            resolved_vms: List of ResolvedVM objects (if not using waves)
            waves: List of wave groups (list of lists of ResolvedVM)

        Returns:
            BatchState with final results
        """
        batch_id = str(uuid.uuid4())[:8]
        self.state = BatchState(
            batch_id=batch_id,
            status=BatchStatus.RUNNING,
            started_at=time.time(),
        )

        # Initialize semaphores from plan concurrency config
        concurrency = None
        if self.plan:
            concurrency = self.plan.concurrency
        self.semaphores = SemaphoreManager(
            max_global=concurrency.max_total_workers if concurrency else 10,
            max_per_host=concurrency.max_exports_per_host if concurrency else 4,
            max_disk_io=concurrency.max_concurrent_conversions if concurrency else 3,
            max_s3_upload=concurrency.max_concurrent_uploads if concurrency else 6,
            max_scw_api=concurrency.max_concurrent_imports if concurrency else 5,
        )

        self._cancel_event = asyncio.Event()

        # Build job list
        if waves:
            all_vms = [vm for wave in waves for vm in wave]
            wave_groups = waves
        elif resolved_vms:
            all_vms = resolved_vms
            wave_groups = [resolved_vms]
        else:
            raise ValueError("Either resolved_vms or waves must be provided")

        for rvm in all_vms:
            job = VMJob(
                vm_name=rvm.vm_name if hasattr(rvm, 'vm_name') else rvm.get("vm_name", ""),
                target_type=getattr(rvm, 'target_type', "") or "",
                zone=getattr(rvm, 'zone', "fr-par-1"),
                migration_id=str(uuid.uuid4())[:8],
                tags=getattr(rvm, 'tags', []),
                skip_validation=getattr(rvm, 'skip_validation', False),
                wave=getattr(rvm, 'wave', ""),
                priority=getattr(rvm, 'priority', 5),
            )
            self.state.jobs.append(job)

        self.state.total_waves = len(wave_groups)
        self._progress.on_batch_start(self.state)

        # Execute wave by wave
        job_index = 0
        for wave_idx, wave_vms in enumerate(wave_groups):
            self.state.current_wave = wave_idx + 1
            wave_name = f"Wave {wave_idx + 1}"
            self._progress.on_wave_start(wave_idx, wave_name, len(wave_vms))

            # Get the corresponding jobs
            wave_jobs = self.state.jobs[job_index:job_index + len(wave_vms)]
            job_index += len(wave_vms)

            # Run all VMs in this wave in parallel (bounded by semaphores)
            tasks = [
                self._run_vm_pipeline(job)
                for job in wave_jobs
            ]
            await asyncio.gather(*tasks, return_exceptions=True)

            # Save state after each wave
            self._save_state()

            # Wave completion
            wave_succeeded = sum(1 for j in wave_jobs if j.status == VMStatus.COMPLETE)
            wave_failed = sum(1 for j in wave_jobs if j.status == VMStatus.FAILED)
            self._progress.on_wave_complete(wave_idx, wave_succeeded, wave_failed)

            # Check if we should pause between waves
            if wave_idx < len(wave_groups) - 1:
                should_pause = False
                if self.plan and self.plan.waves and wave_idx < len(self.plan.waves):
                    wave_cfg = self.plan.waves[wave_idx]
                    if wave_cfg.pause_after.value == "pause":
                        should_pause = True
                    elif wave_cfg.pause_after.value == "pause_on_failure" and wave_failed > 0:
                        should_pause = True

                if should_pause:
                    self.state.status = BatchStatus.PAUSED
                    self._save_state()
                    self._progress.on_wave_pause(
                        wave_idx,
                        f"Wave {wave_idx + 1} complete ({wave_succeeded} OK, {wave_failed} failed). "
                        f"Waiting for confirmation to proceed..."
                    )
                    # Wait for operator to unpause
                    if self._pause_event is None:
                        self._pause_event = asyncio.Event()
                    self._pause_event.clear()
                    await self._pause_event.wait()
                    self.state.status = BatchStatus.RUNNING

        # Finalize
        self.state.completed_at = time.time()
        if self.state.failed:
            self.state.status = BatchStatus.PARTIAL if self.state.succeeded else BatchStatus.FAILED
        else:
            self.state.status = BatchStatus.COMPLETE

        self._save_state()
        self._progress.on_batch_complete(self.state)

        return self.state

    async def resume(self, batch_id: str) -> BatchState:
        """Resume a paused or partially-failed batch migration."""
        state_path = self._state_path(batch_id)
        if not state_path.exists():
            raise FileNotFoundError(f"Batch state not found: {state_path}")

        self.state = BatchState.load(state_path)
        logger.info(f"Resuming batch {batch_id}: "
                     f"{len(self.state.succeeded)} complete, "
                     f"{len(self.state.failed)} failed, "
                     f"{len(self.state.in_progress)} in progress")

        # Reset failed/in-progress jobs to pending for retry
        for job in self.state.jobs:
            if job.status in (VMStatus.FAILED,):
                job.status = VMStatus.PENDING
                job.error = None
                job.error_stage = None
                job.retry_count += 1

        # Re-run pending jobs
        pending = [j for j in self.state.jobs if j.status == VMStatus.PENDING]
        if not pending:
            logger.info("No pending jobs to resume")
            return self.state

        self.state.status = BatchStatus.RUNNING
        self.semaphores = SemaphoreManager()
        self._cancel_event = asyncio.Event()

        tasks = [self._run_vm_pipeline(job) for job in pending]
        await asyncio.gather(*tasks, return_exceptions=True)

        self.state.completed_at = time.time()
        if self.state.failed:
            self.state.status = BatchStatus.PARTIAL
        else:
            self.state.status = BatchStatus.COMPLETE

        self._save_state()
        return self.state

    def unpause(self) -> None:
        """Signal the orchestrator to continue after a wave pause."""
        if self._pause_event:
            self._pause_event.set()

    def cancel(self) -> None:
        """Signal the orchestrator to cancel remaining work."""
        if self._cancel_event:
            self._cancel_event.set()

    # ─── VM Pipeline Execution ────────────────────────────────────

    async def _run_vm_pipeline(self, job: VMJob) -> None:
        """Execute the full migration pipeline for a single VM.

        Acquires the global semaphore, then runs stages sequentially.
        Each stage acquires its own resource-specific semaphore.

        v4 FIX: Uses a while loop instead of for loop so that rebuilding
        the stage list after validate (e.g. Linux -> Windows) actually
        takes effect. Python's `for x in list:` iterates over the original
        list object; reassigning the variable does not change iteration.
        """
        if self._cancel_event and self._cancel_event.is_set():
            job.status = VMStatus.SKIPPED
            return

        async with self.semaphores.global_sem:
            job.started_at = time.time()

            try:
                # Determine pipeline stages based on OS
                # We start with validate to detect OS, then pick the right stages
                stages = await self._build_stage_list(job)
                stage_idx = 0

                while stage_idx < len(stages):
                    stage_name = stages[stage_idx]
                    stage_idx += 1

                    if self._cancel_event and self._cancel_event.is_set():
                        job.status = VMStatus.SKIPPED
                        return

                    if stage_name in job.completed_stages:
                        continue  # Skip already-completed stages (for resume)

                    job.current_stage = stage_name
                    job.status = self._stage_to_status(stage_name)
                    self._progress.on_vm_stage_start(job, stage_name)

                    stage_start = time.time()
                    await self._execute_stage(job, stage_name)
                    stage_duration = time.time() - stage_start

                    job.completed_stages.append(stage_name)
                    job.stage_timings[stage_name] = round(stage_duration, 1)
                    self._progress.on_vm_stage_complete(job, stage_name, stage_duration)

                    # After validate, rebuild stage list (OS is now known)
                    if stage_name == "validate":
                        stages = await self._build_stage_list(job)
                        stage_idx = 0  # Reset index — completed stages will be skipped

                job.status = VMStatus.COMPLETE
                job.completed_at = time.time()
                self._progress.on_vm_complete(job)

            except Exception as e:
                job.status = VMStatus.FAILED
                job.error = str(e)
                job.error_stage = job.current_stage
                job.completed_at = time.time()
                logger.error(f"VM '{job.vm_name}' failed at {job.current_stage}: {e}")
                self._progress.on_vm_failed(job, str(e))

            # Save state after each VM completes
            self._save_state()

    async def _build_stage_list(self, job: VMJob) -> list[str]:
        """Build the stage list based on detected OS family."""
        if job.os_family == "windows":
            stages = [
                "validate", "snapshot", "export", "convert",
                "clean_tools", "inject_virtio", "fix_bootloader",
                "ensure_uefi", "upload_s3", "import_scw", "verify", "cleanup",
            ]
        else:
            # Default to Linux (includes unknown OS)
            stages = [
                "validate", "snapshot", "export", "convert",
                "adapt_guest", "ensure_uefi",
                "upload_s3", "import_scw", "verify", "cleanup",
            ]

        if job.skip_validation:
            stages = [s for s in stages if s != "validate"]

        return stages

    async def _execute_stage(self, job: VMJob, stage: str) -> None:
        """Execute a single pipeline stage with appropriate semaphore.

        Wraps the synchronous MigrationPipeline stage methods in asyncio
        and acquires the right semaphore for each resource type.
        """
        from vmware2scw.config import VMMigrationPlan
        from vmware2scw.pipeline.migration import MigrationPipeline
        from vmware2scw.pipeline.state import MigrationState

        # Build a MigrationState from the job's artifacts
        migration_state = MigrationState(
            migration_id=job.migration_id,
            vm_name=job.vm_name,
            target_type=job.target_type,
            zone=job.zone,
            current_stage=stage,
            completed_stages=list(job.completed_stages),
            artifacts=dict(job.artifacts),
        )

        plan = VMMigrationPlan(
            vm_name=job.vm_name,
            target_type=job.target_type or "POP2-2C-8G",  # Fallback
            zone=job.zone,
            tags=job.tags,
            skip_validation=job.skip_validation,
            network_mapping=job.network_mapping,
        )

        pipeline = MigrationPipeline(self.config)

        # Acquire resource-specific semaphore
        sem = self._get_stage_semaphore(job, stage)

        if sem:
            async with sem:
                await asyncio.to_thread(pipeline._execute_stage, stage, plan, migration_state)
        else:
            await asyncio.to_thread(pipeline._execute_stage, stage, plan, migration_state)

        # Copy artifacts back to job
        job.artifacts = dict(migration_state.artifacts)

        # Update job metadata from validate results
        if stage == "validate":
            vm_info = job.artifacts.get("vm_info", {})
            guest_os = vm_info.get("guest_os", "")
            job.firmware = vm_info.get("firmware", "bios")
            job.esxi_host = vm_info.get("host", "")
            job.total_disk_gb = vm_info.get("total_disk_gb", 0)
            if "win" in guest_os.lower():
                job.os_family = "windows"
            else:
                job.os_family = "linux"

    def _get_stage_semaphore(self, job: VMJob, stage: str) -> asyncio.Semaphore | None:
        """Get the appropriate semaphore for a pipeline stage."""
        if stage in ("export", "snapshot"):
            return self.semaphores.get_host_semaphore(job.esxi_host or "default")
        if stage in ("convert", "adapt_guest", "clean_tools", "inject_virtio",
                      "fix_bootloader", "ensure_uefi"):
            return self.semaphores.disk_io
        if stage == "upload_s3":
            return self.semaphores.s3_upload
        if stage in ("import_scw", "verify"):
            return self.semaphores.scw_api
        return None  # validate, cleanup — no semaphore needed

    def _stage_to_status(self, stage: str) -> VMStatus:
        """Map pipeline stage to VMJob status for dashboard display."""
        mapping = {
            "validate": VMStatus.VALIDATING,
            "snapshot": VMStatus.EXPORTING,
            "export": VMStatus.EXPORTING,
            "convert": VMStatus.CONVERTING,
            "adapt_guest": VMStatus.ADAPTING,
            "clean_tools": VMStatus.ADAPTING,
            "inject_virtio": VMStatus.ADAPTING,
            "fix_bootloader": VMStatus.ADAPTING,
            "ensure_uefi": VMStatus.ADAPTING,
            "upload_s3": VMStatus.UPLOADING,
            "import_scw": VMStatus.IMPORTING,
            "verify": VMStatus.VERIFYING,
            "cleanup": VMStatus.CLEANING,
        }
        return mapping.get(stage, VMStatus.PENDING)

    def _save_state(self) -> None:
        """Persist batch state to disk."""
        if self.state:
            path = self._state_path(self.state.batch_id)
            try:
                self.state.save(path)
            except Exception as e:
                logger.warning(f"Failed to save batch state: {e}")


# ═══════════════════════════════════════════════════════════════════
#  Post-Migration Report Generator
# ═══════════════════════════════════════════════════════════════════

def generate_report(state: BatchState, output_path: Path | None = None) -> str:
    """Generate a Markdown migration report.

    Args:
        state: Completed BatchState
        output_path: Optional path to write the report file

    Returns:
        Report as Markdown string
    """
    duration_min = state.duration_s / 60
    lines = [
        f"# Migration Report — Batch `{state.batch_id}`",
        "",
        f"**Date:** {datetime.fromtimestamp(state.started_at).strftime('%Y-%m-%d %H:%M')}",
        f"**Duration:** {duration_min:.0f} min",
        f"**Status:** {state.status.value.upper()}",
        "",
        f"## Summary",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Total VMs | {len(state.jobs)} |",
        f"| Succeeded | {len(state.succeeded)} |",
        f"| Failed | {len(state.failed)} |",
        f"| Duration | {duration_min:.0f} min |",
        "",
    ]

    # Successful migrations
    if state.succeeded:
        lines += [
            "## Successful Migrations",
            "",
            "| VM | Target Type | OS | Duration | Image ID |",
            "|------|------|------|------|------|",
        ]
        for job in state.succeeded:
            image_id = job.artifacts.get("scaleway_image_id", "—")
            lines.append(
                f"| {job.vm_name} | {job.target_type} | {job.os_family} | "
                f"{job.duration_str} | `{image_id}` |"
            )
        lines.append("")

    # Failed migrations
    if state.failed:
        lines += [
            "## Failed Migrations",
            "",
            "| VM | Failed Stage | Error | Resume Command |",
            "|------|------|------|------|",
        ]
        for job in state.failed:
            error_short = (job.error or "unknown")[:80]
            lines.append(
                f"| {job.vm_name} | {job.error_stage} | {error_short} | "
                f"`vmware2scw batch resume --batch-id {state.batch_id}` |"
            )
        lines.append("")

    # Stage timing analysis
    if state.succeeded:
        lines += [
            "## Stage Timing Analysis",
            "",
            "Average duration per stage (successful VMs):",
            "",
            "| Stage | Avg Duration | Min | Max |",
            "|-------|------|------|------|",
        ]
        all_stages = set()
        for job in state.succeeded:
            all_stages.update(job.stage_timings.keys())

        for stage in sorted(all_stages):
            timings = [j.stage_timings.get(stage, 0) for j in state.succeeded if stage in j.stage_timings]
            if timings:
                avg_t = sum(timings) / len(timings)
                min_t = min(timings)
                max_t = max(timings)
                lines.append(f"| {stage} | {avg_t:.0f}s | {min_t:.0f}s | {max_t:.0f}s |")
        lines.append("")

    report = "\n".join(lines)

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report)
        logger.info(f"Report saved to {output_path}")

    return report

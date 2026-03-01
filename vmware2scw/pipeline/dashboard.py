"""Rich live dashboard for batch migration monitoring.

Displays a live-updating table with:
  - Per-VM status, current stage, duration, progress bar
  - Wave progress
  - Global statistics (succeeded, failed, in-progress)
  - ETA based on completed VMs

Uses Rich's Live display for flicker-free terminal updates.
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime
from typing import TYPE_CHECKING

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from vmware2scw.pipeline.batch_orchestrator import BatchProgressCallback, BatchState, VMJob, VMStatus


# ═══════════════════════════════════════════════════════════════════
#  Status Styling
# ═══════════════════════════════════════════════════════════════════

STATUS_STYLES = {
    "pending":    ("⏳", "dim"),
    "validating": ("🔍", "cyan"),
    "exporting":  ("📤", "blue"),
    "converting": ("🔄", "yellow"),
    "adapting":   ("🔧", "magenta"),
    "uploading":  ("☁️ ", "blue"),
    "importing":  ("📥", "cyan"),
    "verifying":  ("✅", "green"),
    "cleaning":   ("🧹", "dim"),
    "complete":   ("✅", "bold green"),
    "failed":     ("❌", "bold red"),
    "skipped":    ("⏭️ ", "dim"),
}

STAGE_LABELS = {
    "validate": "Validate",
    "snapshot": "Snapshot",
    "export": "Export VMDK",
    "convert": "VMDK→qcow2",
    "adapt_guest": "Adapt Guest",
    "clean_tools": "Clean Tools",
    "inject_virtio": "VirtIO Inject",
    "fix_bootloader": "Fix Boot",
    "ensure_uefi": "BIOS→UEFI",
    "upload_s3": "Upload S3",
    "import_scw": "Import SCW",
    "verify": "Verify",
    "cleanup": "Cleanup",
}


# ═══════════════════════════════════════════════════════════════════
#  Dashboard Implementation
# ═══════════════════════════════════════════════════════════════════

class RichDashboard:
    """Live Rich dashboard for batch migration monitoring.

    Implements BatchProgressCallback to receive real-time updates from
    the orchestrator and renders them as a live terminal UI.

    Usage:
        dashboard = RichDashboard()
        orchestrator.set_progress_callback(dashboard)

        # The dashboard auto-starts when on_batch_start is called
        # and stops when on_batch_complete is called
    """

    def __init__(self, console: Console | None = None):
        self.console = console or Console()
        self._state: BatchState | None = None
        self._jobs: dict[str, VMJob] = {}
        self._wave_info = {"index": 0, "name": "", "count": 0}
        self._start_time: float = 0
        self._live: Live | None = None
        self._update_task: asyncio.Task | None = None
        self._paused: bool = False
        self._pause_reason: str = ""

    # ─── BatchProgressCallback Implementation ─────────────────────

    def on_batch_start(self, state) -> None:
        self._state = state
        self._start_time = time.time()
        for job in state.jobs:
            self._jobs[job.vm_name] = job

        self.console.print()
        self.console.print(Panel.fit(
            f"[bold]Batch Migration[/bold]: {state.batch_id}\n"
            f"VMs: {len(state.jobs)} | Waves: {state.total_waves}",
            border_style="cyan",
        ))

    def on_wave_start(self, wave_index: int, wave_name: str, vm_count: int) -> None:
        self._wave_info = {"index": wave_index, "name": wave_name, "count": vm_count}

    def on_wave_complete(self, wave_index: int, succeeded: int, failed: int) -> None:
        icon = "✅" if failed == 0 else "⚠️"
        self.console.print(
            f"\n{icon} Wave {wave_index + 1} complete: "
            f"[green]{succeeded} succeeded[/green], "
            f"[red]{failed} failed[/red]"
        )

    def on_vm_stage_start(self, job, stage: str) -> None:
        self._jobs[job.vm_name] = job

    def on_vm_stage_complete(self, job, stage: str, duration_s: float) -> None:
        self._jobs[job.vm_name] = job
        label = STAGE_LABELS.get(stage, stage)
        self.console.print(
            f"  [green]✓[/green] {job.vm_name}: {label} ({duration_s:.0f}s)"
        )

    def on_vm_complete(self, job) -> None:
        self._jobs[job.vm_name] = job
        image_id = job.artifacts.get("scaleway_image_id", "")
        self.console.print(
            f"  [bold green]✅ {job.vm_name}[/bold green] complete "
            f"({job.duration_str})"
            + (f" → image: {image_id}" if image_id else "")
        )

    def on_vm_failed(self, job, error: str) -> None:
        self._jobs[job.vm_name] = job
        self.console.print(
            f"  [bold red]❌ {job.vm_name}[/bold red] failed at "
            f"[red]{job.error_stage}[/red]: {error[:100]}"
        )

    def on_batch_complete(self, state) -> None:
        self._state = state
        self.console.print()
        self._print_summary(state)

    def on_wave_pause(self, wave_index: int, reason: str) -> None:
        self._paused = True
        self._pause_reason = reason
        self.console.print(f"\n[yellow]⏸  {reason}[/yellow]")
        self.console.print("[yellow]Press Enter to continue, or Ctrl+C to abort...[/yellow]")

    # ─── Summary Display ──────────────────────────────────────────

    def _print_summary(self, state) -> None:
        """Print final batch summary with statistics."""
        duration_min = state.duration_s / 60

        # Summary table
        table = Table(title="Migration Summary", border_style="cyan")
        table.add_column("Metric", style="bold")
        table.add_column("Value", justify="right")

        table.add_row("Total VMs", str(len(state.jobs)))
        table.add_row("Succeeded", f"[green]{len(state.succeeded)}[/green]")
        table.add_row("Failed", f"[red]{len(state.failed)}[/red]" if state.failed else "0")
        table.add_row("Duration", f"{duration_min:.1f} min")
        table.add_row("Batch ID", state.batch_id)

        self.console.print(table)

        # Detail table for completed VMs
        if state.succeeded:
            detail = Table(title="Completed Migrations", border_style="green")
            detail.add_column("VM", style="cyan")
            detail.add_column("Type", style="white")
            detail.add_column("OS")
            detail.add_column("Duration", justify="right")
            detail.add_column("Image ID", style="dim")

            for job in state.succeeded:
                image_id = job.artifacts.get("scaleway_image_id", "—")
                detail.add_row(
                    job.vm_name,
                    job.target_type,
                    job.os_family,
                    job.duration_str,
                    image_id[:16] + "..." if len(image_id) > 16 else image_id,
                )
            self.console.print(detail)

        # Failed VMs
        if state.failed:
            fail_table = Table(title="Failed Migrations", border_style="red")
            fail_table.add_column("VM", style="cyan")
            fail_table.add_column("Failed Stage", style="red")
            fail_table.add_column("Error")

            for job in state.failed:
                fail_table.add_row(
                    job.vm_name,
                    job.error_stage or "—",
                    (job.error or "unknown")[:80],
                )
            self.console.print(fail_table)
            self.console.print(
                f"\n[yellow]Resume failed VMs with:[/yellow] "
                f"vmware2scw batch resume --batch-id {state.batch_id}"
            )

    # ─── Status Table (for live display) ──────────────────────────

    def render_status_table(self) -> Table:
        """Render current status of all VMs as a Rich Table."""
        if not self._state:
            return Table()

        table = Table(
            title=f"Batch {self._state.batch_id} — "
                  f"Wave {self._state.current_wave}/{self._state.total_waves}",
            border_style="cyan",
        )
        table.add_column("VM", style="cyan", no_wrap=True, max_width=25)
        table.add_column("Status", justify="center", min_width=12)
        table.add_column("Stage", min_width=14)
        table.add_column("OS", justify="center", min_width=7)
        table.add_column("Type", min_width=14)
        table.add_column("Duration", justify="right", min_width=8)
        table.add_column("Progress", min_width=20)

        for job in self._state.jobs:
            icon, style = STATUS_STYLES.get(job.status.value, ("?", "white"))
            stage_label = STAGE_LABELS.get(job.current_stage, job.current_stage or "—")

            # Progress indicator
            total_stages = 10 if job.os_family != "windows" else 12
            done = len(job.completed_stages)
            if job.status.value == "complete":
                progress = "[green]████████████████[/green]"
            elif job.status.value == "failed":
                progress = f"[red]{'█' * done}{'░' * (total_stages - done)}[/red]"
            elif done > 0:
                progress = f"[cyan]{'█' * done}{'░' * (total_stages - done)}[/cyan]"
            else:
                progress = f"[dim]{'░' * total_stages}[/dim]"

            table.add_row(
                job.vm_name,
                f"[{style}]{icon} {job.status.value}[/{style}]",
                stage_label if job.status.value not in ("complete", "failed", "pending") else "—",
                job.os_family or "—",
                job.target_type or "auto",
                job.duration_str if job.started_at else "—",
                progress,
            )

        return table


# ═══════════════════════════════════════════════════════════════════
#  Estimate Display
# ═══════════════════════════════════════════════════════════════════

def print_estimate(estimate: dict, console: Console | None = None) -> None:
    """Display a pre-flight migration estimate as a Rich panel."""
    console = console or Console()

    table = Table(title="Pre-flight Migration Estimate", border_style="cyan")
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")

    table.add_row("Total VMs", str(estimate["total_vms"]))
    table.add_row("  Linux", str(estimate["linux_vms"]))
    table.add_row("  Windows", str(estimate["windows_vms"]))
    table.add_row("Total disk", f"{estimate['total_disk_gb']:.0f} GB")
    table.add_row("Work space needed", f"{estimate['required_work_space_gb']:.0f} GB")
    table.add_row("Est. duration", f"{estimate['estimated_duration_minutes']:.0f} min")
    table.add_row("Est. monthly cost", f"€{estimate['estimated_monthly_cost_eur']:.2f}/mo")

    console.print(table)

    # Breakdown
    breakdown = estimate.get("breakdown", {})
    if breakdown:
        bd = Table(title="Time Breakdown", border_style="dim")
        bd.add_column("Phase")
        bd.add_column("Est. Time", justify="right")
        for phase, minutes in breakdown.items():
            bd.add_row(phase.replace("_min", "").replace("_", " ").title(), f"{minutes:.0f} min")
        console.print(bd)

    # Warnings
    for w in estimate.get("warnings", []):
        console.print(f"[yellow]⚠  {w}[/yellow]")


def print_plan_summary(plan_data: dict, console: Console | None = None) -> None:
    """Display a batch plan summary before execution."""
    console = console or Console()
    metadata = plan_data.get("metadata", {})
    migrations = plan_data.get("migrations", [])

    table = Table(title="Batch Migration Plan", border_style="cyan")
    table.add_column("#", justify="right", style="dim")
    table.add_column("VM", style="cyan", no_wrap=True)
    table.add_column("Target Type", style="green")
    table.add_column("Priority", justify="center")
    table.add_column("Notes", style="dim", max_width=50)

    for i, m in enumerate(migrations, 1):
        table.add_row(
            str(i),
            m.get("vm_name", m.get("vm_pattern", "?")),
            m.get("target_type", "[auto-map]"),
            str(m.get("priority", 5)),
            m.get("notes", ""),
        )

    console.print(table)
    console.print(
        f"\n[dim]Total: {len(migrations)} VMs | "
        f"Generated: {metadata.get('generated_at', '?')} | "
        f"vCenter: {metadata.get('vcenter', '?')}[/dim]"
    )

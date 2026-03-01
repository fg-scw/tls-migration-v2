"""Enhanced CLI for vmware2scw — batch operations, filtering, and live dashboard.

New commands:
  vmware2scw inventory-plan   — Filter VMs + auto-map + export to batch plan YAML
  vmware2scw batch estimate   — Pre-flight estimation (time, space, cost)
  vmware2scw batch run        — Execute batch migration with live dashboard
  vmware2scw batch resume     — Resume a failed/paused batch
  vmware2scw batch status     — Show status of a batch migration
  vmware2scw batch report     — Generate post-migration report
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

import click
import yaml
from rich.console import Console
from rich.table import Table

from vmware2scw.config import AppConfig

console = Console()


def load_config(config_path: str | None) -> AppConfig:
    """Load configuration from file or environment."""
    if config_path:
        return AppConfig.from_yaml(config_path)
    # Try default locations
    for default in ["migration.yaml", "config.yaml", "/etc/vmware2scw/config.yaml"]:
        if Path(default).exists():
            return AppConfig.from_yaml(default)
    try:
        return AppConfig.from_env_and_args()
    except Exception as e:
        console.print(f"[red]Error loading config: {e}[/red]")
        console.print("Provide a --config file or set environment variables.")
        sys.exit(1)


@click.group()
@click.version_option(version="2.0.0", prog_name="vmware2scw")
def main():
    """VMware to Scaleway Instance migration tool.

    Migrate virtual machines from VMware vSphere/vCenter environments
    to Scaleway Instances (KVM/qcow2).

    Quick start:
      1. vmware2scw inventory-plan --config migration.yaml --auto-map -o plan.yaml
      2. Edit plan.yaml (review target types, set priorities/waves)
      3. vmware2scw batch estimate --plan plan.yaml
      4. vmware2scw batch run --plan plan.yaml --config migration.yaml
    """
    pass


# ═══════════════════════════════════════════════════════════════════
#  INVENTORY-PLAN: List, filter, auto-map, export
# ═══════════════════════════════════════════════════════════════════

@main.command("inventory-plan")
@click.option("--config", "config_path", type=click.Path(exists=True), help="Configuration file")
@click.option("--filter", "-f", "filters", multiple=True,
              help="Filter: 'name:web-*', 'os:linux', 'folder:/Prod', 'host:esxi-*', etc.")
@click.option("--min-cpu", type=int, help="Minimum vCPU count")
@click.option("--max-cpu", type=int, help="Maximum vCPU count")
@click.option("--min-ram", type=float, help="Minimum RAM in GB")
@click.option("--max-ram", type=float, help="Maximum RAM in GB")
@click.option("--min-disk", type=float, help="Minimum total disk in GB")
@click.option("--max-disk", type=float, help="Maximum total disk in GB")
@click.option("--auto-map/--no-auto-map", default=True, help="Auto-detect Scaleway instance types")
@click.option("--sizing", type=click.Choice(["exact", "optimize", "cost"]), default="optimize",
              help="Sizing strategy for auto-mapping")
@click.option("--zone", default="fr-par-1", help="Default Scaleway zone")
@click.option("--tag", "default_tags", multiple=True, help="Default tags for Scaleway instances")
@click.option("--output", "-o", type=click.Path(), help="Output batch plan YAML file")
@click.option("--format", "fmt", type=click.Choice(["table", "yaml", "json"]), default="table")
def inventory_plan(
    config_path, filters, min_cpu, max_cpu, min_ram, max_ram,
    min_disk, max_disk, auto_map, sizing, zone, default_tags, output, fmt,
):
    """List VMs from vCenter with filtering and auto-mapping to Scaleway types.

    Generates a batch migration plan YAML file for review before execution.

    Examples:
      # All Linux VMs with auto-mapping
      vmware2scw inventory-plan --config migration.yaml --filter os:linux --auto-map -o plan.yaml

      # Production web servers with >4 CPUs
      vmware2scw inventory-plan -f "name:web-prod-*" -f "os:linux" --min-cpu 4 -o plan.yaml

      # Windows VMs in a specific folder
      vmware2scw inventory-plan -f "folder:/DC1/Production/Windows" -o windows-plan.yaml
    """
    config = load_config(config_path)

    from vmware2scw.pipeline.inventory import InventoryFilter, generate_batch_plan
    from vmware2scw.vmware.client import VSphereClient
    from vmware2scw.vmware.inventory import VMInventory

    # Build filter
    inv_filter = InventoryFilter.from_cli_filters(
        list(filters),
        min_cpu=min_cpu, max_cpu=max_cpu,
        min_ram_gb=min_ram, max_ram_gb=max_ram,
        min_disk_gb=min_disk, max_disk_gb=max_disk,
    )

    # Connect to vCenter
    with console.status("[bold green]Connecting to vCenter..."):
        client = VSphereClient()
        pw = config.vmware.password.get_secret_value() if config.vmware.password else ""
        client.connect(config.vmware.vcenter, config.vmware.username, pw,
                       insecure=config.vmware.insecure)

    # Collect inventory
    with console.status("[bold green]Collecting VM inventory..."):
        inv = VMInventory(client)
        all_vms = inv.list_all_vms()

    client.disconnect()

    # Convert to dicts and apply filters
    vm_dicts = [vm.model_dump() for vm in all_vms]
    filtered = [vm for vm in vm_dicts if inv_filter.matches(vm)]

    console.print(f"\n[dim]Inventory: {len(all_vms)} total, {len(filtered)} after filters[/dim]")

    if not filtered:
        console.print("[yellow]No VMs matched the filters.[/yellow]")
        return

    # Generate plan
    plan_data = generate_batch_plan(
        filtered,
        vcenter=config.vmware.vcenter,
        zone=zone,
        sizing_strategy=sizing,
        default_tags=list(default_tags) if default_tags else None,
        auto_map=auto_map,
    )

    # Output
    if fmt == "yaml" or output:
        if output:
            with open(output, "w") as f:
                yaml.dump(plan_data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
            console.print(f"[green]Plan saved to {output}[/green]")
            console.print(f"[dim]Next: vmware2scw batch estimate --plan {output}[/dim]")
        else:
            console.print(yaml.dump(plan_data, default_flow_style=False, sort_keys=False))

    elif fmt == "json":
        console.print_json(json.dumps(plan_data, indent=2, default=str))

    else:
        # Table display
        from vmware2scw.pipeline.dashboard import print_plan_summary
        print_plan_summary(plan_data, console)

        if not output:
            console.print(
                "\n[dim]Tip: Add -o plan.yaml to save as batch plan for execution[/dim]"
            )


# ═══════════════════════════════════════════════════════════════════
#  BATCH: estimate, run, resume, status, report
# ═══════════════════════════════════════════════════════════════════

@main.group()
def batch():
    """Batch migration operations — estimate, run, resume, report."""
    pass


@batch.command("estimate")
@click.option("--plan", "plan_path", required=True, type=click.Path(exists=True),
              help="Batch migration plan YAML")
@click.option("--available-disk", type=float, help="Available disk space in GB (for space check)")
@click.option("--concurrency", type=int, default=5, help="Expected parallelism level")
def batch_estimate(plan_path, available_disk, concurrency):
    """Estimate time, space, and cost for a batch migration.

    Analyzes the plan and provides estimates before committing to execution.
    """
    with open(plan_path) as f:
        plan_data = yaml.safe_load(f)

    from vmware2scw.pipeline.dashboard import print_estimate
    from vmware2scw.pipeline.inventory import estimate_migration

    # Auto-detect available disk if not specified
    if available_disk is None:
        try:
            stat = shutil.disk_usage("/var/lib/vmware2scw/work")
            available_disk = stat.free / (1024**3)
            console.print(f"[dim]Detected available disk: {available_disk:.0f} GB[/dim]")
        except Exception:
            pass

    estimate = estimate_migration(plan_data, available_disk, concurrency)
    print_estimate(estimate, console)

    console.print(f"\n[dim]Ready? Run: vmware2scw batch run --plan {plan_path}[/dim]")


@batch.command("run")
@click.option("--plan", "plan_path", required=True, type=click.Path(exists=True),
              help="Batch migration plan YAML")
@click.option("--config", "config_path", type=click.Path(exists=True), help="Configuration file")
@click.option("--dry-run", is_flag=True, default=False, help="Simulate without executing")
@click.option("--yes", "-y", is_flag=True, default=False, help="Skip confirmation prompt")
@click.option("--report", "report_path", type=click.Path(), help="Path for post-migration report")
def batch_run(plan_path, config_path, dry_run, yes, report_path):
    """Execute a batch migration plan with live dashboard.

    Runs all VMs in the plan in parallel (bounded by concurrency limits),
    with wave-based execution and automatic state persistence for resume.
    """
    config = load_config(config_path)

    from vmware2scw.pipeline.batch_plan import BatchPlan
    plan = BatchPlan.from_yaml(plan_path)

    # Show plan summary
    from vmware2scw.pipeline.dashboard import print_plan_summary
    with open(plan_path) as f:
        plan_data = yaml.safe_load(f)
    print_plan_summary(plan_data, console)

    if dry_run:
        console.print("\n[yellow]DRY RUN — No changes will be made[/yellow]")
        console.print(f"Would migrate {len(plan.migrations)} VM(s) in "
                      f"{len(plan.waves) or 1} wave(s)")
        return

    if not yes:
        click.confirm(
            f"\nProceed with migration of {len(plan.migrations)} VM(s)?",
            abort=True,
        )

    # Resolve VMs against inventory
    # For now, create ResolvedVMs directly from plan entries
    # (full resolution requires vCenter connection, done during validate stage)
    from vmware2scw.pipeline.batch_plan import ResolvedVM
    resolved_vms = []
    for entry in plan.migrations:
        resolved_vms.append(ResolvedVM(
            vm_name=entry.vm_name or entry.vm_pattern or "",
            target_type=entry.target_type,
            zone=entry.zone or plan.defaults.zone,
            sizing_strategy=entry.sizing_strategy or plan.defaults.sizing_strategy,
            priority=entry.priority,
            wave=entry.wave,
            skip_validation=entry.skip_validation or plan.defaults.skip_validation,
            tags=entry.tags or plan.defaults.tags,
            network_mapping=entry.network_mapping or plan.defaults.network_mapping,
            notes=entry.notes,
        ))

    # Build wave groups
    waves = plan.get_waves(resolved_vms)

    # Create orchestrator with dashboard
    from vmware2scw.pipeline.batch_orchestrator import BatchOrchestrator, generate_report
    from vmware2scw.pipeline.dashboard import RichDashboard

    orchestrator = BatchOrchestrator(config, plan)
    dashboard = RichDashboard(console)
    orchestrator.set_progress_callback(dashboard)

    # Run
    console.print(f"\n[bold]Starting batch migration...[/bold]")
    state = asyncio.run(orchestrator.run(waves=waves))

    # Generate report
    if report_path or state.failed:
        rpath = Path(report_path) if report_path else (
            config.conversion.work_dir / "batch-state" / f"report-{state.batch_id}.md"
        )
        report = generate_report(state, rpath)
        console.print(f"\n[dim]Report saved to {rpath}[/dim]")

    # Exit code
    if state.failed:
        sys.exit(1)


@batch.command("resume")
@click.option("--batch-id", required=True, help="Batch ID to resume")
@click.option("--config", "config_path", type=click.Path(exists=True), help="Configuration file")
def batch_resume(batch_id, config_path):
    """Resume a failed or paused batch migration.

    Retries all failed VMs from their last successful stage.
    """
    config = load_config(config_path)

    from vmware2scw.pipeline.batch_orchestrator import BatchOrchestrator, generate_report
    from vmware2scw.pipeline.dashboard import RichDashboard

    orchestrator = BatchOrchestrator(config)
    dashboard = RichDashboard(console)
    orchestrator.set_progress_callback(dashboard)

    console.print(f"[bold]Resuming batch {batch_id}...[/bold]")
    state = asyncio.run(orchestrator.resume(batch_id))

    # Report
    rpath = config.conversion.work_dir / "batch-state" / f"report-{state.batch_id}.md"
    generate_report(state, rpath)

    if state.failed:
        sys.exit(1)


@batch.command("status")
@click.option("--batch-id", help="Specific batch ID (default: most recent)")
@click.option("--config", "config_path", type=click.Path(exists=True), help="Configuration file")
def batch_status(batch_id, config_path):
    """Show status of a batch migration."""
    config = load_config(config_path)
    state_dir = config.conversion.work_dir / "batch-state"

    if batch_id:
        state_path = state_dir / f"batch-{batch_id}.json"
    else:
        # Find most recent
        state_files = sorted(state_dir.glob("batch-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not state_files:
            console.print("[red]No batch migrations found[/red]")
            return
        state_path = state_files[0]

    if not state_path.exists():
        console.print(f"[red]Batch state not found: {state_path}[/red]")
        return

    from vmware2scw.pipeline.batch_orchestrator import BatchState

    state = BatchState.load(state_path)

    from vmware2scw.pipeline.dashboard import RichDashboard
    dashboard = RichDashboard(console)
    dashboard._state = state
    for job in state.jobs:
        dashboard._jobs[job.vm_name] = job

    console.print(f"\n[bold]Batch: {state.batch_id}[/bold] ({state.status.value})")
    console.print(f"  Started: {state.started_at}")
    console.print(f"  Duration: {state.duration_s / 60:.1f} min")
    console.print(f"  Succeeded: {len(state.succeeded)} | Failed: {len(state.failed)}")

    console.print(dashboard.render_status_table())


@batch.command("report")
@click.option("--batch-id", required=True, help="Batch ID")
@click.option("--config", "config_path", type=click.Path(exists=True), help="Configuration file")
@click.option("--output", "-o", type=click.Path(), help="Output file (default: stdout)")
def batch_report(batch_id, config_path, output):
    """Generate a post-migration report for a completed batch."""
    config = load_config(config_path)
    state_path = config.conversion.work_dir / "batch-state" / f"batch-{batch_id}.json"

    if not state_path.exists():
        console.print(f"[red]Batch state not found: {state_path}[/red]")
        sys.exit(1)

    from vmware2scw.pipeline.batch_orchestrator import BatchState, generate_report

    state = BatchState.load(state_path)
    report = generate_report(state, Path(output) if output else None)

    if not output:
        console.print(report)
    else:
        console.print(f"[green]Report saved to {output}[/green]")


# ═══════════════════════════════════════════════════════════════════
#  Legacy commands (preserved from v1)
# ═══════════════════════════════════════════════════════════════════

@main.command()
@click.option("--vcenter", required=True, help="vCenter hostname or IP")
@click.option("--username", required=True, help="vCenter username")
@click.option("--password-file", type=click.Path(exists=True), help="File containing vCenter password")
@click.option("--password", help="vCenter password (prefer --password-file)")
@click.option("--insecure", is_flag=True, default=False, help="Skip SSL verification")
@click.option("--output", "-o", type=click.Path(), help="Output file (JSON)")
@click.option("--format", "fmt", type=click.Choice(["table", "json"]), default="table")
def inventory(vcenter, username, password_file, password, insecure, output, fmt):
    """List all VMs in a vCenter environment (basic inventory)."""
    from vmware2scw.vmware.client import VSphereClient
    from vmware2scw.vmware.inventory import VMInventory

    if password_file:
        password = Path(password_file).read_text().strip()
    elif not password:
        password = click.prompt("vCenter password", hide_input=True)

    with console.status("[bold green]Connecting to vCenter..."):
        client = VSphereClient()
        client.connect(vcenter, username, password, insecure=insecure)

    with console.status("[bold green]Collecting VM inventory..."):
        inv = VMInventory(client)
        vms = inv.list_all_vms()

    if fmt == "json":
        data = [vm.model_dump() for vm in vms]
        if output:
            Path(output).write_text(json.dumps(data, indent=2, default=str))
            console.print(f"[green]Inventory saved to {output}[/green]")
        else:
            console.print_json(json.dumps(data, indent=2, default=str))
    else:
        table = Table(title=f"VM Inventory — {vcenter}")
        table.add_column("Name", style="cyan", no_wrap=True)
        table.add_column("State", style="green")
        table.add_column("CPU", justify="right")
        table.add_column("RAM (MB)", justify="right")
        table.add_column("Disks", justify="right")
        table.add_column("Total (GB)", justify="right")
        table.add_column("OS", style="magenta")
        table.add_column("Firmware")
        table.add_column("Host")

        for vm in vms:
            total_gb = sum(d.size_gb for d in vm.disks)
            table.add_row(
                vm.name, vm.power_state, str(vm.cpu), str(vm.memory_mb),
                str(len(vm.disks)), f"{total_gb:.1f}",
                vm.guest_os_full or vm.guest_os, vm.firmware, vm.host,
            )
        console.print(table)
        console.print(f"\n[dim]Total: {len(vms)} VMs[/dim]")

    client.disconnect()


@main.command()
@click.option("--vm", required=True, help="VM name to migrate")
@click.option("--target-type", required=True, help="Scaleway instance type")
@click.option("--zone", default="fr-par-1", help="Scaleway availability zone")
@click.option("--config", "config_path", type=click.Path(exists=True), help="Configuration file")
@click.option("--skip-validation", is_flag=True, default=False)
@click.option("--dry-run", is_flag=True, default=False)
def migrate(vm, target_type, zone, config_path, skip_validation, dry_run):
    """Migrate a single VM from VMware to Scaleway."""
    config = load_config(config_path)
    from vmware2scw.config import VMMigrationPlan
    from vmware2scw.pipeline.migration import MigrationPipeline

    plan = VMMigrationPlan(
        vm_name=vm, target_type=target_type, zone=zone,
        skip_validation=skip_validation,
    )
    pipeline = MigrationPipeline(config)

    if dry_run:
        console.print("[yellow]DRY RUN — No changes will be made[/yellow]")
        pipeline.dry_run(plan)
    else:
        result = pipeline.run(plan)
        if result.success:
            console.print(f"\n[bold green]✅ Migration complete![/bold green]")
            console.print(f"  Instance ID: {result.instance_id}")
            console.print(f"  Duration: {result.duration}")
        else:
            console.print(f"\n[bold red]❌ Migration failed at '{result.failed_stage}'[/bold red]")
            console.print(f"  Error: {result.error}")
            sys.exit(1)


if __name__ == "__main__":
    main()

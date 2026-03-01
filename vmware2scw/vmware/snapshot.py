"""VMware snapshot management for migration.

Creates and cleans up migration snapshots to ensure data consistency
during VMDK export.

Pipeline stage: snapshot (stage 2)
"""

from __future__ import annotations

import logging
import time

from pyVmomi import vim

from vmware2scw.vmware.client import VSphereClient

logger = logging.getLogger(__name__)


def _wait_for_task(task, timeout=600):
    """Wait for a vCenter task to complete."""
    start = time.time()
    while task.info.state not in (vim.TaskInfo.State.success, vim.TaskInfo.State.error):
        if time.time() - start > timeout:
            raise RuntimeError(f"Task timed out after {timeout}s: {task.info.descriptionId}")
        time.sleep(2)

    if task.info.state == vim.TaskInfo.State.error:
        error = task.info.error
        raise RuntimeError(f"Task failed: {error.msg if error else 'unknown error'}")

    return task.info.result


class SnapshotManager:
    """Manage VMware snapshots for migration."""

    def __init__(self, client: VSphereClient):
        self.client = client

    def create_migration_snapshot(self, vm_name: str, snapshot_name: str = "vmware2scw-migration") -> str:
        """Create a quiesce snapshot for consistent disk export.

        Args:
            vm_name: VM name in vCenter
            snapshot_name: Name for the snapshot

        Returns:
            Snapshot MoRef ID
        """
        vm = self.client.find_vm_by_name(vm_name)
        if not vm:
            raise ValueError(f"VM '{vm_name}' not found")

        # Check if migration snapshot already exists
        existing = self._find_snapshot(vm, snapshot_name)
        if existing:
            logger.info(f"Migration snapshot '{snapshot_name}' already exists — reusing")
            return str(existing)

        logger.info(f"Creating snapshot '{snapshot_name}' on VM '{vm_name}'...")

        # memory=False (no memory state), quiesce=True (filesystem consistent)
        try:
            task = vm.CreateSnapshot_Task(
                name=snapshot_name,
                description="vmware2scw migration snapshot",
                memory=False,
                quiesce=True,
            )
        except vim.fault.ToolsUnavailable:
            logger.warning("VMware Tools not available — creating crash-consistent snapshot")
            task = vm.CreateSnapshot_Task(
                name=snapshot_name,
                description="vmware2scw migration snapshot (crash-consistent)",
                memory=False,
                quiesce=False,
            )

        result = _wait_for_task(task)
        logger.info(f"Snapshot '{snapshot_name}' created successfully")
        return str(result)

    def delete_migration_snapshot(self, vm_name: str, snapshot_name: str) -> None:
        """Delete a migration snapshot."""
        vm = self.client.find_vm_by_name(vm_name)
        if not vm:
            logger.warning(f"VM '{vm_name}' not found — cannot delete snapshot")
            return

        snap = self._find_snapshot(vm, snapshot_name)
        if not snap:
            logger.info(f"Snapshot '{snapshot_name}' not found on '{vm_name}' — already deleted?")
            return

        logger.info(f"Deleting snapshot '{snapshot_name}' on '{vm_name}'...")
        task = snap.RemoveSnapshot_Task(removeChildren=False)
        _wait_for_task(task)
        logger.info(f"Snapshot '{snapshot_name}' deleted")

    def list_snapshots(self, vm_name: str) -> list[str]:
        """List all snapshot names for a VM."""
        vm = self.client.find_vm_by_name(vm_name)
        if not vm or not vm.snapshot:
            return []

        names: list[str] = []
        self._walk_snapshots(vm.snapshot.rootSnapshotList, names)
        return names

    def cleanup_migration_snapshots(self, vm_name: str) -> None:
        """Remove all vmware2scw snapshots from a VM."""
        vm = self.client.find_vm_by_name(vm_name)
        if not vm or not vm.snapshot:
            return

        snaps: list = []
        self._find_all_migration_snapshots(vm.snapshot.rootSnapshotList, snaps)

        for snap in snaps:
            logger.info(f"Cleaning up snapshot '{snap.name}' on '{vm_name}'...")
            task = snap.snapshot.RemoveSnapshot_Task(removeChildren=False)
            _wait_for_task(task)

    def _find_snapshot(self, vm, name: str):
        """Find a snapshot by name, return the snapshot ManagedObject."""
        if not vm.snapshot:
            return None
        return self._search_snapshot_tree(vm.snapshot.rootSnapshotList, name)

    def _search_snapshot_tree(self, snap_list, name):
        for snap in snap_list:
            if snap.name == name:
                return snap.snapshot
            if snap.childSnapshotList:
                result = self._search_snapshot_tree(snap.childSnapshotList, name)
                if result:
                    return result
        return None

    def _walk_snapshots(self, snap_list, result):
        for s in snap_list:
            result.append(s.name)
            if s.childSnapshotList:
                self._walk_snapshots(s.childSnapshotList, result)

    def _find_all_migration_snapshots(self, snap_list, result):
        for s in snap_list:
            if s.name.startswith("vmware2scw"):
                result.append(s)
            if s.childSnapshotList:
                self._find_all_migration_snapshots(s.childSnapshotList, result)

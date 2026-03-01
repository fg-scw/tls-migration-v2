"""VMware VM inventory collection.

Collects VM information from vCenter using pyVmomi PropertyCollector
for efficient batch retrieval:
  - VM name, CPU, memory, disk layout
  - Guest OS detection
  - Firmware type (BIOS/EFI)
  - ESXi host, cluster, datacenter, folder
  - Power state and VMware Tools status

Requires: pyvmomi >= 8.0
"""

from __future__ import annotations

import logging
from typing import Optional

from pydantic import BaseModel, Field
from pyVmomi import vim

from vmware2scw.vmware.client import VSphereClient

logger = logging.getLogger(__name__)


class DiskInfo(BaseModel):
    """Information about a single VM disk."""
    name: str = ""
    size_gb: float = 0.0
    thin_provisioned: bool = True
    datastore: str = ""
    file_path: str = ""  # [datastore] path/to/disk.vmdk
    controller_type: str = ""  # "scsi", "nvme", "ide"


class NICInfo(BaseModel):
    """Information about a single VM network adapter."""
    mac_address: str = ""
    network: str = ""
    adapter_type: str = ""  # "vmxnet3", "e1000", "e1000e"
    connected: bool = False


class VMInfo(BaseModel):
    """Complete information about a VMware VM."""
    name: str
    moref: str = ""                    # Managed Object Reference
    power_state: str = ""              # "poweredOn", "poweredOff", "suspended"
    cpu: int = 0
    memory_mb: int = 0
    guest_os: str = ""                 # VMware guestId (e.g., "ubuntu64Guest")
    guest_os_full: str = ""            # Full guest OS name from VMware Tools
    firmware: str = "bios"             # "bios" or "efi"
    disks: list[DiskInfo] = Field(default_factory=list)
    nics: list[NICInfo] = Field(default_factory=list)
    total_disk_gb: float = 0.0
    networks: list[str] = Field(default_factory=list)
    host: str = ""                     # ESXi host
    cluster: str = ""                  # vCenter cluster
    datacenter: str = ""               # vCenter datacenter
    folder: str = ""                   # VM folder path
    resource_pool: str = ""
    tags: list[str] = Field(default_factory=list)
    tools_status: str = ""             # "toolsOk", "toolsNotInstalled", etc.
    tools_version: str = ""
    annotation: str = ""               # VM notes
    uuid: str = ""                     # BIOS UUID
    instance_uuid: str = ""            # vCenter instance UUID
    snapshots: list[str] = Field(default_factory=list)


def _get_folder_path(entity) -> str:
    """Reconstruct the full folder path for a VM."""
    path_parts = []
    parent = getattr(entity, "parent", None)
    while parent:
        if isinstance(parent, vim.Folder):
            name = parent.name
            # Skip the root "vm" folder and datacenters folder
            if name not in ("vm", "Datacenters"):
                path_parts.insert(0, name)
        elif isinstance(parent, vim.Datacenter):
            path_parts.insert(0, parent.name)
            break
        parent = getattr(parent, "parent", None)
    return "/" + "/".join(path_parts) if path_parts else ""


def _get_cluster_name(host_obj) -> str:
    """Get the cluster name for an ESXi host."""
    if host_obj is None:
        return ""
    parent = getattr(host_obj, "parent", None)
    if parent and isinstance(parent, vim.ClusterComputeResource):
        return parent.name
    return ""


def _get_datacenter(entity) -> str:
    """Find the datacenter containing this entity."""
    parent = getattr(entity, "parent", None)
    while parent:
        if isinstance(parent, vim.Datacenter):
            return parent.name
        parent = getattr(parent, "parent", None)
    return ""


def _collect_vm_info(vm: vim.VirtualMachine) -> VMInfo:
    """Extract all relevant information from a single VM object.

    Handles cases where properties may be None (e.g., powered-off VMs
    without VMware Tools reporting guest info).
    """
    config = vm.config
    summary = vm.summary
    guest = vm.guest
    runtime = vm.runtime

    if config is None:
        logger.warning(f"VM '{vm.name}' has no config — skipping detailed collection")
        return VMInfo(name=vm.name, moref=str(vm._moId))

    # ── Basic properties ──
    info = VMInfo(
        name=vm.name,
        moref=str(vm._moId),
        power_state=str(runtime.powerState) if runtime else "",
        cpu=config.hardware.numCPU,
        memory_mb=config.hardware.memoryMB,
        guest_os=config.guestId or "",
        guest_os_full=(config.guestFullName or
                       (guest.guestFullName if guest else "") or ""),
        firmware=getattr(config, "firmware", "bios") or "bios",
        uuid=config.uuid or "",
        instance_uuid=config.instanceUuid or "",
        annotation=config.annotation or "",
    )

    # ── VMware Tools ──
    if guest:
        info.tools_status = guest.toolsStatus or ""
        info.tools_version = guest.toolsVersionStatus2 or guest.toolsVersion or ""

    # ── Host / Cluster / Datacenter ──
    host_obj = runtime.host if runtime else None
    if host_obj:
        info.host = host_obj.name
        info.cluster = _get_cluster_name(host_obj)

    info.datacenter = _get_datacenter(vm)
    info.folder = _get_folder_path(vm)

    # ── Resource Pool ──
    if vm.resourcePool:
        info.resource_pool = vm.resourcePool.name

    # ── Disks ──
    total_disk_gb = 0.0
    for device in config.hardware.device:
        if isinstance(device, vim.vm.device.VirtualDisk):
            size_gb = device.capacityInKB / (1024 * 1024)
            total_disk_gb += size_gb

            # Determine datastore
            ds_name = ""
            backing = device.backing
            if hasattr(backing, "fileName") and backing.fileName:
                # fileName format: "[datastore] path/file.vmdk"
                fn = backing.fileName
                if fn.startswith("["):
                    ds_name = fn.split("]")[0].lstrip("[")

            # Determine thin provisioning
            thin = False
            if hasattr(backing, "thinProvisioned"):
                thin = backing.thinProvisioned or False

            # Determine controller type
            ctrl_type = "scsi"
            ctrl_key = device.controllerKey
            for dev2 in config.hardware.device:
                if hasattr(dev2, "key") and dev2.key == ctrl_key:
                    if isinstance(dev2, vim.vm.device.VirtualNVMEController):
                        ctrl_type = "nvme"
                    elif isinstance(dev2, vim.vm.device.VirtualIDEController):
                        ctrl_type = "ide"
                    elif isinstance(dev2, vim.vm.device.VirtualSCSIController):
                        ctrl_type = "scsi"
                    break

            info.disks.append(DiskInfo(
                name=device.deviceInfo.label if device.deviceInfo else f"disk-{device.key}",
                size_gb=round(size_gb, 2),
                thin_provisioned=thin,
                datastore=ds_name,
                file_path=getattr(backing, "fileName", ""),
                controller_type=ctrl_type,
            ))

    info.total_disk_gb = round(total_disk_gb, 2)

    # ── NICs ──
    for device in config.hardware.device:
        if isinstance(device, vim.vm.device.VirtualEthernetCard):
            network_name = ""
            backing = device.backing
            if isinstance(backing, vim.vm.device.VirtualEthernetCard.NetworkBackingInfo):
                network_name = backing.deviceName or ""
            elif isinstance(backing, vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo):
                # DVS port group — try to resolve name
                try:
                    pg_key = backing.port.portgroupKey
                    network_name = f"dvs:{pg_key}"
                except Exception:
                    network_name = "dvs:unknown"

            adapter_type = type(device).__name__
            # Clean up adapter type name
            adapter_type = adapter_type.replace("Virtual", "").replace("Card", "")
            # e.g., "Vmxnet3" → "vmxnet3", "E1000e" → "e1000e"
            adapter_type = adapter_type.lower()

            connected = False
            if device.connectable:
                connected = device.connectable.connected or False

            info.nics.append(NICInfo(
                mac_address=device.macAddress or "",
                network=network_name,
                adapter_type=adapter_type,
                connected=connected,
            ))
            if network_name:
                info.networks.append(network_name)

    # ── Snapshots ──
    if vm.snapshot and vm.snapshot.rootSnapshotList:
        def _walk_snapshots(snap_list, result):
            for s in snap_list:
                result.append(s.name)
                if s.childSnapshotList:
                    _walk_snapshots(s.childSnapshotList, result)
        snap_names = []
        _walk_snapshots(vm.snapshot.rootSnapshotList, snap_names)
        info.snapshots = snap_names

    return info


class VMInventory:
    """Collect VM inventory from vCenter.

    Usage:
        client = VSphereClient()
        client.connect(...)
        inv = VMInventory(client)
        vms = inv.list_all_vms()
    """

    def __init__(self, client: VSphereClient):
        self.client = client

    def list_all_vms(self) -> list[VMInfo]:
        """List all VMs in the connected vCenter.

        Uses ContainerView for efficient traversal of the entire inventory.
        Skips templates (config.template == True).

        Returns:
            List of VMInfo objects for all non-template VMs
        """
        logger.info("Collecting VM inventory...")
        container = self.client.get_container_view([vim.VirtualMachine])
        vms: list[VMInfo] = []

        try:
            vm_objects = list(container.view)
            logger.info(f"Found {len(vm_objects)} VM objects in vCenter")

            for vm_obj in vm_objects:
                try:
                    # Skip templates
                    if vm_obj.config and vm_obj.config.template:
                        logger.debug(f"Skipping template: {vm_obj.name}")
                        continue

                    vm_info = _collect_vm_info(vm_obj)
                    vms.append(vm_info)
                    logger.debug(
                        f"  {vm_info.name}: {vm_info.cpu}vCPU, "
                        f"{vm_info.memory_mb}MB, {vm_info.total_disk_gb:.1f}GB, "
                        f"firmware={vm_info.firmware}, os={vm_info.guest_os}"
                    )

                except Exception as e:
                    logger.warning(f"Error collecting info for VM '{vm_obj.name}': {e}")
                    # Still add with minimal info
                    vms.append(VMInfo(
                        name=vm_obj.name,
                        moref=str(vm_obj._moId),
                    ))
        finally:
            container.Destroy()

        logger.info(f"Inventory complete: {len(vms)} VMs collected")
        return vms

    def get_vm_info(self, vm_name: str) -> VMInfo:
        """Get detailed info for a specific VM by name.

        Args:
            vm_name: Exact VM name in vCenter

        Returns:
            VMInfo object

        Raises:
            ValueError: If VM not found
        """
        logger.info(f"Looking up VM: {vm_name}")
        vm_obj = self.client.find_vm_by_name(vm_name)

        if vm_obj is None:
            raise ValueError(
                f"VM '{vm_name}' not found in vCenter {self.client.host}. "
                f"Check the VM name and ensure it exists."
            )

        return _collect_vm_info(vm_obj)

    def get_vm_by_moref(self, moref: str) -> VMInfo | None:
        """Get VM info by Managed Object Reference."""
        container = self.client.get_container_view([vim.VirtualMachine])
        try:
            for vm_obj in container.view:
                if str(vm_obj._moId) == moref:
                    return _collect_vm_info(vm_obj)
        finally:
            container.Destroy()
        return None

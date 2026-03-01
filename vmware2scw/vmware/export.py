"""VMware VMDK disk export via pyVmomi HTTP NFC lease.

Exports virtual disks from vCenter/ESXi to local VMDK files using
the NFC (Network File Copy) lease mechanism.

Pipeline stage: export (stage 3)

v3 FIX: Filter NFC lease devices to only export real disks (disk=True).
Skips CD-ROM (.iso), NVRAM (.nvram), and other non-disk devices.
This ensures disk-0 is always the boot disk (first SCSI controller device).
"""

from __future__ import annotations

import logging
import os
import ssl
import time
import urllib.request
from pathlib import Path
from typing import Optional, Callable

from pyVmomi import vim

from vmware2scw.vmware.client import VSphereClient

logger = logging.getLogger(__name__)

DOWNLOAD_CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB


class VMExporter:
    """Export VM disks from VMware to local VMDK files via NFC lease."""

    def __init__(self, client: VSphereClient):
        self.client = client

    def export_vm_disks(
        self,
        vm_name: str,
        output_dir,
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
    ) -> list[Path]:
        """Export all virtual hard disks of a VM as local VMDK files.

        Only exports real disk devices (disk=True in NFC lease).
        Skips CD-ROM drives, NVRAM files, and other non-disk devices.
        Disks are exported in NFC lease order, which follows the SCSI
        controller order — so disk-0 is always the boot disk.

        Args:
            vm_name: Name of the VM in vCenter
            output_dir: Directory (str or Path) for exported VMDKs
            progress_callback: Optional callback(disk_name, bytes_downloaded, total_bytes)

        Returns:
            List of Path objects to exported VMDK files (boot disk first)
        """
        vm = self.client.find_vm_by_name(vm_name)
        if not vm:
            raise ValueError(f"VM '{vm_name}' not found in vCenter")

        dest_dir = Path(output_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"Exporting disks for VM '{vm_name}' to {dest_dir}")

        # Initiate NFC export lease
        lease = vm.ExportVm()
        self._wait_for_lease(lease)

        exported_files: list[Path] = []

        try:
            info = lease.info
            device_urls = info.deviceUrl

            if not device_urls:
                raise RuntimeError(f"No device URLs in export lease for VM '{vm_name}'")

            # ── Filter: only real disks (skip CD-ROM, NVRAM, etc.) ──
            disk_devices = []
            skipped = []
            for dev_url in device_urls:
                is_disk = getattr(dev_url, 'disk', None)
                url_str = dev_url.url or ""

                if is_disk:
                    disk_devices.append(dev_url)
                else:
                    # Log what we're skipping for transparency
                    dev_type = "CD-ROM" if ".iso" in url_str else \
                               "NVRAM" if ".nvram" in url_str else \
                               "non-disk"
                    skipped.append(f"{dev_url.key} ({dev_type})")

            if skipped:
                logger.info(f"Skipping {len(skipped)} non-disk device(s): {', '.join(skipped)}")

            if not disk_devices:
                raise RuntimeError(
                    f"No virtual disks found in NFC lease for VM '{vm_name}' "
                    f"(total devices: {len(device_urls)}, all skipped as non-disk)"
                )

            logger.info(f"Exporting {len(disk_devices)} disk(s) "
                        f"(skipped {len(skipped)} non-disk device(s))")

            # ── Export each real disk ──
            for idx, dev_url in enumerate(disk_devices):
                disk_key = dev_url.key
                url = dev_url.url

                # Replace * in URL with actual ESXi host
                if "*" in url:
                    host = self.client.host
                    url = url.replace("*", host)

                filename = f"disk-{idx}.vmdk"
                dest_path = dest_dir / filename

                logger.info(f"  Exporting {disk_key} -> {filename}")

                self._download_disk(
                    url=url,
                    dest_path=str(dest_path),
                    lease=lease,
                    disk_key=disk_key,
                    progress_callback=progress_callback,
                )

                if dest_path.exists() and dest_path.stat().st_size > 0:
                    exported_files.append(dest_path)
                    size_gb = dest_path.stat().st_size / (1024**3)
                    logger.info(f"  Exported {filename}: {size_gb:.2f} GB")
                else:
                    logger.warning(f"  Export produced empty file: {filename}")

        finally:
            try:
                lease.HttpNfcLeaseComplete()
                logger.debug("NFC lease completed")
            except Exception as e:
                logger.warning(f"Error completing lease: {e}")

        logger.info(f"Export complete: {len(exported_files)} disk(s) exported")
        return exported_files

    def _wait_for_lease(self, lease, timeout: int = 120):
        """Wait for an NFC lease to become ready."""
        start = time.time()
        while True:
            state = lease.state
            if state == vim.HttpNfcLease.State.ready:
                logger.debug("NFC lease ready")
                return
            elif state == vim.HttpNfcLease.State.error:
                error = lease.error
                raise RuntimeError(
                    f"NFC lease error: {error.msg if error else 'unknown'}"
                )
            elif time.time() - start > timeout:
                try:
                    lease.HttpNfcLeaseAbort()
                except Exception:
                    pass
                raise RuntimeError(f"NFC lease timed out after {timeout}s")
            time.sleep(1)

    def _download_disk(
        self,
        url: str,
        dest_path: str,
        lease,
        disk_key: str,
        progress_callback: Optional[Callable] = None,
    ):
        """Download a single disk via HTTPS from the NFC lease URL."""
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        cookie = self._get_session_cookie()
        headers = {}
        if cookie:
            headers["Cookie"] = cookie

        request = urllib.request.Request(url, headers=headers)

        downloaded = 0
        last_progress_time = time.time()

        with urllib.request.urlopen(request, context=ssl_context) as response:
            total_size = int(response.headers.get("Content-Length", 0))
            logger.debug(f"Download size: {total_size / (1024**3):.2f} GB")

            with open(dest_path, "wb") as f:
                while True:
                    chunk = response.read(DOWNLOAD_CHUNK_SIZE)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)

                    # Keep lease alive (progress update every 30s)
                    now = time.time()
                    if now - last_progress_time > 30:
                        try:
                            if total_size > 0:
                                pct = int(downloaded * 100 / total_size)
                            else:
                                pct = 50
                            lease.HttpNfcLeaseProgress(pct)
                        except Exception:
                            pass
                        last_progress_time = now

                    if progress_callback and total_size > 0:
                        progress_callback(disk_key, downloaded, total_size)

    def _get_session_cookie(self) -> str:
        """Extract the vmware_soap_session cookie from the current connection."""
        try:
            stub = self.client.si._stub
            cookie_str = stub.cookie
            if cookie_str:
                for part in cookie_str.split(";"):
                    part = part.strip()
                    if part.startswith("vmware_soap_session"):
                        return part
            return cookie_str or ""
        except Exception as e:
            logger.debug(f"Could not extract session cookie: {e}")
            return ""


# Backward compatibility alias
VMDKExporter = VMExporter

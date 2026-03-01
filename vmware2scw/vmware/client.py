"""VMware vSphere client wrapper.

Thin wrapper around pyVmomi for vCenter operations:
  - Connection management (SSL, retry)
  - VM lookup
  - Content property access

Requires: pyvmomi >= 8.0
"""

from __future__ import annotations

import atexit
import logging
import ssl
from typing import Optional

from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim

logger = logging.getLogger(__name__)


class VSphereClient:
    """vCenter API client.

    Usage:
        client = VSphereClient()
        client.connect("vcenter.local", "admin", "password", insecure=True)
        # use client.content / client.si for pyVmomi operations
        client.disconnect()
    """

    def __init__(self):
        self._si: Optional[vim.ServiceInstance] = None
        self._content = None
        self._host: str = ""

    def connect(self, host: str, username: str, password: str, insecure: bool = False) -> None:
        """Connect to vCenter/ESXi.

        Args:
            host: vCenter or ESXi hostname/IP
            username: Login username (e.g. administrator@vsphere.local)
            password: Login password
            insecure: If True, skip SSL certificate verification
        """
        self._host = host
        logger.info(f"Connecting to vCenter: {host} (user={username}, insecure={insecure})")

        connect_kwargs = {
            "host": host,
            "user": username,
            "pwd": password,
            "port": 443,
        }

        if insecure:
            # Disable SSL certificate verification (common with self-signed certs)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            connect_kwargs["sslContext"] = ctx

        try:
            self._si = SmartConnect(**connect_kwargs)
        except vim.fault.InvalidLogin as e:
            raise RuntimeError(f"Authentication failed for {username}@{host}: {e.msg}") from e
        except Exception as e:
            raise RuntimeError(f"Failed to connect to {host}: {e}") from e

        if not self._si:
            raise RuntimeError(f"SmartConnect returned None for {host}")

        self._content = self._si.RetrieveContent()

        # Auto-disconnect on interpreter exit
        atexit.register(self._safe_disconnect)

        about = self._content.about
        logger.info(
            f"Connected to {about.fullName} "
            f"(API {about.apiVersion}, build {about.build})"
        )

    def disconnect(self) -> None:
        """Disconnect from vCenter."""
        if self._si:
            try:
                Disconnect(self._si)
                logger.info(f"Disconnected from {self._host}")
            except Exception as e:
                logger.debug(f"Disconnect error (non-fatal): {e}")
            finally:
                self._si = None
                self._content = None

    def _safe_disconnect(self):
        """Safe disconnect for atexit — ignores errors."""
        try:
            if self._si:
                Disconnect(self._si)
        except Exception:
            pass

    @property
    def host(self) -> str:
        return self._host

    @property
    def si(self) -> vim.ServiceInstance:
        if not self._si:
            raise RuntimeError("Not connected to vCenter. Call connect() first.")
        return self._si

    @property
    def content(self):
        if not self._content:
            raise RuntimeError("Not connected to vCenter. Call connect() first.")
        return self._content

    def get_container_view(self, obj_type: list, container=None) -> vim.view.ContainerView:
        """Create a container view for efficient object traversal.

        Args:
            obj_type: List of vim types, e.g. [vim.VirtualMachine]
            container: Root container (default: rootFolder)

        Returns:
            ContainerView that must be destroyed after use
        """
        container = container or self.content.rootFolder
        return self.content.viewManager.CreateContainerView(
            container, obj_type, recursive=True
        )

    def find_vm_by_name(self, name: str) -> Optional[vim.VirtualMachine]:
        """Find a VM by exact name."""
        container = self.get_container_view([vim.VirtualMachine])
        try:
            for vm in container.view:
                if vm.name == name:
                    return vm
        finally:
            container.Destroy()
        return None

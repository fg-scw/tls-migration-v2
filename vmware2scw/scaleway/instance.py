"""Scaleway Instance + Block Storage API operations for migration.

Snapshot import uses the Block Storage API:
  POST /block/v1/zones/{zone}/snapshots/import-from-object-storage

Image creation uses the Instance API:
  POST /instance/v1/zones/{zone}/images

Pipeline stages: import_scw (stage 8/10), verify (stage 9/11)
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

SCW_API_BASE = "https://api.scaleway.com"


class ScalewayInstanceAPI:
    """Client for Scaleway Instance + Block Storage API operations.

    Usage (from migration.py):
        api = ScalewayInstanceAPI(
            access_key="...", secret_key="...", project_id="..."
        )
        snapshot = api.create_snapshot_from_s3(zone, name, bucket, key)
        api.wait_for_snapshot(zone, snapshot["id"])
        image = api.create_image(zone, name, snapshot_id, extra_snapshots=None)
    """

    def __init__(
        self,
        access_key: str = "",
        secret_key: str = "",
        project_id: str = "",
        organization_id: str = "",
    ):
        self.access_key = access_key
        self.secret_key = secret_key
        self.project_id = project_id
        self.organization_id = organization_id or project_id

        self._session = requests.Session()
        self._session.headers.update({
            "X-Auth-Token": secret_key,
            "Content-Type": "application/json",
        })

    def _instance_url(self, zone: str, path: str) -> str:
        """Instance API URL."""
        return f"{SCW_API_BASE}/instance/v1/zones/{zone}{path}"

    def _block_url(self, zone: str, path: str) -> str:
        """Block Storage API URL."""
        return f"{SCW_API_BASE}/block/v1/zones/{zone}{path}"

    def _request(self, method: str, url: str, **kwargs) -> dict:
        """Make an authenticated API request."""
        resp = self._session.request(method, url, **kwargs)

        if resp.status_code >= 400:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            raise RuntimeError(
                f"Scaleway API error {resp.status_code}: {detail}"
            )

        if resp.status_code == 204:
            return {}
        return resp.json()

    # ── Snapshot Import (Block Storage API) ──────────────────────────

    def create_snapshot_from_s3(
        self,
        zone: str,
        name: str,
        bucket: str,
        key: str,
        size: Optional[int] = None,
    ) -> dict:
        """Import a qcow2 image from S3 into a Scaleway Block Storage snapshot.

        Uses the Block Storage API endpoint:
        POST /block/v1/zones/{zone}/snapshots/import-from-object-storage

        Ref: https://www.scaleway.com/en/docs/instances/api-cli/snapshot-import-export-feature/

        Args:
            zone: Scaleway zone (e.g., "fr-par-1")
            name: Name for the snapshot
            bucket: S3 bucket name
            key: S3 object key (must end in .qcow or .qcow2)
            size: Optional volume size in bytes

        Returns:
            Snapshot dict with at least {"id": "..."}
        """
        logger.info(f"Creating snapshot '{name}' from s3://{bucket}/{key} in {zone}")

        if not key.endswith((".qcow", ".qcow2")):
            logger.warning(f"S3 key '{key}' does not end with .qcow2 — Scaleway may reject it")

        url = self._block_url(zone, "/snapshots/import-from-object-storage")

        body = {
            "bucket": bucket,
            "key": key,
            "name": name,
            "project_id": self.project_id,
        }

        if size:
            body["size"] = size

        result = self._request("POST", url, json=body)

        snapshot = result.get("snapshot", result)
        snap_id = snapshot.get("id", "unknown")
        logger.info(f"Snapshot import initiated: {snap_id}")
        return snapshot

    def wait_for_snapshot(
        self,
        zone: str,
        snapshot_id: str,
        timeout: int = 3600,
        poll_interval: int = 15,
    ) -> dict:
        """Wait for a snapshot import to complete.

        Polls the Block Storage API until snapshot status is "available".
        """
        logger.info(f"Waiting for snapshot {snapshot_id} to be available...")
        start = time.time()

        while time.time() - start < timeout:
            url = self._block_url(zone, f"/snapshots/{snapshot_id}")
            result = self._request("GET", url)
            snapshot = result.get("snapshot", result)
            status = snapshot.get("status", snapshot.get("state", ""))

            if status in ("available", "in_use"):
                logger.info(f"Snapshot {snapshot_id} is available")
                return snapshot
            elif status == "error":
                raise RuntimeError(
                    f"Snapshot import failed: {snapshot.get('error_message', snapshot)}"
                )

            elapsed = int(time.time() - start)
            logger.debug(f"  Snapshot status: {status} ({elapsed}s elapsed)")
            time.sleep(poll_interval)

        raise RuntimeError(f"Snapshot import timed out after {timeout}s")

    # ── Image Creation (Instance API) ────────────────────────────────

    def create_image(
        self,
        zone: str,
        name: str,
        root_snapshot_id: str,
        arch: str = "x86_64",
        extra_snapshots: Optional[list[str]] = None,
    ) -> dict:
        """Create a Scaleway image from snapshot(s).

        Uses the Instance API:
        POST /instance/v1/zones/{zone}/images

        Args:
            zone: Scaleway zone
            name: Image name
            root_snapshot_id: Root volume snapshot ID
            arch: Architecture (x86_64)
            extra_snapshots: Additional volume snapshot IDs

        Returns:
            Image dict with at least {"id": "..."}
        """
        logger.info(f"Creating image '{name}' from snapshot {root_snapshot_id}")

        url = self._instance_url(zone, "/images")
        body = {
            "name": name,
            "arch": arch,
            "root_volume": root_snapshot_id,
            "project": self.project_id,
        }

        if extra_snapshots:
            body["extra_volumes"] = {
                str(i + 1): {"id": snap_id}
                for i, snap_id in enumerate(extra_snapshots)
            }

        result = self._request("POST", url, json=body)
        image = result.get("image", {})
        logger.info(f"Image created: {image.get('id', 'unknown')}")
        return image

    # ── Optional Instance operations ─────────────────────────────────

    def create_server(
        self,
        zone: str,
        name: str,
        image_id: str,
        commercial_type: str = "POP2-2C-8G",
        tags: Optional[list[str]] = None,
        dynamic_ip: bool = True,
    ) -> dict:
        """Create a Scaleway Instance from an imported image."""
        logger.info(f"Creating instance '{name}' (type={commercial_type})")

        url = self._instance_url(zone, "/servers")
        body = {
            "name": name,
            "commercial_type": commercial_type,
            "image": image_id,
            "project": self.project_id,
            "dynamic_ip_required": dynamic_ip,
            "tags": tags or [],
        }

        result = self._request("POST", url, json=body)
        server = result.get("server", {})
        logger.info(f"Instance created: {server.get('id', '')}")
        return server

    # ── Resource management ──────────────────────────────────────────

    def get_snapshot(self, zone: str, snapshot_id: str) -> dict:
        url = self._block_url(zone, f"/snapshots/{snapshot_id}")
        return self._request("GET", url).get("snapshot", {})

    def get_image(self, zone: str, image_id: str) -> dict:
        url = self._instance_url(zone, f"/images/{image_id}")
        return self._request("GET", url).get("image", {})

    def delete_snapshot(self, zone: str, snapshot_id: str) -> None:
        logger.info(f"Deleting snapshot {snapshot_id}")
        url = self._block_url(zone, f"/snapshots/{snapshot_id}")
        self._request("DELETE", url)

    def delete_image(self, zone: str, image_id: str) -> None:
        logger.info(f"Deleting image {image_id}")
        url = self._instance_url(zone, f"/images/{image_id}")
        self._request("DELETE", url)


# Backward compat alias
ScalewayInstanceClient = ScalewayInstanceAPI

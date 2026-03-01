"""Scaleway Object Storage (S3) operations for migration transit.

Handles upload of converted qcow2 images to Scaleway S3 as transit
before importing as Scaleway snapshots/images.

Pipeline stage: upload_s3 (stage 7/9)
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional, Callable

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

SCW_S3_ENDPOINT = "https://s3.{region}.scw.cloud"

DEFAULT_MULTIPART_THRESHOLD = 64 * 1024 * 1024   # 64 MB
DEFAULT_MULTIPART_CHUNKSIZE = 64 * 1024 * 1024    # 64 MB
DEFAULT_MAX_CONCURRENCY = 10


class ScalewayS3:
    """Scaleway Object Storage client for migration transit.

    Usage (from migration.py):
        s3 = ScalewayS3(region="fr-par", access_key="...", secret_key="...")
        s3.create_bucket_if_not_exists(bucket)
        s3.upload_image(local_path, bucket, key)
    """

    def __init__(
        self,
        region: str = "fr-par",
        access_key: str = "",
        secret_key: str = "",
        endpoint_url: Optional[str] = None,
    ):
        self.region = region
        self.endpoint_url = endpoint_url or SCW_S3_ENDPOINT.format(region=region)

        self._client = boto3.client(
            "s3",
            region_name=region,
            endpoint_url=self.endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=BotoConfig(
                max_pool_connections=DEFAULT_MAX_CONCURRENCY,
                retries={"max_attempts": 3, "mode": "adaptive"},
            ),
        )

        self._transfer_config = boto3.s3.transfer.TransferConfig(
            multipart_threshold=DEFAULT_MULTIPART_THRESHOLD,
            multipart_chunksize=DEFAULT_MULTIPART_CHUNKSIZE,
            max_concurrency=DEFAULT_MAX_CONCURRENCY,
        )

    def create_bucket_if_not_exists(self, bucket: str) -> None:
        """Create the S3 bucket if it doesn't exist."""
        try:
            self._client.head_bucket(Bucket=bucket)
            logger.debug(f"Bucket '{bucket}' exists")
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code in ("404", "NoSuchBucket"):
                logger.info(f"Creating bucket '{bucket}' in {self.region}")
                self._client.create_bucket(Bucket=bucket)
            else:
                raise

    def upload_image(
        self,
        local_path: str,
        bucket: str,
        key: str,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        """Upload a qcow2 image to S3.

        Args:
            local_path: Path to the local qcow2 file
            bucket: S3 bucket name
            key: S3 object key
            progress_callback: Optional callback(bytes_uploaded, total_bytes)
        """
        path = Path(local_path)
        if not path.exists():
            raise FileNotFoundError(f"Image not found: {local_path}")

        file_size = path.stat().st_size
        logger.info(
            f"Uploading {path.name} ({file_size / (1024**3):.2f} GB) "
            f"to s3://{bucket}/{key}"
        )

        callback = None
        if progress_callback:
            uploaded = {"bytes": 0}
            lock = threading.Lock()

            def _progress(bytes_transferred):
                with lock:
                    uploaded["bytes"] += bytes_transferred
                    progress_callback(uploaded["bytes"], file_size)

            callback = _progress

        start = time.time()

        self._client.upload_file(
            Filename=str(path),
            Bucket=bucket,
            Key=key,
            Callback=callback,
            Config=self._transfer_config,
        )

        elapsed = time.time() - start
        speed_mbps = (file_size / (1024**2)) / elapsed if elapsed > 0 else 0
        logger.info(f"Upload complete in {elapsed:.0f}s ({speed_mbps:.1f} MB/s)")

    def check_object_exists(self, bucket: str, key: str) -> bool:
        """Check if an object exists in the bucket."""
        try:
            self._client.head_object(Bucket=bucket, Key=key)
            return True
        except ClientError:
            return False

    def get_object_size(self, bucket: str, key: str) -> int:
        """Get the size in bytes of an S3 object."""
        try:
            resp = self._client.head_object(Bucket=bucket, Key=key)
            return resp.get("ContentLength", 0)
        except ClientError:
            return 0

    def delete_object(self, bucket: str, key: str) -> None:
        """Delete an object from S3."""
        logger.debug(f"Deleting s3://{bucket}/{key}")
        self._client.delete_object(Bucket=bucket, Key=key)

    def list_objects(self, bucket: str, prefix: str = "") -> list[dict]:
        """List objects in the bucket with optional prefix filter."""
        objects = []
        paginator = self._client.get_paginator("list_objects_v2")

        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                objects.append({
                    "key": obj["Key"],
                    "size": obj["Size"],
                    "last_modified": obj["LastModified"],
                })

        return objects


# Backward compat alias
S3TransitStorage = ScalewayS3

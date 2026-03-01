"""Application configuration for vmware2scw.

Field names are aligned with pipeline/migration.py usage:
  config.scaleway.s3_region   (not config.s3.region)
  config.scaleway.s3_bucket   (not config.s3.bucket)
  config.scaleway.access_key  (str, not SecretStr)
  config.conversion.virtio_win_iso
  config.conversion.compress_qcow2
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, SecretStr


class VMwareConfig(BaseModel):
    vcenter: str = Field("", description="vCenter hostname or IP")
    username: str = Field("", description="vCenter username")
    password: Optional[SecretStr] = Field(None, description="vCenter password")
    insecure: bool = Field(False, description="Skip SSL verification")
    datacenter: str = Field("", description="Default datacenter name")


class ScalewayConfig(BaseModel):
    """Scaleway API + S3 settings (flat — accessed as config.scaleway.*)."""
    access_key: Optional[str] = Field(None)
    secret_key: Optional[SecretStr] = Field(None)
    organization_id: str = Field("")
    project_id: str = Field("")
    default_zone: str = Field("fr-par-1")
    region: str = Field("fr-par")
    s3_region: str = Field("fr-par")
    s3_bucket: str = Field("vmware2scw-transit")
    s3_endpoint: str = Field("https://s3.fr-par.scw.cloud")


class ConversionConfig(BaseModel):
    work_dir: Path = Field(Path("/var/lib/vmware2scw/work"))
    virtio_win_iso: str = Field("")
    ovmf_path: str = Field("/usr/share/OVMF/OVMF_CODE.fd")
    compress_qcow2: bool = Field(True)
    keep_intermediates: bool = Field(False)
    qemu_img_path: str = Field("qemu-img")
    virt_customize_path: str = Field("virt-customize")


class AppConfig(BaseModel):
    vmware: VMwareConfig = Field(default_factory=VMwareConfig)
    scaleway: ScalewayConfig = Field(default_factory=ScalewayConfig)
    conversion: ConversionConfig = Field(default_factory=ConversionConfig)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "AppConfig":
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        # Resolve *_env fields → os.getenv()
        for section_name, section_data in data.items():
            if not isinstance(section_data, dict):
                continue
            env_keys = [k for k in section_data if k.endswith("_env")]
            for env_key in env_keys:
                real_key = env_key[:-4]  # "access_key_env" → "access_key"
                env_var = section_data[env_key]
                if env_var and not section_data.get(real_key):
                    val = os.getenv(env_var)
                    if val:
                        section_data[real_key] = val
                del section_data[env_key]  # Not in Pydantic model
        return cls(**data)

    #@classmethod
    #def from_yaml(cls, path: str | Path) -> "AppConfig":
    #    with open(path) as f:
    #        data = yaml.safe_load(f) or {}
    #    return cls(**data)

    @classmethod
    def from_env_and_args(cls, **overrides) -> "AppConfig":
        return cls(
            vmware=VMwareConfig(
                vcenter=os.getenv("VMWARE_VCENTER", ""),
                username=os.getenv("VMWARE_USERNAME", ""),
                password=SecretStr(os.getenv("VMWARE_PASSWORD", "")) if os.getenv("VMWARE_PASSWORD") else None,
                insecure=os.getenv("VMWARE_INSECURE", "false").lower() == "true",
            ),
            scaleway=ScalewayConfig(
                access_key=os.getenv("SCW_ACCESS_KEY"),
                secret_key=SecretStr(os.getenv("SCW_SECRET_KEY", "")) if os.getenv("SCW_SECRET_KEY") else None,
                organization_id=os.getenv("SCW_ORGANIZATION_ID", ""),
                project_id=os.getenv("SCW_PROJECT_ID", ""),
                default_zone=os.getenv("SCW_DEFAULT_ZONE", "fr-par-1"),
                s3_bucket=os.getenv("SCW_S3_BUCKET", "vmware2scw-transit"),
                s3_region=os.getenv("SCW_S3_REGION", "fr-par"),
            ),
        )

    def to_yaml(self, path: str | Path) -> None:
        data = self.model_dump(mode="json")
        for section in data.values():
            if isinstance(section, dict):
                for key, val in section.items():
                    if ("password" in key or "secret" in key) and val:
                        section[key] = "***REDACTED***"
        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)


class VMMigrationPlan(BaseModel):
    vm_name: str
    target_type: str
    zone: str = "fr-par-1"
    tags: list[str] = Field(default_factory=list)
    skip_validation: bool = False
    network_mapping: dict[str, str] = Field(default_factory=dict)

"""Migration state persistence for single-VM and batch operations.

Tracks the progress of each VM migration through pipeline stages,
enabling resume after failure. State is persisted as JSON files.

Used by:
  - batch_orchestrator.py: Creates MigrationState per VM job
  - migration.py: Updates state after each stage
  - cli.py: Resume command loads state
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


@dataclass
class MigrationState:
    """Tracks progress of a single VM migration through the pipeline.

    Attributes:
        migration_id: Unique identifier for this migration run
        vm_name: Source VM name in vCenter
        target_type: Scaleway instance type
        zone: Target availability zone
        current_stage: Currently executing stage
        completed_stages: List of successfully completed stages
        artifacts: Key-value store for inter-stage data
    """
    migration_id: str
    vm_name: str
    target_type: str = ""
    zone: str = "fr-par-1"
    current_stage: str = ""
    completed_stages: list[str] = field(default_factory=list)
    artifacts: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    started_at: Any = None      # datetime or str — flexible for both pipeline and batch
    completed_at: Any = None

    def mark_stage_complete(self, stage: str) -> None:
        """Record a stage as completed."""
        if stage not in self.completed_stages:
            self.completed_stages.append(stage)

    def set_artifact(self, key: str, value: Any) -> None:
        """Store an artifact (e.g., S3 key, snapshot ID, image ID)."""
        self.artifacts[key] = value

    def get_artifact(self, key: str, default: Any = None) -> Any:
        """Retrieve a stored artifact."""
        return self.artifacts.get(key, default)

    def is_stage_complete(self, stage: str) -> bool:
        return stage in self.completed_stages

    def to_dict(self) -> dict:
        return {
            "migration_id": self.migration_id,
            "vm_name": self.vm_name,
            "target_type": self.target_type,
            "zone": self.zone,
            "current_stage": self.current_stage,
            "completed_stages": self.completed_stages,
            "artifacts": self.artifacts,
            "error": self.error,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


class MigrationStateStore:
    """Persistent store for migration states.

    Saves/loads migration state as JSON files in the work directory.
    Each migration gets its own state file: {work_dir}/state/{migration_id}.json
    """

    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, migration_id: str) -> Path:
        return self.state_dir / f"{migration_id}.json"

    def save(self, state: MigrationState) -> None:
        """Persist migration state to disk."""
        path = self._path(state.migration_id)
        with open(path, "w") as f:
            json.dump(state.to_dict(), f, indent=2, default=str)

    def load(self, migration_id: str) -> MigrationState | None:
        """Load migration state from disk."""
        path = self._path(migration_id)
        if not path.exists():
            return None
        with open(path) as f:
            data = json.load(f)
        return MigrationState(**data)

    def list_states(self) -> list[MigrationState]:
        """List all persisted migration states."""
        states = []
        for path in self.state_dir.glob("*.json"):
            try:
                with open(path) as f:
                    data = json.load(f)
                states.append(MigrationState(**data))
            except Exception:
                continue
        return states

    def delete(self, migration_id: str) -> None:
        """Remove a migration state file."""
        path = self._path(migration_id)
        if path.exists():
            path.unlink()

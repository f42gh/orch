from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, UTC
from enum import StrEnum
from pathlib import Path


class Risk(StrEnum):
    READ_ONLY = "read_only"
    NORMAL = "normal"
    HIGH = "high"


class Priority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    BLOCKED = "blocked"
    FAILED = "failed"
    SUCCEEDED = "succeeded"
    NEEDS_REVIEW = "needs_review"


@dataclass(slots=True)
class Task:
    id: str
    repo_path: Path
    task: str
    risk: Risk
    priority: Priority
    status: TaskStatus
    created_at: datetime
    updated_at: datetime
    workspace_path: Path | None = None
    branch_name: str | None = None
    session_id: str | None = None
    result_summary: str | None = None
    error: str | None = None


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)

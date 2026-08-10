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


TERMINAL_STATUSES = frozenset(
    {TaskStatus.FAILED, TaskStatus.SUCCEEDED, TaskStatus.NEEDS_REVIEW, TaskStatus.BLOCKED}
)


class Engine(StrEnum):
    CLAUDE = "claude"
    CODEX = "codex"
    GROK = "grok"
    ANTIGRAVITY = "antigravity"


class TaskKind(StrEnum):
    """What the task is for. The router maps this to an engine."""

    IMPLEMENT = "implement"
    REFACTOR = "refactor"
    TEST = "test"
    REVIEW = "review"
    INVESTIGATE = "investigate"
    UI_VERIFY = "ui_verify"


#: Kinds that are expected to modify the workspace. Anything else runs read-only.
WRITING_KINDS = frozenset({TaskKind.IMPLEMENT, TaskKind.REFACTOR, TaskKind.TEST, TaskKind.UI_VERIFY})


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
    kind: TaskKind = TaskKind.IMPLEMENT
    engine: Engine | None = None
    workspace_path: Path | None = None
    branch_name: str | None = None
    session_id: str | None = None
    engine_session_id: str | None = None
    parent_id: str | None = None
    base_ref: str | None = None
    cost_usd: float | None = None
    exit_code: int | None = None
    result_summary: str | None = None
    error: str | None = None


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)

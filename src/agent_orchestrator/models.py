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


class WorkflowType(StrEnum):
    RUN = "run"
    BATCH = "batch"


class WorkflowStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    SEALED = "sealed"


class FallbackMode(StrEnum):
    AUTO = "auto"
    MANUAL = "manual"


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


@dataclass(frozen=True, slots=True)
class WorkflowRouteOverride:
    """A caller-supplied primary and its fallback behaviour for one task kind."""

    primary: Engine
    fallback_mode: FallbackMode = FallbackMode.AUTO
    fallbacks: tuple[Engine, ...] = ()

    def __post_init__(self) -> None:
        if self.fallback_mode is FallbackMode.AUTO and self.fallbacks:
            raise ValueError("auto workflow fallbacks are derived from the routing table")
        if self.fallback_mode is FallbackMode.MANUAL:
            _validate_manual_fallbacks(self.primary, self.fallbacks)


@dataclass(frozen=True, slots=True)
class WorkflowRoute:
    """The immutable routing snapshot persisted with a workflow."""

    kind: TaskKind
    primary: Engine
    fallback_mode: FallbackMode
    fallbacks: tuple[Engine, ...]

    def __post_init__(self) -> None:
        if len(set(self.fallbacks)) != len(self.fallbacks):
            raise ValueError("workflow route fallbacks must be unique")
        if self.primary in self.fallbacks:
            raise ValueError("workflow route fallbacks must exclude the primary engine")
        if self.fallback_mode is FallbackMode.MANUAL:
            _validate_manual_fallbacks(self.primary, self.fallbacks)


@dataclass(frozen=True, slots=True)
class Workflow:
    id: str
    workflow_type: WorkflowType
    repo_path: Path
    status: WorkflowStatus
    created_at: datetime
    closed_at: datetime | None = None

    @property
    def is_open(self) -> bool:
        return self.status is WorkflowStatus.OPEN

    @property
    def is_sealed(self) -> bool:
        return self.status is WorkflowStatus.SEALED


@dataclass(frozen=True, slots=True)
class WorkflowTask:
    workflow_id: str
    task_id: str
    ordinal: int


@dataclass(frozen=True, slots=True)
class WorkflowTaskRequest:
    task: str
    kind: TaskKind = TaskKind.IMPLEMENT
    risk: Risk = Risk.NORMAL
    priority: Priority = Priority.NORMAL
    parent_id: str | None = None
    base_ref: str | None = None


@dataclass(frozen=True, slots=True)
class PreparedWorkflowTask:
    """A request whose route was successfully resolved before persistence."""

    request: WorkflowTaskRequest
    engine: Engine


@dataclass(frozen=True, slots=True)
class WorkflowDetails:
    workflow: Workflow
    routes: tuple[WorkflowRoute, ...]
    memberships: tuple[WorkflowTask, ...]
    tasks: tuple[Task, ...]

    @property
    def id(self) -> str:
        return self.workflow.id

    @property
    def status(self) -> WorkflowStatus:
        return self.workflow.status

    @property
    def workflow_type(self) -> WorkflowType:
        return self.workflow.workflow_type


def _validate_manual_fallbacks(primary: Engine, fallbacks: tuple[Engine, ...]) -> None:
    if not fallbacks:
        raise ValueError("manual workflow fallbacks must be nonempty")
    if len(set(fallbacks)) != len(fallbacks):
        raise ValueError("manual workflow fallbacks must be unique")
    if primary in fallbacks:
        raise ValueError("manual workflow fallbacks must exclude the primary engine")


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)

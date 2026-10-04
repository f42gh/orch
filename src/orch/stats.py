from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypeVar

from orch.models import Engine, TERMINAL_STATUSES, Task, TaskStatus, TokenUsage


_Number = TypeVar("_Number", int, float)
_GROUP_BY_VALUES = frozenset({"engine", "kind", "model", "status", "repo"})


@dataclass(frozen=True, slots=True)
class Totals:
    tasks: int
    by_status: Mapping[str, int]
    terminal: int
    in_flight: int
    completed: int
    failed: int
    cancelled: int
    success_rate: float | None
    cost_usd: float
    cost_reported_tasks: int
    cost_unreported_tasks: int
    engines_without_cost: tuple[str, ...]
    tokens: TokenUsage
    tokens_reported_tasks: int
    engine_ms_total: int
    engine_ms_p50: int | None
    engine_ms_p95: int | None
    queue_wait_s_p50: float | None
    files_changed: int
    insertions: int
    deletions: int

    def describe(self) -> dict[str, Any]:
        return {
            "tasks": self.tasks,
            "by_status": dict(self.by_status),
            "terminal": self.terminal,
            "in_flight": self.in_flight,
            "completed": self.completed,
            "failed": self.failed,
            "cancelled": self.cancelled,
            "success_rate": self.success_rate,
            "cost_usd": self.cost_usd,
            "cost_reported_tasks": self.cost_reported_tasks,
            "cost_unreported_tasks": self.cost_unreported_tasks,
            "engines_without_cost": list(self.engines_without_cost),
            "tokens": {
                "input_tokens": self.tokens.input_tokens,
                "output_tokens": self.tokens.output_tokens,
                "cache_read_tokens": self.tokens.cache_read_tokens,
                "cache_write_tokens": self.tokens.cache_write_tokens,
                "reasoning_tokens": self.tokens.reasoning_tokens,
                "total": self.tokens.total,
            },
            "tokens_reported_tasks": self.tokens_reported_tasks,
            "engine_ms_total": self.engine_ms_total,
            "engine_ms_p50": self.engine_ms_p50,
            "engine_ms_p95": self.engine_ms_p95,
            "queue_wait_s_p50": self.queue_wait_s_p50,
            "files_changed": self.files_changed,
            "insertions": self.insertions,
            "deletions": self.deletions,
        }


@dataclass(frozen=True, slots=True)
class QuotaSnapshot:
    engine: Engine
    plan_type: str | None
    used_pct: float
    window_minutes: int | None
    resets_at: datetime | None

    def describe(self) -> dict[str, Any]:
        return {
            "engine": self.engine.value,
            "plan_type": self.plan_type,
            "used_pct": self.used_pct,
            "window_minutes": self.window_minutes,
            "resets_at": self.resets_at.isoformat() if self.resets_at is not None else None,
        }


@dataclass(frozen=True, slots=True)
class Stats:
    totals: Totals
    group_by: str | None
    groups: Mapping[str, Totals]
    quota_snapshots: Mapping[str, QuotaSnapshot]

    def describe(self) -> dict[str, Any]:
        return {
            "totals": self.totals.describe(),
            "group_by": self.group_by,
            "groups": {key: totals.describe() for key, totals in self.groups.items()},
            "quota_snapshots": {
                key: snapshot.describe()
                for key, snapshot in self.quota_snapshots.items()
            },
        }


def _nearest_rank(values: Iterable[_Number], percentile: float) -> _Number | None:
    ordered = sorted(values)
    if not ordered:
        return None
    if not 0 < percentile <= 1:
        raise ValueError("percentile must be greater than 0 and at most 1")
    rank = math.ceil(percentile * len(ordered))
    return ordered[rank - 1]


def summarize(tasks: Iterable[Task]) -> Totals:
    items = tuple(tasks)
    by_status = Counter(task.status.value for task in items)

    # Clean workers stop at needs_review, so it is a successful completion alongside
    # the legacy succeeded status rather than an incomplete outcome.
    completed = by_status[TaskStatus.NEEDS_REVIEW.value] + by_status[TaskStatus.SUCCEEDED.value]
    failed = by_status[TaskStatus.FAILED.value]
    cancelled = by_status[TaskStatus.BLOCKED.value]
    in_flight = by_status[TaskStatus.QUEUED.value] + by_status[TaskStatus.RUNNING.value]
    terminal = completed + failed + cancelled

    terminal_tasks = [
        task for task in items if task.status in TERMINAL_STATUSES
    ]
    cost_reported_tasks = sum(task.cost_usd is not None for task in terminal_tasks)
    cost_unreported = [task for task in terminal_tasks if task.cost_usd is None]
    engines_without_cost = tuple(
        sorted({task.engine.value for task in cost_unreported if task.engine is not None})
    )

    token_values = [task.tokens for task in items if task.tokens is not None]
    engine_ms = [task.engine_ms for task in items if task.engine_ms is not None]
    queue_wait_s = [
        (task.started_at - task.created_at).total_seconds()
        for task in items
        if task.started_at is not None
    ]

    # Quota is an account-wide snapshot shared by overlapping tasks, so summing it
    # into Totals would multiply one window by the number of recorded runs.
    return Totals(
        tasks=len(items),
        by_status=dict(by_status),
        terminal=terminal,
        in_flight=in_flight,
        completed=completed,
        failed=failed,
        cancelled=cancelled,
        success_rate=completed / terminal if terminal else None,
        cost_usd=sum(task.cost_usd for task in items if task.cost_usd is not None),
        cost_reported_tasks=cost_reported_tasks,
        cost_unreported_tasks=len(cost_unreported),
        engines_without_cost=engines_without_cost,
        tokens=sum(token_values, TokenUsage()),
        tokens_reported_tasks=len(token_values),
        engine_ms_total=sum(engine_ms),
        engine_ms_p50=_nearest_rank(engine_ms, 0.5),
        engine_ms_p95=_nearest_rank(engine_ms, 0.95),
        queue_wait_s_p50=_nearest_rank(queue_wait_s, 0.5),
        files_changed=sum(task.files_changed or 0 for task in items),
        insertions=sum(task.insertions or 0 for task in items),
        deletions=sum(task.deletions or 0 for task in items),
    )


def build_stats(tasks: Iterable[Task], group_by: str | None = None) -> Stats:
    if group_by is not None and group_by not in _GROUP_BY_VALUES:
        allowed = ", ".join(sorted(_GROUP_BY_VALUES))
        raise ValueError(f"group_by {group_by!r} is not one of: {allowed}")

    items = tuple(tasks)
    quota_snapshots = _quota_snapshots(items)
    if group_by is None:
        return Stats(
            totals=summarize(items),
            group_by=None,
            groups={},
            quota_snapshots=quota_snapshots,
        )

    grouped: dict[str, list[Task]] = {}
    for task in items:
        if group_by == "engine":
            key = task.engine.value if task.engine is not None else "-"
        elif group_by == "model":
            key = task.model if task.model is not None else "-"
        elif group_by == "kind":
            key = task.kind.value
        elif group_by == "status":
            key = task.status.value
        else:
            key = str(task.repo_path)
        grouped.setdefault(key, []).append(task)

    ordered_groups = {
        key: summarize(group)
        for key, group in sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0]))
    }
    return Stats(
        totals=summarize(items),
        group_by=group_by,
        groups=ordered_groups,
        quota_snapshots=quota_snapshots,
    )


def _quota_snapshots(tasks: Iterable[Task]) -> Mapping[str, QuotaSnapshot]:
    latest: dict[Engine, Task] = {}
    for task in tasks:
        if task.engine is None or task.quota_used_pct is None:
            continue
        current = latest.get(task.engine)
        if current is None or _finished_at(task) > _finished_at(current):
            latest[task.engine] = task

    return {
        engine.value: QuotaSnapshot(
            engine=engine,
            plan_type=task.plan_type,
            used_pct=task.quota_used_pct,
            window_minutes=task.quota_window_minutes,
            resets_at=task.quota_resets_at,
        )
        for engine, task in sorted(latest.items(), key=lambda item: item[0].value)
    }


def _finished_at(task: Task) -> datetime:
    value = task.finished_at
    if value is None:
        return datetime.min.replace(tzinfo=UTC)
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC)

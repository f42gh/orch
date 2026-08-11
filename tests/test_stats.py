from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_orchestrator.models import (
    Engine,
    Priority,
    Risk,
    Task,
    TaskKind,
    TaskStatus,
    TokenUsage,
)
from agent_orchestrator.stats import _nearest_rank, build_stats, summarize


CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)


def _task(
    task_id: str,
    status: TaskStatus,
    *,
    engine: Engine | None = Engine.CODEX,
    model: str | None = None,
    kind: TaskKind = TaskKind.IMPLEMENT,
    repo: str = "/repo",
    cost_usd: float | None = None,
    tokens: TokenUsage | None = None,
    engine_ms: int | None = None,
    queue_wait_s: float | None = None,
    finished_at: datetime | None = None,
    plan_type: str | None = None,
    quota_used_pct: float | None = None,
    quota_window_minutes: int | None = None,
    quota_resets_at: datetime | None = None,
) -> Task:
    return Task(
        id=task_id,
        repo_path=Path(repo),
        task="work",
        risk=Risk.NORMAL,
        priority=Priority.NORMAL,
        status=status,
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
        kind=kind,
        engine=engine,
        model=model,
        cost_usd=cost_usd,
        tokens=tokens,
        engine_ms=engine_ms,
        started_at=(
            CREATED_AT + timedelta(seconds=queue_wait_s)
            if queue_wait_s is not None
            else None
        ),
        finished_at=finished_at,
        plan_type=plan_type,
        quota_used_pct=quota_used_pct,
        quota_window_minutes=quota_window_minutes,
        quota_resets_at=quota_resets_at,
    )


def test_needs_review_is_a_successful_completion() -> None:
    totals = summarize(
        [
            _task("task-1", TaskStatus.NEEDS_REVIEW),
            _task("task-2", TaskStatus.NEEDS_REVIEW),
        ]
    )

    assert totals.completed == 2
    assert totals.terminal == 2
    assert totals.success_rate == 1.0


def test_no_terminal_tasks_have_no_success_rate() -> None:
    totals = summarize(
        [
            _task("task-1", TaskStatus.QUEUED),
            _task("task-2", TaskStatus.RUNNING),
        ]
    )

    assert totals.terminal == 0
    assert totals.in_flight == 2
    assert totals.success_rate is None


def test_in_flight_tasks_stay_out_of_success_rate_denominator() -> None:
    totals = summarize(
        [
            _task("task-1", TaskStatus.NEEDS_REVIEW),
            _task("task-2", TaskStatus.FAILED),
            _task("task-3", TaskStatus.QUEUED),
            _task("task-4", TaskStatus.RUNNING),
        ]
    )

    assert totals.terminal == 2
    assert totals.in_flight == 2
    assert totals.success_rate == 0.5


def test_null_cost_is_reported_as_missing_without_changing_total() -> None:
    totals = summarize(
        [
            _task(
                "task-1",
                TaskStatus.NEEDS_REVIEW,
                engine=Engine.GROK,
                cost_usd=1.25,
            ),
            _task("task-2", TaskStatus.FAILED, engine=Engine.CODEX),
            _task("task-3", TaskStatus.RUNNING, engine=Engine.ANTIGRAVITY),
        ]
    )

    assert totals.cost_usd == 1.25
    assert totals.cost_reported_tasks == 1
    assert totals.cost_unreported_tasks == 1
    assert totals.engines_without_cost == ("codex",)


def test_missing_tokens_do_not_contribute_or_count_as_reported() -> None:
    totals = summarize(
        [
            _task(
                "task-1",
                TaskStatus.NEEDS_REVIEW,
                tokens=TokenUsage(input_tokens=10, output_tokens=4),
            ),
            _task("task-2", TaskStatus.FAILED, tokens=None),
        ]
    )

    assert totals.tokens == TokenUsage(input_tokens=10, output_tokens=4)
    assert totals.tokens_reported_tasks == 1


def test_nearest_rank_percentiles_cover_short_and_long_series() -> None:
    assert _nearest_rank([7], 0.5) == 7
    assert _nearest_rank([20, 10], 0.5) == 10
    assert _nearest_rank([20, 10], 0.95) == 20
    assert _nearest_rank([5, 1, 4, 2, 3], 0.5) == 3
    assert _nearest_rank([6, 1, 5, 2, 4, 3], 0.5) == 3
    assert _nearest_rank([1, 2, 3, 4, 5, 6], 0.95) == 6
    assert _nearest_rank([], 0.5) is None


def test_summarize_uses_non_null_duration_and_queue_wait_values() -> None:
    totals = summarize(
        [
            _task(
                "task-1",
                TaskStatus.NEEDS_REVIEW,
                engine_ms=2_000,
                queue_wait_s=4.5,
            ),
            _task("task-2", TaskStatus.FAILED, engine_ms=1_000, queue_wait_s=2.5),
            _task("task-3", TaskStatus.QUEUED),
        ]
    )

    assert totals.engine_ms_total == 3_000
    assert totals.engine_ms_p50 == 1_000
    assert totals.engine_ms_p95 == 2_000
    assert totals.queue_wait_s_p50 == 2.5


def test_group_by_partitions_and_orders_groups() -> None:
    stats = build_stats(
        [
            _task("task-1", TaskStatus.NEEDS_REVIEW, engine=Engine.CODEX),
            _task("task-2", TaskStatus.FAILED, engine=Engine.CODEX),
            _task("task-3", TaskStatus.RUNNING, engine=None),
            _task("task-4", TaskStatus.QUEUED, engine=Engine.GROK),
        ],
        group_by="engine",
    )

    assert stats.totals.tasks == 4
    assert list(stats.groups) == ["codex", "-", "grok"]
    assert stats.groups["codex"].tasks == 2
    assert stats.groups["-"].by_status == {"running": 1}


def test_group_by_model_partitions_unknown_models_under_dash() -> None:
    stats = build_stats(
        [
            _task("task-1", TaskStatus.NEEDS_REVIEW, model="gpt-5.6-sol"),
            _task("task-2", TaskStatus.FAILED, model="gpt-5.6-sol"),
            _task("task-3", TaskStatus.RUNNING, model=None),
        ],
        group_by="model",
    )

    assert list(stats.groups) == ["gpt-5.6-sol", "-"]
    assert stats.groups["gpt-5.6-sol"].tasks == 2
    assert stats.groups["-"].tasks == 1


def test_quota_snapshots_take_the_latest_task_per_engine_without_summing() -> None:
    reset = datetime(2026, 8, 18, 0, 47, 55, tzinfo=UTC)
    stats = build_stats(
        [
            _task(
                "task-1",
                TaskStatus.NEEDS_REVIEW,
                finished_at=CREATED_AT + timedelta(hours=1),
                plan_type="plus",
                quota_used_pct=3.0,
                quota_window_minutes=10080,
                quota_resets_at=reset,
            ),
            _task(
                "task-2",
                TaskStatus.NEEDS_REVIEW,
                finished_at=CREATED_AT + timedelta(hours=2),
                plan_type="plus",
                quota_used_pct=4.0,
                quota_window_minutes=10080,
                quota_resets_at=reset,
            ),
            _task(
                "task-3",
                TaskStatus.NEEDS_REVIEW,
                engine=Engine.GROK,
                finished_at=CREATED_AT + timedelta(hours=1),
                plan_type="team",
                quota_used_pct=8.0,
                quota_window_minutes=300,
            ),
        ]
    )

    assert stats.quota_snapshots["codex"].used_pct == 4.0
    assert stats.quota_snapshots["codex"].resets_at == reset
    assert stats.quota_snapshots["grok"].used_pct == 8.0
    assert not hasattr(stats.totals, "quota_used_pct")
    assert stats.describe()["quota_snapshots"]["codex"]["used_pct"] == 4.0


def test_unknown_group_by_is_rejected() -> None:
    with pytest.raises(ValueError, match="group_by 'priority'"):
        build_stats([], group_by="priority")

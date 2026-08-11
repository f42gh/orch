from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

import agent_orchestrator.db as db_module
from agent_orchestrator.config import Config
from agent_orchestrator.db import TaskStore
from agent_orchestrator.models import (
    Engine,
    FallbackMode,
    PreparedWorkflowTask,
    Priority,
    Risk,
    TaskKind,
    TaskStatus,
    TokenUsage,
    WorkflowRoute,
    WorkflowTaskRequest,
)


def test_add_update_and_list_task(tmp_path: Path) -> None:
    store = TaskStore(Config(runtime_root=tmp_path))

    task = store.add_task(tmp_path, "READMEを更新して", Risk.NORMAL, Priority.HIGH)

    assert task.id == "task-0001"
    assert task.status == TaskStatus.QUEUED
    assert task.risk == Risk.NORMAL

    store.set_status(task.id, TaskStatus.RUNNING)
    updated = store.get_task(task.id)
    assert updated is not None
    assert updated.status == TaskStatus.RUNNING

    tasks = store.list_tasks()
    assert [item.id for item in tasks] == ["task-0001"]


def test_next_queued_task_uses_priority(tmp_path: Path) -> None:
    store = TaskStore(Config(runtime_root=tmp_path))

    low = store.add_task(tmp_path, "low", Risk.NORMAL, Priority.LOW)
    high = store.add_task(tmp_path, "high", Risk.NORMAL, Priority.HIGH)

    next_task = store.next_queued_task()
    assert next_task is not None
    assert next_task.id == high.id
    assert low.id == "task-0001"


def test_token_usage_total_and_aggregation() -> None:
    usage = TokenUsage(
        input_tokens=10,
        output_tokens=20,
        cache_read_tokens=30,
        cache_write_tokens=40,
        reasoning_tokens=5,
    )

    assert usage.total == 100
    assert sum((usage, TokenUsage(output_tokens=2)), TokenUsage()) == TokenUsage(
        input_tokens=10,
        output_tokens=22,
        cache_read_tokens=30,
        cache_write_tokens=40,
        reasoning_tokens=5,
    )
    assert usage.__add__(1) is NotImplemented


def test_claim_stamps_started_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    timestamps = iter(
        (
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:01+00:00",
            "2026-01-01T00:00:02+00:00",
            "2026-01-01T00:00:03+00:00",
        )
    )
    monkeypatch.setattr(db_module, "utc_now_iso", lambda: next(timestamps))
    store = TaskStore(Config(runtime_root=tmp_path))
    task = store.add_task(tmp_path, "work")

    first_claim = store.claim_task(task.id)
    assert first_claim is not None
    assert first_claim.started_at == datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC)

    store.set_status(task.id, TaskStatus.QUEUED)
    second_claim = store.claim_task(task.id)
    assert second_claim is not None
    assert second_claim.started_at == first_claim.started_at


def test_terminal_status_automatically_stamps_finished_at(tmp_path: Path) -> None:
    store = TaskStore(Config(runtime_root=tmp_path))
    direct = store.add_task(tmp_path, "direct")
    via_set_status = store.add_task(tmp_path, "set status")

    store.update_task(direct.id, status=TaskStatus.RUNNING)
    running = store.get_task(direct.id)
    assert running is not None
    assert running.finished_at is None

    store.update_task(direct.id, status=TaskStatus.SUCCEEDED)
    direct_finished = store.get_task(direct.id)
    assert direct_finished is not None
    assert direct_finished.finished_at == direct_finished.updated_at

    store.set_status(via_set_status.id, TaskStatus.FAILED)
    set_status_finished = store.get_task(via_set_status.id)
    assert set_status_finished is not None
    assert set_status_finished.finished_at == set_status_finished.updated_at


def test_explicit_finished_at_wins_over_automatic_stamp(tmp_path: Path) -> None:
    store = TaskStore(Config(runtime_root=tmp_path))
    task = store.add_task(tmp_path, "work")
    explicit = datetime(2025, 12, 31, 23, 59, tzinfo=UTC)

    store.update_task(
        task.id,
        status=TaskStatus.NEEDS_REVIEW,
        finished_at=explicit,
    )

    updated = store.get_task(task.id)
    assert updated is not None
    assert updated.finished_at == explicit


def test_token_usage_round_trip(tmp_path: Path) -> None:
    store = TaskStore(Config(runtime_root=tmp_path))
    task = store.add_task(tmp_path, "work")
    expected = TokenUsage(
        input_tokens=11,
        output_tokens=12,
        cache_read_tokens=13,
        cache_write_tokens=14,
        reasoning_tokens=15,
    )

    store.update_task(
        task.id,
        tokens_input=expected.input_tokens,
        tokens_output=expected.output_tokens,
        tokens_cache_read=expected.cache_read_tokens,
        tokens_cache_write=expected.cache_write_tokens,
        tokens_reasoning=expected.reasoning_tokens,
    )

    stored = store.get_task(task.id)
    assert stored is not None
    assert stored.tokens == expected


def test_missing_token_usage_round_trips_as_none(tmp_path: Path) -> None:
    store = TaskStore(Config(runtime_root=tmp_path))

    task = store.add_task(tmp_path, "work")

    assert task.tokens is None
    stored = store.get_task(task.id)
    assert stored is not None
    assert stored.tokens is None


def test_model_and_quota_fields_round_trip(tmp_path: Path) -> None:
    store = TaskStore(Config(runtime_root=tmp_path))
    task = store.add_task(tmp_path, "work")
    resets_at = datetime(2026, 8, 18, 0, 47, 55, tzinfo=UTC)

    store.update_task(
        task.id,
        model="gpt-5.6-sol",
        plan_type="plus",
        quota_used_pct=4.0,
        quota_window_minutes=10080,
        quota_resets_at=resets_at,
    )

    stored = store.get_task(task.id)
    assert stored is not None
    assert stored.model == "gpt-5.6-sol"
    assert stored.plan_type == "plus"
    assert stored.quota_used_pct == 4.0
    assert stored.quota_window_minutes == 10080
    assert stored.quota_resets_at == resets_at


def test_list_tasks_filters_by_engine_kind_and_repo_path(tmp_path: Path) -> None:
    store = TaskStore(Config(runtime_root=tmp_path))
    first_repo = tmp_path / "first"
    second_repo = tmp_path / "second"
    first = store.add_task(
        first_repo,
        "implement",
        kind=TaskKind.IMPLEMENT,
        engine=Engine.CODEX,
    )
    second = store.add_task(
        second_repo,
        "review",
        kind=TaskKind.REVIEW,
        engine=Engine.GROK,
    )

    assert [task.id for task in store.list_tasks(engine=Engine.CODEX)] == [first.id]
    assert [task.id for task in store.list_tasks(kind=TaskKind.REVIEW)] == [second.id]
    equivalent_path = first_repo / ".." / first_repo.name
    assert [task.id for task in store.list_tasks(repo_path=equivalent_path)] == [first.id]


def test_list_tasks_filters_by_created_at_window(tmp_path: Path) -> None:
    store = TaskStore(Config(runtime_root=tmp_path))
    first = store.add_task(tmp_path, "first")
    second = store.add_task(tmp_path, "second")
    third = store.add_task(tmp_path, "third")
    first_time = datetime(2026, 1, 1, tzinfo=UTC)
    second_time = datetime(2026, 1, 2, tzinfo=UTC)
    third_time = datetime(2026, 1, 3, tzinfo=UTC)
    store.update_task(first.id, created_at=first_time)
    store.update_task(second.id, created_at=second_time)
    store.update_task(third.id, created_at=third_time)

    assert [
        task.id
        for task in store.list_tasks(since=second_time, until=third_time)
    ] == [second.id]


def test_list_tasks_filters_by_workflow_id(tmp_path: Path) -> None:
    store = TaskStore(Config(runtime_root=tmp_path))
    unrelated = store.add_task(tmp_path, "unrelated")
    routes = tuple(
        WorkflowRoute(kind, Engine.CODEX, FallbackMode.AUTO, ()) for kind in TaskKind
    )
    batch = store.create_batch(
        tmp_path,
        routes,
        (
            PreparedWorkflowTask(WorkflowTaskRequest("first"), Engine.CODEX),
            PreparedWorkflowTask(
                WorkflowTaskRequest("second", kind=TaskKind.REVIEW),
                Engine.GROK,
            ),
        ),
    )

    listed = store.list_tasks(workflow_id=batch.id)

    assert [task.id for task in listed] == [task.id for task in batch.tasks]
    assert unrelated.id not in {task.id for task in listed}


def test_since_and_until_bounds_are_converted_to_utc_before_comparing(tmp_path: Path) -> None:
    """SQLite compares these bounds lexically, so a non-UTC offset names another instant.

    Measured before the fix: a +09:00 bound of 10:00 sorted after a row stored at
    05:00+00:00 and excluded it, though that row is four hours later.
    """
    config = Config(runtime_root=tmp_path)
    store = TaskStore(config)
    task = store.add_task(repo_path=tmp_path, task="t")

    tokyo = timezone(timedelta(hours=9))
    before = (task.created_at - timedelta(hours=1)).astimezone(tokyo)
    after = (task.created_at + timedelta(hours=1)).astimezone(tokyo)

    assert [found.id for found in store.list_tasks(since=before)] == [task.id]
    assert store.list_tasks(since=after) == []
    assert [found.id for found in store.list_tasks(until=after)] == [task.id]
    assert store.list_tasks(until=before) == []


def test_a_naive_bound_is_read_as_utc(tmp_path: Path) -> None:
    config = Config(runtime_root=tmp_path)
    store = TaskStore(config)
    task = store.add_task(repo_path=tmp_path, task="t")

    naive_before = (task.created_at - timedelta(hours=1)).replace(tzinfo=None)

    assert [found.id for found in store.list_tasks(since=naive_before)] == [task.id]

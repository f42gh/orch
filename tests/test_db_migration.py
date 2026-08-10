"""The v0 database already exists on this machine, so migration must be non-destructive."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_orchestrator.config import Config
from agent_orchestrator.db import ADDED_COLUMNS, TaskStore
from agent_orchestrator.models import Engine, Priority, Risk, TaskKind, TaskStatus


V0_SCHEMA = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    repo_path TEXT NOT NULL,
    workspace_path TEXT,
    branch_name TEXT,
    session_id TEXT,
    task TEXT NOT NULL,
    risk TEXT NOT NULL,
    priority TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    result_summary TEXT,
    error TEXT
);
"""


def write_v0_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(V0_SCHEMA)
    conn.execute(
        """
        INSERT INTO tasks (
            id, repo_path, task, risk, priority, status, created_at, updated_at, result_summary
        ) VALUES (
            'task-0001', '/tmp/legacy', 'legacy task', 'normal', 'normal', 'succeeded',
            '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', 'done'
        )
        """
    )
    conn.commit()
    conn.close()


def test_v0_rows_survive_and_gain_defaults(tmp_path: Path) -> None:
    config = Config(runtime_root=tmp_path)
    write_v0_database(config.db_path)

    store = TaskStore(config)
    task = store.get_task("task-0001")

    assert task is not None
    assert task.task == "legacy task"
    assert task.result_summary == "done"
    assert task.status == TaskStatus.SUCCEEDED
    # New columns read back as sensible defaults rather than blowing up.
    assert task.kind == TaskKind.IMPLEMENT
    assert task.engine is None
    assert task.cost_usd is None
    assert task.parent_id is None


def test_migration_adds_every_new_column_once(tmp_path: Path) -> None:
    config = Config(runtime_root=tmp_path)
    write_v0_database(config.db_path)

    TaskStore(config)
    TaskStore(config)  # re-opening must not try to add the columns twice

    conn = sqlite3.connect(config.db_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
    conn.close()

    assert {name for name, _ in ADDED_COLUMNS} <= columns


@pytest.mark.parametrize("legacy", [False, True])
def test_concurrent_store_initialization_serializes_migration(
    tmp_path: Path, legacy: bool
) -> None:
    config = Config(runtime_root=tmp_path)
    if legacy:
        write_v0_database(config.db_path)

    with ThreadPoolExecutor(max_workers=12) as executor:
        stores = list(executor.map(lambda _: TaskStore(config), range(12)))

    assert len(stores) == 12
    conn = sqlite3.connect(config.db_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
    conn.close()
    assert {name for name, _ in ADDED_COLUMNS} <= columns


def test_new_tasks_round_trip_engine_and_kind(tmp_path: Path) -> None:
    store = TaskStore(Config(runtime_root=tmp_path))

    created = store.add_task(
        tmp_path,
        "レビューして",
        Risk.READ_ONLY,
        Priority.NORMAL,
        kind=TaskKind.REVIEW,
        engine=Engine.GROK,
        parent_id="task-0001",
        base_ref="main",
    )

    stored = store.get_task(created.id)
    assert stored is not None
    assert stored.kind == TaskKind.REVIEW
    assert stored.engine == Engine.GROK
    assert stored.parent_id == "task-0001"
    assert stored.base_ref == "main"

    store.update_task(stored.id, engine=Engine.CODEX, cost_usd=0.125, exit_code=0)
    updated = store.get_task(stored.id)
    assert updated is not None
    assert updated.engine == Engine.CODEX
    assert updated.cost_usd == 0.125
    assert updated.exit_code == 0


def test_task_ids_continue_past_four_digits(tmp_path: Path) -> None:
    store = TaskStore(Config(runtime_root=tmp_path))
    first = store.add_task(tmp_path, "old", Risk.NORMAL, Priority.NORMAL)
    second = store.add_task(tmp_path, "newer", Risk.NORMAL, Priority.NORMAL)
    with store._conn() as conn:  # noqa: SLF001 - establish the persisted boundary case
        conn.execute("UPDATE tasks SET id = 'task-9999' WHERE id = ?", (first.id,))
        conn.execute("UPDATE tasks SET id = 'task-10000' WHERE id = ?", (second.id,))

    created = store.add_task(tmp_path, "next", Risk.NORMAL, Priority.NORMAL)

    assert created.id == "task-10001"


def test_claim_is_atomic(tmp_path: Path) -> None:
    store = TaskStore(Config(runtime_root=tmp_path))
    store.add_task(tmp_path, "low", Risk.NORMAL, Priority.LOW)
    high = store.add_task(tmp_path, "high", Risk.NORMAL, Priority.HIGH)

    first = store.claim_next_task()
    assert first is not None
    assert first.id == high.id
    assert first.status == TaskStatus.RUNNING

    # A second claim must not hand out the same task again.
    second = store.claim_next_task()
    assert second is not None
    assert second.id != first.id

    assert store.claim_next_task() is None


def test_claiming_a_specific_task_twice_fails_the_second_time(tmp_path: Path) -> None:
    store = TaskStore(Config(runtime_root=tmp_path))
    task = store.add_task(tmp_path, "work", Risk.NORMAL, Priority.NORMAL)

    assert store.claim_task(task.id) is not None
    assert store.claim_task(task.id) is None


def test_list_tasks_filters_by_status_and_parent(tmp_path: Path) -> None:
    store = TaskStore(Config(runtime_root=tmp_path))
    parent = store.add_task(tmp_path, "parent", Risk.NORMAL, Priority.NORMAL)
    child = store.add_task(tmp_path, "child", Risk.NORMAL, Priority.NORMAL, parent_id=parent.id)

    assert [item.id for item in store.list_tasks(parent_id=parent.id)] == [child.id]
    assert len(store.list_tasks(status=TaskStatus.QUEUED)) == 2
    assert store.list_tasks(status=TaskStatus.FAILED) == []

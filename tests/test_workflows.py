from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import agent_orchestrator.db as db_module
from agent_orchestrator.config import Config
from agent_orchestrator.db import TaskStore
from agent_orchestrator.models import (
    Engine,
    FallbackMode,
    Risk,
    TaskKind,
    TaskStatus,
    WorkflowRoute,
    WorkflowRouteOverride,
    WorkflowStatus,
    WorkflowTaskRequest,
    WorkflowType,
)
from agent_orchestrator.router import (
    AccessLevel,
    RoutingError,
    resolve_policy,
    resolve_workflow_engine,
    snapshot_workflow_routes,
)
from agent_orchestrator.workflows import (
    WorkflowError,
    close_workflow,
    create_run,
    dispatch_batch,
    dispatch_run,
    list_workflows,
    show_workflow,
)


@pytest.fixture
def env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Config, TaskStore, Path, list[str]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = Config(
        runtime_root=tmp_path / "runtime",
        routing_path=tmp_path / "missing-routing.toml",
    )
    store = TaskStore(config)
    spawned: list[str] = []
    monkeypatch.setattr(
        "agent_orchestrator.workflows.spawn_worker",
        lambda cfg, task_id: (spawned.append(task_id), 4000 + len(spawned))[1],
    )
    monkeypatch.setattr(
        "agent_orchestrator.workflows.probe_all",
        lambda refresh=False: {engine: object() for engine in Engine},
    )
    return config, store, repo, spawned


def test_existing_database_migrates_workflow_tables(tmp_path: Path) -> None:
    config = Config(runtime_root=tmp_path / "runtime")
    config.runtime_root.mkdir()
    conn = sqlite3.connect(config.db_path)
    conn.executescript(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, repo_path TEXT NOT NULL, workspace_path TEXT,
            branch_name TEXT, session_id TEXT, task TEXT NOT NULL, risk TEXT NOT NULL,
            priority TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, result_summary TEXT, error TEXT
        );
        """
    )
    conn.close()

    TaskStore(config)

    conn = sqlite3.connect(config.db_path)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    conn.close()
    assert {"workflows", "workflow_routes", "workflow_tasks"} <= tables


def test_route_snapshot_is_complete_and_auto_chains_after_custom_primary() -> None:
    routes = snapshot_workflow_routes(
        overrides={TaskKind.IMPLEMENT: WorkflowRouteOverride(Engine.CLAUDE)}
    )

    assert tuple(route.kind for route in routes) == tuple(TaskKind)
    implement = next(route for route in routes if route.kind is TaskKind.IMPLEMENT)
    assert implement.primary is Engine.CLAUDE
    assert implement.fallbacks == (Engine.CODEX, Engine.GROK)
    assert implement.fallback_mode is FallbackMode.AUTO

    # Auto keeps deterministic rescue outside the snapshotted configured chain.
    ui = next(route for route in routes if route.kind is TaskKind.UI_VERIFY)
    assert resolve_workflow_engine(ui, {Engine.GROK, Engine.CODEX}) is Engine.CODEX


@pytest.mark.parametrize(
    "fallbacks, message",
    [
        ((), "nonempty"),
        ((Engine.CODEX, Engine.CODEX), "unique"),
        ((Engine.CLAUDE,), "exclude"),
    ],
)
def test_manual_fallback_validation(
    fallbacks: tuple[Engine, ...], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        WorkflowRouteOverride(
            Engine.CLAUDE,
            fallback_mode=FallbackMode.MANUAL,
            fallbacks=fallbacks,
        )


def test_manual_route_is_exhaustive() -> None:
    route = WorkflowRoute(
        kind=TaskKind.REVIEW,
        primary=Engine.GROK,
        fallback_mode=FallbackMode.MANUAL,
        fallbacks=(Engine.CLAUDE,),
    )

    with pytest.raises(RoutingError, match="manual route.*exhausted"):
        resolve_workflow_engine(route, {Engine.CODEX})


def test_run_persists_snapshot_and_is_immutable(env) -> None:
    config, store, repo, _ = env
    details = create_run(
        config,
        store,
        repo=repo,
        routes={TaskKind.REVIEW: Engine.CODEX},
    )

    assert details.workflow.id == "run-0001"
    assert details.workflow.workflow_type is WorkflowType.RUN
    assert details.workflow.status is WorkflowStatus.OPEN
    assert details.workflow.repo_path == repo.resolve()
    assert len(details.routes) == len(TaskKind)
    with pytest.raises(FrozenInstanceError):
        details.workflow.status = WorkflowStatus.CLOSED  # type: ignore[misc]

    assert list_workflows(store) == (details.workflow,)
    assert show_workflow(store, "run-0001").routes == details.routes


def test_run_dispatch_uses_persisted_routes_after_config_changes(env) -> None:
    config, store, repo, _ = env
    run = create_run(config, store, repo=repo).workflow
    config.routing_path.write_text(
        """
        [kinds.implement]
        engine = "grok"
        fallbacks = ["claude"]
        """,
        encoding="utf-8",
    )

    dispatched = dispatch_run(config, store, run_id=run.id, task="implement")

    assert dispatched.engine is Engine.CODEX


def test_run_resolution_failure_creates_no_task(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, store, repo, spawned = env
    details = create_run(
        config,
        store,
        repo=repo,
        routes={
            TaskKind.REVIEW: WorkflowRouteOverride(
                Engine.GROK,
                fallback_mode=FallbackMode.MANUAL,
                fallbacks=(Engine.CLAUDE,),
            )
        },
    )
    monkeypatch.setattr(
        "agent_orchestrator.workflows.probe_all",
        lambda refresh=False: {Engine.CODEX: object()},
    )

    with pytest.raises(WorkflowError, match="manual route.*exhausted"):
        dispatch_run(
            config,
            store,
            run_id=details.workflow.id,
            task="review it",
            kind=TaskKind.REVIEW,
        )

    assert store.list_tasks() == []
    assert show_workflow(store, details.workflow.id).memberships == ()
    assert spawned == []


@pytest.mark.parametrize("task", ["", "   "])
def test_run_rejects_empty_task_text_without_persisting(env, task: str) -> None:
    config, store, repo, spawned = env
    run = create_run(config, store, repo=repo).workflow

    with pytest.raises(WorkflowError, match="non-empty"):
        dispatch_run(config, store, run_id=run.id, task=task)

    assert store.list_tasks() == []
    assert show_workflow(store, run.id).memberships == ()
    assert spawned == []


def test_run_accepts_ordered_tasks_until_closed(env) -> None:
    config, store, repo, spawned = env
    run = create_run(config, store, repo=repo).workflow

    first = dispatch_run(config, store, run_id=run.id, task="first")
    second = dispatch_run(
        config,
        store,
        run_id=run.id,
        task="second",
        kind=TaskKind.REVIEW,
    )
    closed = close_workflow(store, run.id)

    assert [first.membership.ordinal, second.membership.ordinal] == [0, 1]
    assert [task.task for task in closed.tasks] == ["first", "second"]
    assert closed.workflow.status is WorkflowStatus.CLOSED
    assert closed.workflow.closed_at is not None
    with pytest.raises(WorkflowError, match="closed"):
        dispatch_run(config, store, run_id=run.id, task="too late")
    assert len(store.list_tasks()) == 2
    assert spawned == [first.task.id, second.task.id]


def test_run_reports_a_spawn_failure_and_keeps_the_queued_task(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, store, repo, _ = env
    run = create_run(config, store, repo=repo).workflow
    monkeypatch.setattr(
        "agent_orchestrator.workflows.spawn_worker",
        lambda cfg, task_id: (_ for _ in ()).throw(OSError("cannot spawn")),
    )

    result = dispatch_run(config, store, run_id=run.id, task="recover me")

    assert result.worker_pid is None
    assert result.spawn_error == "OSError: cannot spawn"
    assert result.task.status is TaskStatus.QUEUED
    assert show_workflow(store, run.id).memberships == (result.membership,)


def test_run_dispatch_rejects_a_repo_that_disappeared(env) -> None:
    config, store, repo, spawned = env
    run = create_run(config, store, repo=repo).workflow
    repo.rmdir()

    with pytest.raises(WorkflowError, match="repo does not exist"):
        dispatch_run(config, store, run_id=run.id, task="too late")

    assert store.list_tasks() == []
    assert spawned == []


def test_run_dispatch_refreshes_engine_availability(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, store, repo, _ = env
    run = create_run(config, store, repo=repo).workflow
    refreshes: list[bool] = []

    def probe(refresh: bool = False) -> dict[Engine, object]:
        refreshes.append(refresh)
        return {Engine.CODEX: object()}

    monkeypatch.setattr("agent_orchestrator.workflows.probe_all", probe)

    dispatch_run(config, store, run_id=run.id, task="fresh probe")

    assert refreshes == [True]


def test_batch_preflight_failure_writes_nothing(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, store, repo, spawned = env
    monkeypatch.setattr(
        "agent_orchestrator.workflows.probe_all",
        lambda refresh=False: {Engine.CODEX: object()},
    )

    with pytest.raises(WorkflowError, match="manual route.*exhausted"):
        dispatch_batch(
            config,
            store,
            repo=repo,
            tasks=(
                WorkflowTaskRequest("implement", kind=TaskKind.IMPLEMENT),
                WorkflowTaskRequest("review", kind=TaskKind.REVIEW),
            ),
            routes={
                TaskKind.REVIEW: WorkflowRouteOverride(
                    Engine.GROK,
                    fallback_mode=FallbackMode.MANUAL,
                    fallbacks=(Engine.CLAUDE,),
                )
            },
        )

    assert store.list_tasks() == []
    assert list_workflows(store) == ()
    assert spawned == []


def test_batch_database_failure_rolls_back_every_row(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, store, repo, spawned = env
    original = db_module._insert_prepared_task
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected insert failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(db_module, "_insert_prepared_task", fail_second)

    with pytest.raises(RuntimeError, match="injected"):
        dispatch_batch(
            config,
            store,
            repo=repo,
            tasks=(WorkflowTaskRequest("one"), WorkflowTaskRequest("two")),
        )

    assert store.list_tasks() == []
    assert list_workflows(store) == ()
    assert spawned == []


def test_batch_is_sealed_and_starts_workers_in_input_order(env) -> None:
    config, store, repo, spawned = env

    result = dispatch_batch(
        config,
        store,
        repo=repo,
        tasks=(
            WorkflowTaskRequest("third name, first input", kind=TaskKind.REVIEW),
            WorkflowTaskRequest("first name, second input"),
            WorkflowTaskRequest("second name, third input", kind=TaskKind.TEST),
        ),
    )
    details = show_workflow(store, result.workflow.id)

    assert result.workflow.id == "batch-0001"
    assert result.workflow.status is WorkflowStatus.SEALED
    assert [member.ordinal for member in details.memberships] == [0, 1, 2]
    assert [task.task for task in details.tasks] == [
        "third name, first input",
        "first name, second input",
        "second name, third input",
    ]
    assert spawned == [task.id for task in details.tasks]
    with pytest.raises(WorkflowError, match="sealed batch"):
        close_workflow(store, result.workflow.id)


def test_batch_reports_a_spawn_failure_and_starts_remaining_tasks(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, store, repo, _ = env
    calls: list[str] = []

    def spawn(config: Config, task_id: str) -> int:
        calls.append(task_id)
        if len(calls) == 2:
            raise OSError("cannot spawn")
        return 5000 + len(calls)

    monkeypatch.setattr("agent_orchestrator.workflows.spawn_worker", spawn)

    result = dispatch_batch(
        config,
        store,
        repo=repo,
        tasks=tuple(WorkflowTaskRequest(f"task {index}") for index in range(3)),
    )

    assert calls == ["task-0001", "task-0002", "task-0003"]
    assert [item.worker_pid for item in result.dispatched] == [5001, None, 5003]
    assert result.dispatched[0].spawn_error is None
    assert result.dispatched[1].spawn_error == "OSError: cannot spawn"
    assert result.dispatched[2].spawn_error is None
    assert [task.status for task in show_workflow(store, result.workflow.id).tasks] == [
        TaskStatus.QUEUED,
        TaskStatus.QUEUED,
        TaskStatus.QUEUED,
    ]


def test_concurrent_run_dispatch_assigns_unique_ordinals(env) -> None:
    config, store, repo, _ = env
    run = create_run(config, store, repo=repo).workflow

    def add(number: int) -> None:
        dispatch_run(config, store, run_id=run.id, task=f"task {number}")

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(add, range(8)))

    details = show_workflow(store, run.id)
    assert [membership.ordinal for membership in details.memberships] == list(range(8))
    assert len({membership.task_id for membership in details.memberships}) == 8
    assert {task.task for task in details.tasks} == {f"task {number}" for number in range(8)}


def test_workflow_engine_does_not_change_kind_and_risk_safety(env) -> None:
    config, store, repo, _ = env
    run = create_run(
        config,
        store,
        repo=repo,
        routes={TaskKind.IMPLEMENT: Engine.CLAUDE},
    ).workflow

    dispatched = dispatch_run(
        config,
        store,
        run_id=run.id,
        task="plan dangerous work",
        kind=TaskKind.IMPLEMENT,
        risk=Risk.HIGH,
    )
    policy = resolve_policy(
        dispatched.task.kind,
        dispatched.task.risk,
        dispatched.task.engine or Engine.CLAUDE,
    )

    assert dispatched.task.engine is Engine.CLAUDE
    assert dispatched.task.kind is TaskKind.IMPLEMENT
    assert dispatched.task.risk is Risk.HIGH
    assert policy.access is AccessLevel.READ_ONLY

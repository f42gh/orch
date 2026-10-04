"""Persistent run and batch workflows built on the existing task worker path."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from orch.config import Config
from orch.db import TaskStore
from orch.dispatch import Dispatched, spawn_worker
from orch.engines import probe_all
from orch.models import (
    Engine,
    PreparedWorkflowTask,
    Priority,
    Risk,
    Task,
    TaskKind,
    Workflow,
    WorkflowDetails,
    WorkflowRoute,
    WorkflowRouteOverride,
    WorkflowTask,
    WorkflowTaskRequest,
    WorkflowType,
)
from orch.router import (
    RoutingError,
    load_routing_table,
    resolve_workflow_engine,
    snapshot_workflow_routes,
)


class WorkflowError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DispatchedWorkflowTask:
    workflow: Workflow
    membership: WorkflowTask
    dispatched: Dispatched
    spawn_error: str | None = None

    @property
    def task(self) -> Task:
        return self.dispatched.task

    @property
    def engine(self) -> Engine:
        return self.dispatched.engine

    @property
    def branch(self) -> str:
        return self.dispatched.branch

    @property
    def worker_pid(self) -> int | None:
        return self.dispatched.worker_pid


@dataclass(frozen=True, slots=True)
class DispatchedBatch:
    workflow: Workflow
    dispatched: tuple[DispatchedWorkflowTask, ...]

    @property
    def tasks(self) -> tuple[Task, ...]:
        return tuple(item.task for item in self.dispatched)


def create_run(
    config: Config,
    store: TaskStore,
    *,
    repo: str | Path,
    routes: Mapping[TaskKind, WorkflowRouteOverride | Engine] | None = None,
) -> WorkflowDetails:
    """Create an open run with all six current kind routes frozen in place."""
    repo_path = _existing_repo(repo)
    snapshots = _snapshot(config, routes)
    workflow = store.create_run(repo_path, snapshots)
    details = store.get_workflow_details(workflow.id)
    if details is None:
        raise WorkflowError(f"failed to load workflow {workflow.id}")
    return details


def dispatch_run(
    config: Config,
    store: TaskStore,
    *,
    run_id: str,
    task: str | WorkflowTaskRequest,
    kind: TaskKind = TaskKind.IMPLEMENT,
    risk: Risk = Risk.NORMAL,
    priority: Priority = Priority.NORMAL,
    parent_id: str | None = None,
    base_ref: str | None = None,
) -> DispatchedWorkflowTask:
    """Resolve and append one task to an open run, then start its worker."""
    request = _task_request(task, kind, risk, priority, parent_id, base_ref)
    details = show_workflow(store, run_id)
    if details.workflow.workflow_type is not WorkflowType.RUN:
        raise WorkflowError(f"workflow {run_id} is a sealed batch")
    if not details.workflow.is_open:
        raise WorkflowError(f"workflow {run_id} is closed")
    _existing_repo(details.workflow.repo_path)

    prepared = _preflight((request,), details.routes)[0]
    try:
        created, membership = store.append_workflow_task(run_id, prepared)
    except (LookupError, ValueError) as exc:
        raise WorkflowError(str(exc)) from None
    dispatched, spawn_error = _launch_task(config, created)
    refreshed = store.get_workflow(run_id) or details.workflow
    return DispatchedWorkflowTask(
        refreshed,
        membership,
        dispatched,
        spawn_error=spawn_error,
    )


def dispatch_batch(
    config: Config,
    store: TaskStore,
    *,
    repo: str | Path,
    tasks: Sequence[WorkflowTaskRequest],
    routes: Mapping[TaskKind, WorkflowRouteOverride | Engine] | None = None,
) -> DispatchedBatch:
    """Preflight a whole batch, persist it atomically, then start workers in order."""
    repo_path = _existing_repo(repo)
    requests = tuple(tasks)
    if not requests:
        raise WorkflowError("a batch must contain at least one task")
    snapshots = _snapshot(config, routes)
    prepared = _preflight(requests, snapshots)

    try:
        details = store.create_batch(repo_path, snapshots, prepared)
    except ValueError as exc:
        raise WorkflowError(str(exc)) from None

    dispatched: list[DispatchedWorkflowTask] = []
    for task, membership in zip(
        details.tasks, details.memberships, strict=True
    ):
        started, spawn_error = _launch_task(config, task)
        dispatched.append(
            DispatchedWorkflowTask(
                details.workflow,
                membership,
                started,
                spawn_error=spawn_error,
            )
        )
    return DispatchedBatch(details.workflow, tuple(dispatched))


def list_workflows(
    store: TaskStore,
    workflow_type: WorkflowType | None = None,
) -> tuple[Workflow, ...]:
    return tuple(store.list_workflows(workflow_type))


def show_workflow(store: TaskStore, workflow_id: str) -> WorkflowDetails:
    details = store.get_workflow_details(workflow_id)
    if details is None:
        raise WorkflowError(f"workflow not found: {workflow_id}")
    return details


def close_workflow(store: TaskStore, workflow_id: str) -> WorkflowDetails:
    try:
        store.close_workflow(workflow_id)
    except (LookupError, ValueError) as exc:
        raise WorkflowError(str(exc)) from None
    return show_workflow(store, workflow_id)


def _snapshot(
    config: Config,
    routes: Mapping[TaskKind, WorkflowRouteOverride | Engine] | None,
) -> tuple[WorkflowRoute, ...]:
    normalized = {
        kind: route if isinstance(route, WorkflowRouteOverride) else WorkflowRouteOverride(route)
        for kind, route in (routes or {}).items()
    }
    try:
        return snapshot_workflow_routes(
            load_routing_table(config.routing_path), normalized
        )
    except (RoutingError, ValueError) as exc:
        raise WorkflowError(str(exc)) from None


def _preflight(
    requests: tuple[WorkflowTaskRequest, ...],
    routes: tuple[WorkflowRoute, ...],
) -> tuple[PreparedWorkflowTask, ...]:
    for request in requests:
        if not request.task.strip():
            raise WorkflowError("workflow task text must be a non-empty string")
    by_kind = {route.kind: route for route in routes}
    # Runs may outlive both this process and the set of installed CLIs. Probe once per
    # dispatch (and once for the whole Batch) so a stale process cache cannot select an
    # engine that disappeared after the route snapshot was created.
    available = probe_all(refresh=True).keys()
    prepared: list[PreparedWorkflowTask] = []
    for request in requests:
        route = by_kind.get(request.kind)
        if route is None:
            raise WorkflowError(f"workflow has no route for {request.kind.value}")
        try:
            engine = resolve_workflow_engine(route, available)
        except RoutingError as exc:
            raise WorkflowError(str(exc)) from None
        prepared.append(PreparedWorkflowTask(request, engine))
    return tuple(prepared)


def _task_request(
    task: str | WorkflowTaskRequest,
    kind: TaskKind,
    risk: Risk,
    priority: Priority,
    parent_id: str | None,
    base_ref: str | None,
) -> WorkflowTaskRequest:
    if isinstance(task, WorkflowTaskRequest):
        return task
    return WorkflowTaskRequest(
        task=task,
        kind=kind,
        risk=risk,
        priority=priority,
        parent_id=parent_id,
        base_ref=base_ref,
    )


def _existing_repo(repo: str | Path) -> Path:
    repo_path = Path(repo).expanduser()
    if not repo_path.exists():
        raise WorkflowError(f"repo does not exist: {repo_path}")
    return repo_path.resolve()


def _start_task(config: Config, task: Task) -> Dispatched:
    if task.engine is None or task.branch_name is None:
        raise WorkflowError(f"workflow task {task.id} has incomplete routing")
    pid = spawn_worker(config, task.id)
    return Dispatched(
        task=task,
        branch=task.branch_name,
        engine=task.engine,
        worker_pid=pid,
    )


def _launch_task(config: Config, task: Task) -> tuple[Dispatched, str | None]:
    try:
        return _start_task(config, task), None
    except OSError as exc:
        # The task row is already committed. Report it as queued so callers retain its
        # identity and the daemon can recover it; Batch callers can keep launching the
        # remaining independent tasks.
        if task.engine is None or task.branch_name is None:
            raise WorkflowError(f"workflow task {task.id} has incomplete routing") from exc
        return (
            Dispatched(task, task.branch_name, task.engine, None),
            f"{type(exc).__name__}: {exc}",
        )

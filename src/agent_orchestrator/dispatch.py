"""The one dispatch path: write the task row, then start its detached worker.

`orch_dispatch` (MCP) and `agentctl dispatch` (what CAGE calls) both go through
`dispatch_task`, so queueing and spawning cannot drift apart between entry points.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from agent_orchestrator.config import Config
from agent_orchestrator.db import TaskStore
from agent_orchestrator.engines import probe_all
from agent_orchestrator.logging_utils import task_log_dir
from agent_orchestrator.models import Engine, Priority, Risk, Task, TaskKind
from agent_orchestrator.router import RoutingError, load_routing_table, resolve_engine
from agent_orchestrator.workspace import branch_name_for_task


class DispatchError(RuntimeError):
    pass


@dataclass(frozen=True)
class Dispatched:
    """What a caller learns immediately: identity, routing, and the worker's pid."""

    task: Task
    branch: str
    engine: Engine
    worker_pid: int | None

    def describe(self, config: Config) -> dict[str, object]:
        return {
            "task_id": self.task.id,
            "status": self.task.status.value,
            "kind": self.task.kind.value,
            "engine": self.engine.value,
            "risk": self.task.risk.value,
            "repo": str(self.task.repo_path),
            "branch": self.branch,
            "parent_id": self.task.parent_id,
            "created_at": self.task.created_at.isoformat(),
            "log_path": str(config.logs_dir / self.task.id),
            "worker_pid": self.worker_pid,
        }


def pid_path(config: Config, task_id: str) -> Path:
    return task_log_dir(config, task_id) / "worker.pid"


def spawn_worker(config: Config, task_id: str) -> int:
    """Start a detached worker for `task_id` and return its pid.

    The child is put in its own session so it outlives the dispatching process, and its
    output goes to a file — the caller's stdout may be an MCP transport.
    """
    log_dir = task_log_dir(config, task_id)
    worker_log = log_dir / "worker.log"
    handle = worker_log.open("a", encoding="utf-8")
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agent_orchestrator.daemon",
                "run-task",
                task_id,
                "--runtime-root",
                str(config.runtime_root),
            ],
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=handle,
            start_new_session=True,
            env={**os.environ, "PYTHONPATH": _package_root()},
        )
    finally:
        handle.close()
    try:
        pid_path(config, task_id).write_text(str(process.pid), encoding="utf-8")
    except OSError:
        # A detached worker without its pid file cannot be cancelled or inspected
        # reliably. Stop the whole new session before reporting that spawn failed.
        _stop_spawned_worker(process)
        raise
    return process.pid


def _stop_spawned_worker(process: subprocess.Popen[bytes]) -> None:
    _signal_spawned_worker(process, signal.SIGTERM)
    try:
        process.wait(timeout=5)
        return
    except (subprocess.TimeoutExpired, OSError):
        pass
    _signal_spawned_worker(process, signal.SIGKILL)
    try:
        process.wait(timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _signal_spawned_worker(
    process: subprocess.Popen[bytes], requested: signal.Signals
) -> None:
    try:
        os.killpg(process.pid, requested)
        return
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        if requested is signal.SIGTERM:
            process.terminate()
        else:
            process.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _package_root() -> str:
    """`src/` on the path, so the detached worker imports the same code as this process."""
    existing = os.environ.get("PYTHONPATH", "")
    root = str(Path(__file__).resolve().parents[1])
    return f"{root}{os.pathsep}{existing}" if existing else root


def existing_repo(repo: str | Path) -> Path:
    """The repo check every entry point that queues a task has to make.

    `agentctl add` used to skip it and store `--repo` verbatim, so a typo or an
    unexpanded `~` became a queued task that only failed later, in a detached worker,
    where nobody was watching.
    """
    repo_path = Path(repo).expanduser()
    if not repo_path.exists():
        raise DispatchError(f"repo does not exist: {repo_path}")
    return repo_path


def dispatch_task(
    config: Config,
    store: TaskStore,
    *,
    repo: str | Path,
    task: str,
    kind: TaskKind,
    risk: Risk,
    priority: Priority,
    engine: Engine | None = None,
    parent_id: str | None = None,
    base_ref: str | None = None,
) -> Dispatched:
    repo_path = existing_repo(repo)

    # Resolve the engine now rather than in the worker, so the caller learns
    # immediately when it asked for something this machine does not have.
    table = load_routing_table(config.routing_path)
    try:
        chosen = resolve_engine(kind, probe_all().keys(), engine, table)
    except RoutingError as exc:
        raise DispatchError(str(exc)) from None

    created = store.add_task(
        repo_path=repo_path,
        task=task,
        risk=risk,
        priority=priority,
        kind=kind,
        engine=chosen,
        parent_id=parent_id,
        base_ref=base_ref,
    )
    # The worktree is created by the worker, but its branch name is already
    # determined — and the caller needs it now, to point a reviewer at the branch
    # without waiting for the task to finish.
    branch = branch_name_for_task(created.id, chosen)
    store.update_task(created.id, branch_name=branch)
    refreshed = store.get_task(created.id) or created

    pid = spawn_worker(config, created.id)
    return Dispatched(task=refreshed, branch=branch, engine=chosen, worker_pid=pid)

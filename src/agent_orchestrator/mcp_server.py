"""MCP control plane for the orchestrator.

Claude Code is the orchestrator; this server is how it reaches the other engines.

It deliberately does no work itself. `orch_dispatch` writes a row and spawns a detached
`agentd run-task`, then returns immediately. That keeps three properties worth having:

- a tool call never blocks Claude for the 30 minutes a real task can take,
- work survives the Claude session exiting, since the worker is not a child of it,
- `agentctl`, the HTTP API and the React UI all see the same tasks, because SQLite
  stays the single source of truth.

Nothing here may print to stdout: that is the MCP transport.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from mcp.server import MCPServer

from agent_orchestrator.config import Config, load_config
from agent_orchestrator.db import TaskStore
from agent_orchestrator.dispatch import DispatchError, dispatch_task, pid_path
from agent_orchestrator.engines import probe_all
from agent_orchestrator.models import (
    TERMINAL_STATUSES,
    Engine,
    FallbackMode,
    Priority,
    Risk,
    Task,
    TaskKind,
    TaskStatus,
    Workflow,
    WorkflowDetails,
    WorkflowRouteOverride,
    WorkflowTaskRequest,
    WorkflowType,
)
from agent_orchestrator.router import load_routing_table
from agent_orchestrator.result import save_git_diff
from agent_orchestrator.workflows import (
    DispatchedBatch,
    DispatchedWorkflowTask,
    WorkflowError,
    close_workflow,
    create_run,
    dispatch_batch,
    dispatch_run,
    list_workflows,
    show_workflow,
)

#: Upper bound on `orch_wait`, so a stuck task cannot wedge the caller.
MAX_WAIT_S = 120.0
POLL_INTERVAL_S = 1.0
#: How much of a patch to inline before telling the caller to read the file instead.
DEFAULT_DIFF_BYTES = 60_000
LOG_TAIL_LINES = 40
ENGINE_ALIASES = {"agy": Engine.ANTIGRAVITY.value}


def _store(config: Config) -> TaskStore:
    return TaskStore(config)


def wait_budget(timeout_s: float) -> float:
    """Clamp a requested wait so a stuck task cannot wedge the caller indefinitely."""
    return max(1.0, min(float(timeout_s), MAX_WAIT_S))


def _parse[T](enum: type[T], value: str | None, default: T | None = None) -> T | None:
    if value is None:
        return default
    try:
        return enum(value)  # type: ignore[call-arg]
    except ValueError:
        allowed = ", ".join(member.value for member in enum)  # type: ignore[attr-defined]
        raise DispatchError(f"{value!r} is not one of: {allowed}") from None


def _parse_engine(value: str | None) -> Engine | None:
    if value is None:
        return None
    normalized = ENGINE_ALIASES.get(value.strip(), value.strip())
    try:
        return Engine(normalized)
    except ValueError:
        allowed = ", ".join([*(engine.value for engine in Engine), "agy"])
        raise DispatchError(f"engine {value!r} is not one of: {allowed}") from None


def _parse_workflow_routes(
    routes: Mapping[str, str] | None,
    fallbacks: Mapping[str, Sequence[str]] | None,
) -> dict[TaskKind, WorkflowRouteOverride]:
    primaries: dict[TaskKind, Engine] = {}
    for raw_kind, raw_engine in (routes or {}).items():
        try:
            kind = TaskKind(raw_kind)
        except ValueError:
            allowed = ", ".join(kind.value for kind in TaskKind)
            raise DispatchError(f"task kind {raw_kind!r} is not one of: {allowed}") from None
        parsed = _parse_engine(raw_engine)
        assert parsed is not None
        primaries[kind] = parsed

    manual: dict[TaskKind, tuple[Engine, ...]] = {}
    for raw_kind, raw_chain in (fallbacks or {}).items():
        try:
            kind = TaskKind(raw_kind)
        except ValueError:
            allowed = ", ".join(item.value for item in TaskKind)
            raise DispatchError(f"task kind {raw_kind!r} is not one of: {allowed}") from None
        if kind not in primaries:
            raise DispatchError(
                f"fallbacks for {kind.value} require a matching explicit routes entry"
            )
        if isinstance(raw_chain, str) or not raw_chain:
            raise DispatchError(f"fallbacks for {kind.value} must be a non-empty engine list")
        parsed_chain: list[Engine] = []
        for raw_engine in raw_chain:
            if not isinstance(raw_engine, str) or not raw_engine.strip():
                raise DispatchError(
                    f"fallbacks for {kind.value} must contain non-empty engine names"
                )
            parsed = _parse_engine(raw_engine)
            assert parsed is not None
            parsed_chain.append(parsed)
        manual[kind] = tuple(parsed_chain)

    parsed_routes: dict[TaskKind, WorkflowRouteOverride] = {}
    for kind, primary in primaries.items():
        chain = manual.get(kind)
        try:
            parsed_routes[kind] = (
                WorkflowRouteOverride(primary)
                if chain is None
                else WorkflowRouteOverride(primary, FallbackMode.MANUAL, chain)
            )
        except ValueError as exc:
            raise DispatchError(f"route {kind.value}: {exc}") from None
    return parsed_routes


def _workflow_task_request(value: object, *, where: str) -> WorkflowTaskRequest:
    if not isinstance(value, Mapping):
        raise DispatchError(f"{where} must be an object")
    allowed = {"task", "kind", "risk", "priority", "parent_id", "base_ref"}
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        raise DispatchError(f"{where} has unknown fields: {', '.join(unknown)}")
    task = value.get("task")
    if not isinstance(task, str) or not task.strip():
        raise DispatchError(f"{where}.task must be a non-empty string")
    kind = _parse(TaskKind, value.get("kind", TaskKind.IMPLEMENT.value))
    risk = _parse(Risk, value.get("risk", Risk.NORMAL.value))
    priority = _parse(Priority, value.get("priority", Priority.NORMAL.value))
    parent_id = value.get("parent_id")
    base_ref = value.get("base_ref")
    if parent_id is not None and not isinstance(parent_id, str):
        raise DispatchError(f"{where}.parent_id must be a string or null")
    if base_ref is not None and not isinstance(base_ref, str):
        raise DispatchError(f"{where}.base_ref must be a string or null")
    assert isinstance(kind, TaskKind)
    assert isinstance(risk, Risk)
    assert isinstance(priority, Priority)
    return WorkflowTaskRequest(
        task.strip(),
        kind=kind,
        risk=risk,
        priority=priority,
        parent_id=parent_id,
        base_ref=base_ref,
    )


def _workflow_summary(workflow: Workflow) -> dict[str, Any]:
    return {
        "workflow_id": workflow.id,
        "type": workflow.workflow_type.value,
        "status": workflow.status.value,
        "repo": str(workflow.repo_path),
        "created_at": workflow.created_at.isoformat(),
        "closed_at": workflow.closed_at.isoformat() if workflow.closed_at else None,
    }


def _workflow_details(config: Config, details: WorkflowDetails) -> dict[str, Any]:
    routes = [
        {
            "kind": route.kind.value,
            "primary": route.primary.value,
            "fallback_mode": route.fallback_mode.value,
            "fallbacks": [engine.value for engine in route.fallbacks],
        }
        for route in details.routes
    ]
    tasks = []
    for membership, task in zip(details.memberships, details.tasks, strict=True):
        tasks.append({**_serialize(config, task), "ordinal": membership.ordinal})
    return {
        **_workflow_summary(details.workflow),
        "routes": routes,
        "task_ids": [task["task_id"] for task in tasks],
        "tasks": tasks,
    }


def _workflow_dispatch(config: Config, item: DispatchedWorkflowTask) -> dict[str, Any]:
    return {
        **item.dispatched.describe(config),
        "workflow_id": item.workflow.id,
        "ordinal": item.membership.ordinal,
        "task": item.task.task,
        "spawn_error": item.spawn_error,
    }


def _workflow_batch(
    config: Config, store: TaskStore, result: DispatchedBatch
) -> dict[str, Any]:
    details = show_workflow(store, result.workflow.id)
    tasks = [_workflow_dispatch(config, item) for item in result.dispatched]
    return {
        **_workflow_details(config, details),
        "task_ids": [task["task_id"] for task in tasks],
        "tasks": tasks,
    }


def _workflow_call[T](call: Any, *args: Any, **kwargs: Any) -> T:
    try:
        return call(*args, **kwargs)
    except (WorkflowError, ValueError) as exc:
        raise DispatchError(str(exc)) from None


def _serialize(config: Config, task: Task) -> dict[str, Any]:
    log_dir = config.logs_dir / task.id
    return {
        "task_id": task.id,
        "status": task.status.value,
        "kind": task.kind.value,
        "engine": task.engine.value if task.engine else None,
        "risk": task.risk.value,
        "priority": task.priority.value,
        "task": task.task,
        "repo": str(task.repo_path),
        "workspace": str(task.workspace_path) if task.workspace_path else None,
        "branch": task.branch_name,
        "parent_id": task.parent_id,
        "base_ref": task.base_ref,
        "cost_usd": task.cost_usd,
        "exit_code": task.exit_code,
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
        "log_path": str(log_dir),
        "error": task.error,
    }


def _tail(path: Path, lines: int = LOG_TAIL_LINES) -> str:
    if not path.exists():
        return ""
    content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(content[-lines:])


def build_server(config: Config) -> MCPServer:
    server = MCPServer(
        name="orch",
        version="0.3.0",
        instructions=(
            "Delegate coding work to other agent CLIs (codex, grok, antigravity, claude). "
            "Each task runs in its own git worktree, so several can run at once without "
            "colliding. Call orch_engines first, then confirm Run versus Batch and the "
            "primary/fallback routes with the user. Use orch_run_create and "
            "orch_run_dispatch when later work may be added; use orch_batch_dispatch for "
            "one complete independent task set. Dispatch is asynchronous: tools return "
            "immediately, then poll orch_status or block on orch_wait. Inspect every "
            "dispatch response for spawn_error; that task is still queued and must not "
            "be waited on until an external daemon starts it. Always read "
            "orch_result and orch_diff before adopting anything — the "
            "engines are told not to commit, and nothing reaches the real repository "
            "unless you call orch_adopt with strategy='apply'. The input alias agy always "
            "serializes as antigravity. Legacy orch_dispatch remains supported."
        ),
    )

    @server.tool(
        description=(
            "List the agent CLIs installed on this machine, what each can report, and the "
            "kind-to-engine routing table. Call this before dispatching so you know which "
            "engines are actually available."
        )
    )
    def orch_engines() -> dict[str, Any]:
        capabilities = probe_all(refresh=True)
        table = load_routing_table(config.routing_path)
        return {
            "engines": [entry.describe() for entry in capabilities.values()],
            "routing": table.describe(),
            "routing_file": str(config.routing_path) if config.routing_path.exists() else None,
            "kinds": [kind.value for kind in TaskKind],
        }

    @server.tool(
        description=(
            "Queue a task and start it in a fresh git worktree. Returns immediately with a "
            "task_id — it does not wait for the work. `kind` selects the engine "
            "(implement/refactor/test go to codex, review/investigate to grok, ui_verify to "
            "antigravity); pass `engine` only to override that. Dispatch the same work "
            "twice with different engines and a shared `parent_id` to compare them."
        )
    )
    def orch_dispatch(
        repo: str,
        task: str,
        kind: str = "implement",
        risk: str = "normal",
        engine: str | None = None,
        priority: str = "normal",
        base_ref: str | None = None,
        parent_id: str | None = None,
    ) -> dict[str, Any]:
        dispatched = dispatch_task(
            config,
            _store(config),
            repo=repo,
            task=task,
            kind=_parse(TaskKind, kind, TaskKind.IMPLEMENT),
            risk=_parse(Risk, risk, Risk.NORMAL),
            priority=_parse(Priority, priority, Priority.NORMAL),
            engine=_parse_engine(engine),
            parent_id=parent_id,
            base_ref=base_ref,
        )
        return {
            **_serialize(config, dispatched.task),
            "branch": dispatched.branch,
            "engine": dispatched.engine.value,
            "worker_pid": dispatched.worker_pid,
        }

    @server.tool(
        description=(
            "Current state of a task, with the tail of its live log. Use this to check on "
            "long-running work without blocking."
        )
    )
    def orch_status(task_id: str) -> dict[str, Any]:
        task = _require(config, task_id)
        log_dir = config.logs_dir / task_id
        return {
            **_serialize(config, task),
            "elapsed_s": round((task.updated_at - task.created_at).total_seconds(), 1),
            "stdout_tail": _tail(log_dir / "stdout.log"),
            "stderr_tail": _tail(log_dir / "stderr.log"),
        }

    @server.tool(
        description=(
            f"Block until the given tasks finish, up to {int(MAX_WAIT_S)} seconds. This is "
            "the join point after dispatching several tasks in parallel. Tasks still "
            "running when the timeout expires are reported as pending, not cancelled."
        )
    )
    async def orch_wait(task_ids: list[str], timeout_s: float = 60.0) -> dict[str, Any]:
        deadline = time.monotonic() + wait_budget(timeout_s)
        store = _store(config)

        pending = set(task_ids)
        finished: dict[str, dict[str, Any]] = {}
        while pending and time.monotonic() < deadline:
            for task_id in sorted(pending):
                task = store.get_task(task_id)
                if task is None:
                    finished[task_id] = {"task_id": task_id, "status": "not_found"}
                    pending.discard(task_id)
                elif task.status in TERMINAL_STATUSES:
                    finished[task_id] = _serialize(config, task)
                    pending.discard(task_id)
            if pending:
                await asyncio.sleep(POLL_INTERVAL_S)

        return {
            "finished": [finished[key] for key in sorted(finished)],
            "still_running": sorted(pending),
            "timed_out": bool(pending),
        }

    @server.tool(
        description=(
            "Full outcome of a finished task: the engine's summary, structured findings "
            "when it produced any, token usage, cost, which files changed, and warnings "
            "from the post-run credential scan."
        )
    )
    def orch_result(task_id: str) -> dict[str, Any]:
        task = _require(config, task_id)
        path = config.logs_dir / task_id / "result.json"
        if not path.exists():
            return {
                **_serialize(config, task),
                "note": "no result yet; the task has not finished",
            }
        return {**_serialize(config, task), "result": json.loads(path.read_text(encoding="utf-8"))}

    @server.tool(
        description=(
            "The patch a task produced, as unified diff text. Read this before adopting "
            "anything. Large patches are truncated; the full file path is always returned."
        )
    )
    def orch_diff(task_id: str, max_bytes: int = DEFAULT_DIFF_BYTES) -> dict[str, Any]:
        task = _require(config, task_id)
        path = config.logs_dir / task_id / "diff.patch"

        # A task still running has no saved patch yet, but its worktree already has the
        # changes, so capture them live rather than reporting nothing.
        if not path.exists() and task.workspace_path and Path(task.workspace_path).exists():
            save_git_diff(config, task_id, Path(task.workspace_path))

        if not path.exists():
            return {"task_id": task_id, "diff": "", "note": "no diff captured yet"}

        text = path.read_text(encoding="utf-8", errors="replace")
        truncated = len(text.encode("utf-8")) > max_bytes
        return {
            "task_id": task_id,
            "diff_path": str(path),
            "diff": text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore"),
            "truncated": truncated,
            "branch": task.branch_name,
        }

    @server.tool(description="List tasks, optionally filtered by status or by parent_id.")
    def orch_list(status: str | None = None, parent_id: str | None = None) -> dict[str, Any]:
        store = _store(config)
        parsed = _parse(TaskStatus, status) if status else None
        tasks = store.list_tasks(status=parsed, parent_id=parent_id)
        return {"tasks": [_serialize(config, task) for task in tasks]}

    @server.tool(
        description=(
            "Stop a running task. The worktree and any partial changes are left in place "
            "so you can still inspect what it did."
        )
    )
    def orch_cancel(task_id: str) -> dict[str, Any]:
        task = _require(config, task_id)
        if task.status in TERMINAL_STATUSES:
            return {"task_id": task_id, "status": task.status.value, "note": "already finished"}

        killed = _kill_worker(config, task_id)
        _store(config).set_status(task_id, TaskStatus.BLOCKED, "cancelled by the orchestrator")
        return {"task_id": task_id, "status": TaskStatus.BLOCKED.value, "killed": killed}

    @server.tool(
        description=(
            "Take a task's work out of its worktree. strategy='patch' (the default) only "
            "returns the patch and changes nothing. strategy='apply' writes it into the "
            "real repository's working tree, and refuses if that tree has uncommitted "
            "changes. Nothing is ever committed or pushed for you."
        )
    )
    def orch_adopt(task_id: str, strategy: str = "patch") -> dict[str, Any]:
        task = _require(config, task_id)
        diff_path = config.logs_dir / task_id / "diff.patch"
        if not diff_path.exists() or not diff_path.read_text(encoding="utf-8").strip():
            return {"task_id": task_id, "applied": False, "note": "this task produced no changes"}

        if strategy == "patch":
            return {
                "task_id": task_id,
                "applied": False,
                "patch_path": str(diff_path),
                "branch": task.branch_name,
                "note": "read-only; call again with strategy='apply' to write to the repo",
            }

        if strategy != "apply":
            raise DispatchError(f"unknown strategy {strategy!r}; use 'patch' or 'apply'")

        repo = Path(task.repo_path)
        dirty = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
        )
        if dirty.stdout.strip():
            return {
                "task_id": task_id,
                "applied": False,
                "note": "the repository has uncommitted changes; commit or stash them first",
            }

        applied = subprocess.run(
            ["git", "-C", str(repo), "apply", "--index", str(diff_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if applied.returncode != 0:
            return {
                "task_id": task_id,
                "applied": False,
                "error": applied.stderr.strip() or "git apply failed",
            }
        return {
            "task_id": task_id,
            "applied": True,
            "repo": str(repo),
            "note": "staged in the working tree; review and commit it yourself",
        }

    @server.tool(
        description=(
            "Create an open persistent Run for one repository. routes maps task kinds to "
            "primary engines. fallbacks maps the same explicitly routed kinds to strict, "
            "non-empty ordered fallback lists; omit a kind for snapshotted automatic "
            "fallbacks. Inputs accept agy, while output uses antigravity."
        )
    )
    def orch_run_create(
        repo: str,
        routes: dict[str, str] | None = None,
        fallbacks: dict[str, list[str]] | None = None,
    ) -> dict[str, Any]:
        store = _store(config)
        overrides = _parse_workflow_routes(routes, fallbacks)
        details = _workflow_call(create_run, config, store, repo=repo, routes=overrides)
        return _workflow_details(config, details)

    @server.tool(
        description=(
            "Add and start one task under an open Run. The immutable route snapshot "
            "selects its engine. Returns immediately; inspect spawn_error, then use "
            "orch_wait only after a worker has started."
        )
    )
    def orch_run_dispatch(
        run_id: str,
        task: str,
        kind: str = "implement",
        risk: str = "normal",
        priority: str = "normal",
        parent_id: str | None = None,
        base_ref: str | None = None,
    ) -> dict[str, Any]:
        parsed_kind = _parse(TaskKind, kind, TaskKind.IMPLEMENT)
        parsed_risk = _parse(Risk, risk, Risk.NORMAL)
        parsed_priority = _parse(Priority, priority, Priority.NORMAL)
        assert isinstance(parsed_kind, TaskKind)
        assert isinstance(parsed_risk, Risk)
        assert isinstance(parsed_priority, Priority)
        dispatched = _workflow_call(
            dispatch_run,
            config,
            _store(config),
            run_id=run_id,
            task=task,
            kind=parsed_kind,
            risk=parsed_risk,
            priority=parsed_priority,
            parent_id=parent_id,
            base_ref=base_ref,
        )
        return _workflow_dispatch(config, dispatched)

    @server.tool(
        description=(
            "Close an open Run to further additions. Tasks already running continue and "
            "remain available through the normal task inspection tools."
        )
    )
    def orch_run_close(run_id: str) -> dict[str, Any]:
        store = _store(config)
        details = _workflow_call(close_workflow, store, run_id)
        return _workflow_details(config, details)

    @server.tool(
        description=(
            "Validate, persist, and start one sealed Batch of independent tasks. tasks is "
            "a non-empty ordered array of objects with task plus optional kind, risk, "
            "priority, parent_id, and base_ref. routes/fallbacks match orch_run_create. "
            "The response includes ordered task_ids for orch_wait and a per-task "
            "spawn_error when a member remained queued instead of starting."
        )
    )
    def orch_batch_dispatch(
        repo: str,
        tasks: list[dict[str, Any]],
        routes: dict[str, str] | None = None,
        fallbacks: dict[str, list[str]] | None = None,
    ) -> dict[str, Any]:
        if not tasks:
            raise DispatchError("a batch must contain at least one task")
        requests = tuple(
            _workflow_task_request(task, where=f"tasks[{index}]")
            for index, task in enumerate(tasks)
        )
        overrides = _parse_workflow_routes(routes, fallbacks)
        store = _store(config)
        batch = _workflow_call(
            dispatch_batch,
            config,
            store,
            repo=repo,
            tasks=requests,
            routes=overrides,
        )
        return _workflow_batch(config, store, batch)

    @server.tool(
        description="List saved Run and Batch workflows; pass run or batch to filter."
    )
    def orch_workflow_list(workflow_type: str | None = None) -> dict[str, Any]:
        parsed = _parse(WorkflowType, workflow_type) if workflow_type else None
        workflows = _workflow_call(list_workflows, _store(config), parsed)
        return {"workflows": [_workflow_summary(workflow) for workflow in workflows]}

    @server.tool(
        description=(
            "Show a Run or Batch by workflow id, including its ordered full route snapshot "
            "and member tasks."
        )
    )
    def orch_workflow_show(workflow_id: str) -> dict[str, Any]:
        details = _workflow_call(show_workflow, _store(config), workflow_id)
        return _workflow_details(config, details)

    return server


def _require(config: Config, task_id: str) -> Task:
    task = _store(config).get_task(task_id)
    if task is None:
        raise DispatchError(f"task not found: {task_id}")
    return task


def _kill_worker(config: Config, task_id: str) -> bool:
    path = pid_path(config, task_id)
    if not path.exists():
        return False
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except ValueError:
        return False
    try:
        # The worker starts its own session, so the engine and any shell it spawned are
        # in this group too.
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser(prog="agentmcp")
    parser.add_argument("--runtime-root", default=None)
    parser.add_argument("--routing", default=None)
    args = parser.parse_args()

    config = load_config(args.runtime_root, args.routing)
    TaskStore(config)  # create the runtime tree and migrate before serving
    build_server(config).run("stdio")


if __name__ == "__main__":
    main()

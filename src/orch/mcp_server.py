"""MCP control plane for the orchestrator.

Claude Code is the orchestrator; this server is how it reaches the other engines.

It deliberately does no work itself. `orch_dispatch` writes a row and spawns a detached
`orch daemon run-task`, then returns immediately. That keeps three properties worth having:

- a tool call never blocks Claude for the 30 minutes a real task can take,
- work survives the Claude session exiting, since the worker is not a child of it,
- `orch`, the HTTP API and the React UI all see the same tasks, because SQLite
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
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from mcp.server import MCPServer

from orch.config import Config, load_config
from orch.db import TaskStore
from orch.dispatch import DispatchError, dispatch_task, pid_path
from orch.engines import probe_all
from orch.models import (
    TERMINAL_STATUSES,
    Engine,
    Priority,
    Risk,
    Task,
    TaskKind,
    TaskStatus,
    WorkflowRouteOverride,
    WorkflowTaskRequest,
    WorkflowType,
)
from orch.parsing import (
    parse_engine,
    parse_enum,
    parse_iso_datetime,
    route_overrides,
    task_request,
)
from orch.router import load_routing_table
from orch.result import save_git_diff
from orch.stats import build_stats
from orch.usage import collect_usage
from orch.views import (
    dispatched_batch,
    dispatched_detail,
    dispatched_task,
    task_detail,
    workflow_details,
    workflow_summary,
)
from orch.workflows import (
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


def _store(config: Config) -> TaskStore:
    return TaskStore(config)


def wait_budget(timeout_s: float) -> float:
    """Clamp a requested wait so a stuck task cannot wedge the caller indefinitely."""
    return max(1.0, min(float(timeout_s), MAX_WAIT_S))


# The validation itself lives in `parsing`, shared with `orch`, and raises
# `WorkflowError`. These wrappers exist only to keep this server's outward error type
# `DispatchError` — an MCP caller should not have to know which module rejected it.
def _parse[T: Enum](enum: type[T], value: str | None, default: T | None = None) -> T | None:
    return _workflow_call(parse_enum, enum, value, default)


def _parse_engine(value: str | None) -> Engine | None:
    return _workflow_call(parse_engine, value)


def _parse_iso_datetime(value: str | None, field: str) -> datetime | None:
    return _workflow_call(parse_iso_datetime, value, field)


def _parse_workflow_routes(
    routes: Mapping[str, str] | None,
    fallbacks: Mapping[str, Sequence[str]] | None,
) -> dict[TaskKind, WorkflowRouteOverride]:
    return _workflow_call(route_overrides, routes, fallbacks)


def _workflow_task_request(value: object, *, where: str) -> WorkflowTaskRequest:
    return _workflow_call(task_request, value, where=where)


def _workflow_call[T](call: Any, *args: Any, **kwargs: Any) -> T:
    try:
        return call(*args, **kwargs)
    except (WorkflowError, ValueError) as exc:
        raise DispatchError(str(exc)) from None


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
            "colliding. Call orch_engines first — and orch_usage when the plan is large "
            "enough that an exhausted subscription would matter — then confirm Run versus "
            "Batch and the primary/fallback routes with the user. Use orch_run_create and "
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
            "Show how much of each engine's account quota is already spent and when it "
            "resets, read from the files the engines themselves write. Call it before "
            "routing a large batch. Every reading carries observed_at and age_seconds "
            "because these are cached snapshots, not live figures — claude's is only "
            "refreshed while Claude Code runs. antigravity reports no quota at all, and "
            "says so in its notes rather than being omitted. This is account-wide usage; "
            "orch_stats covers what the tasks in this database spent."
        )
    )
    def orch_usage() -> dict[str, Any]:
        return collect_usage().describe()

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
        return dispatched_detail(config, dispatched)

    @server.tool(
        description=(
            "Current state of a task, with the tail of its live log. Use this to check on "
            "long-running work without blocking."
        )
    )
    def orch_status(task_id: str) -> dict[str, Any]:
        task = _require(config, task_id)
        log_dir = config.logs_dir / task_id
        # Older rows have no started_at, so derived durations stay null instead of
        # silently relabelling queue time as execution time.
        queue_wait_s = (
            round((task.started_at - task.created_at).total_seconds(), 1)
            if task.started_at is not None
            else None
        )
        if task.started_at is None:
            elapsed_s = None
        elif task.status in TERMINAL_STATUSES:
            elapsed_s = (
                round((task.finished_at - task.started_at).total_seconds(), 1)
                if task.finished_at is not None
                else None
            )
        else:
            elapsed_s = round((datetime.now(UTC) - task.started_at).total_seconds(), 1)
        return {
            **task_detail(config, task),
            "queue_wait_s": queue_wait_s,
            "elapsed_s": elapsed_s,
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
                    finished[task_id] = task_detail(config, task)
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
                **task_detail(config, task),
                "note": "no result yet; the task has not finished",
            }
        return {**task_detail(config, task), "result": json.loads(path.read_text(encoding="utf-8"))}

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
        return {"tasks": [task_detail(config, task) for task in tasks]}

    @server.tool(
        description=(
            "Aggregate task outcomes, token usage, durations, and change counts, with "
            "optional filters and grouping. The cost total covers only engines that "
            "report cost; coverage counts and engines without cost are always included."
        )
    )
    def orch_stats(
        repo: str | None = None,
        workflow_id: str | None = None,
        engine: str | None = None,
        kind: str | None = None,
        since: str | None = None,
        until: str | None = None,
        group_by: str | None = None,
    ) -> dict[str, Any]:
        parsed_kind = _parse(TaskKind, kind) if kind else None
        tasks = _store(config).list_tasks(
            engine=_parse_engine(engine),
            kind=parsed_kind,
            repo_path=Path(repo) if repo else None,
            since=_parse_iso_datetime(since, "since"),
            until=_parse_iso_datetime(until, "until"),
            workflow_id=workflow_id,
        )
        try:
            return build_stats(tasks, group_by=group_by).describe()
        except ValueError as exc:
            raise DispatchError(str(exc)) from None

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
        return workflow_details(config, details)

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
        return dispatched_task(config, dispatched)

    @server.tool(
        description=(
            "Close an open Run to further additions. Tasks already running continue and "
            "remain available through the normal task inspection tools."
        )
    )
    def orch_run_close(run_id: str) -> dict[str, Any]:
        store = _store(config)
        details = _workflow_call(close_workflow, store, run_id)
        return workflow_details(config, details)

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
        return dispatched_batch(config, store, batch)

    @server.tool(
        description="List saved Run and Batch workflows; pass run or batch to filter."
    )
    def orch_workflow_list(workflow_type: str | None = None) -> dict[str, Any]:
        parsed = _parse(WorkflowType, workflow_type) if workflow_type else None
        workflows = _workflow_call(list_workflows, _store(config), parsed)
        return {"workflows": [workflow_summary(workflow) for workflow in workflows]}

    @server.tool(
        description=(
            "Show a Run or Batch by workflow id, including its ordered full route snapshot "
            "and member tasks."
        )
    )
    def orch_workflow_show(workflow_id: str) -> dict[str, Any]:
        details = _workflow_call(show_workflow, _store(config), workflow_id)
        return workflow_details(config, details)

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


def serve(runtime_root: str | None = None, routing: str | None = None) -> None:
    """Serve MCP over stdio. Shared by `orch mcp` and `agentmcp`."""
    config = load_config(runtime_root, routing)
    TaskStore(config)  # create the runtime tree and migrate before serving
    build_server(config).run("stdio")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="agentmcp", description="Deprecated alias for `orch mcp`."
    )
    parser.add_argument("--runtime-root", default=None, metavar="PATH")
    parser.add_argument("--routing", default=None, metavar="PATH")
    args = parser.parse_args()
    serve(args.runtime_root, args.routing)


if __name__ == "__main__":
    main()

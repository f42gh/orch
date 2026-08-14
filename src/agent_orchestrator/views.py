"""The JSON shapes `agentctl --json` and the MCP tools both return.

These used to exist twice, once in `cli.py` and once in `mcp_server.py`, several of
them byte-identical. The one place they had genuinely diverged was the task inside a
workflow: the CLI returned eleven fields, the MCP server returned everything. That
difference was not a decision — a caller reading `run show --json` had no way to see
cost, tokens or timings that the same workflow reported over MCP.

`Dispatched.describe` stays in `dispatch.py` and is deliberately not merged into
`task_detail`: it is the payload CAGE parses out of `agentctl dispatch --json`, into a
struct whose fields are all required.
"""

from __future__ import annotations

from typing import Any

from agent_orchestrator.config import Config
from agent_orchestrator.db import TaskStore
from agent_orchestrator.models import Task, Workflow, WorkflowDetails
from agent_orchestrator.stats import summarize
from agent_orchestrator.workflows import (
    DispatchedBatch,
    DispatchedWorkflowTask,
    show_workflow,
)


def workflow_summary(workflow: Workflow) -> dict[str, Any]:
    return {
        "workflow_id": workflow.id,
        "type": workflow.workflow_type.value,
        "status": workflow.status.value,
        "repo": str(workflow.repo_path),
        "created_at": workflow.created_at.isoformat(),
        "closed_at": workflow.closed_at.isoformat() if workflow.closed_at else None,
    }


def task_detail(config: Config, task: Task) -> dict[str, Any]:
    log_dir = config.logs_dir / task.id
    tokens = task.tokens
    return {
        "task_id": task.id,
        "status": task.status.value,
        "kind": task.kind.value,
        "engine": task.engine.value if task.engine else None,
        "model": task.model,
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
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
        "engine_ms": task.engine_ms,
        "plan_type": task.plan_type,
        "quota_used_pct": task.quota_used_pct,
        "quota_window_minutes": task.quota_window_minutes,
        "quota_resets_at": (task.quota_resets_at.isoformat() if task.quota_resets_at else None),
        "files_changed": task.files_changed,
        "insertions": task.insertions,
        "deletions": task.deletions,
        "tokens": (
            {
                "input_tokens": tokens.input_tokens,
                "output_tokens": tokens.output_tokens,
                "cache_read_tokens": tokens.cache_read_tokens,
                "cache_write_tokens": tokens.cache_write_tokens,
                "reasoning_tokens": tokens.reasoning_tokens,
                "total": tokens.total,
            }
            if tokens is not None
            else None
        ),
        "log_path": str(log_dir),
        "diff_path": str(log_dir / "diff.patch"),
        "result_summary": task.result_summary,
        "error": task.error,
    }


def workflow_details(config: Config, details: WorkflowDetails) -> dict[str, Any]:
    routes = [
        {
            "kind": route.kind.value,
            "primary": route.primary.value,
            "fallback_mode": route.fallback_mode.value,
            "fallbacks": [engine.value for engine in route.fallbacks],
        }
        for route in details.routes
    ]
    tasks = [
        {**task_detail(config, task), "ordinal": membership.ordinal}
        for membership, task in zip(details.memberships, details.tasks, strict=True)
    ]
    return {
        **workflow_summary(details.workflow),
        "routes": routes,
        "task_ids": [task["task_id"] for task in tasks],
        "tasks": tasks,
        "totals": summarize(details.tasks).describe(),
    }


def dispatched_task(config: Config, item: DispatchedWorkflowTask) -> dict[str, Any]:
    return {
        **item.dispatched.describe(config),
        "workflow_id": item.workflow.id,
        "ordinal": item.membership.ordinal,
        "task": item.task.task,
        "spawn_error": item.spawn_error,
    }


def dispatched_batch(
    config: Config, store: TaskStore, batch: DispatchedBatch
) -> dict[str, Any]:
    details = show_workflow(store, batch.workflow.id)
    tasks = [dispatched_task(config, item) for item in batch.dispatched]
    return {
        **workflow_details(config, details),
        "task_ids": [task["task_id"] for task in tasks],
        "tasks": tasks,
    }

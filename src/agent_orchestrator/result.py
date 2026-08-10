from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from agent_orchestrator.config import Config
from agent_orchestrator.logging_utils import task_log_dir, write_json
from agent_orchestrator.models import Engine, Task, TaskStatus


def _git(workspace_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(workspace_path), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def save_git_diff(config: Config, task_id: str, workspace_path: Path) -> Path:
    """Capture everything the agent changed, including files it newly created.

    `git diff` alone reports neither staged nor untracked changes, and creating new
    files is the single most common thing these agents do — so an unqualified diff
    silently loses most of the work. Registering untracked paths with --intent-to-add
    first, then diffing against HEAD, covers all three cases.
    """
    log_dir = task_log_dir(config, task_id)
    diff_path = log_dir / "diff.patch"

    _git(workspace_path, "add", "--all", "--intent-to-add", ".")
    result = _git(workspace_path, "diff", "HEAD")
    diff_path.write_text(
        result.stdout if result.returncode == 0 else result.stderr,
        encoding="utf-8",
    )
    return diff_path


def diff_stat(workspace_path: Path) -> str:
    result = _git(workspace_path, "diff", "--stat", "HEAD")
    return result.stdout.strip() if result.returncode == 0 else ""


def changed_files(workspace_path: Path) -> list[str]:
    result = _git(workspace_path, "diff", "--name-only", "HEAD")
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def write_result_json(
    config: Config,
    task: Task,
    status: TaskStatus,
    summary: str,
    commands_run: list[str] | None = None,
    warnings: list[str] | None = None,
    human_review_required: bool = True,
    engine: Engine | None = None,
    usage: dict[str, Any] | None = None,
    cost_usd: float | None = None,
    structured: dict[str, Any] | None = None,
) -> Path:
    log_dir = task_log_dir(config, task.id)
    diff_path = log_dir / "diff.patch"
    result_path = log_dir / "result.json"
    workspace = Path(task.workspace_path) if task.workspace_path else None
    write_json(
        result_path,
        {
            "task_id": task.id,
            "status": status.value,
            "kind": task.kind.value,
            "engine": (engine or task.engine).value if (engine or task.engine) else None,
            "workspace_path": str(task.workspace_path) if task.workspace_path else None,
            "branch_name": task.branch_name,
            "diff_path": str(diff_path),
            "diffstat": diff_stat(workspace) if workspace and workspace.exists() else "",
            "changed_files": changed_files(workspace) if workspace and workspace.exists() else [],
            "summary": summary,
            "structured": structured,
            "usage": usage,
            "cost_usd": cost_usd,
            "commands_run": commands_run or [],
            "warnings": warnings or [],
            "human_review_required": human_review_required,
        },
    )
    return result_path

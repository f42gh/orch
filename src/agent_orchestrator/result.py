from __future__ import annotations

import subprocess
from pathlib import Path

from agent_orchestrator.config import Config
from agent_orchestrator.logging_utils import task_log_dir, write_json
from agent_orchestrator.models import Task, TaskStatus


def save_git_diff(config: Config, task_id: str, workspace_path: Path) -> Path:
    log_dir = task_log_dir(config, task_id)
    diff_path = log_dir / "diff.patch"
    result = subprocess.run(
        ["git", "-C", str(workspace_path), "diff", "--"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode == 0:
        diff_path.write_text(result.stdout, encoding="utf-8")
    else:
        diff_path.write_text(result.stderr, encoding="utf-8")
    return diff_path


def write_result_json(
    config: Config,
    task: Task,
    status: TaskStatus,
    summary: str,
    commands_run: list[str] | None = None,
    warnings: list[str] | None = None,
    human_review_required: bool = True,
) -> Path:
    log_dir = task_log_dir(config, task.id)
    diff_path = log_dir / "diff.patch"
    result_path = log_dir / "result.json"
    write_json(
        result_path,
        {
            "task_id": task.id,
            "status": status.value,
            "workspace_path": str(task.workspace_path) if task.workspace_path else None,
            "diff_path": str(diff_path),
            "summary": summary,
            "commands_run": commands_run or [],
            "warnings": warnings or [],
            "human_review_required": human_review_required,
        },
    )
    return result_path

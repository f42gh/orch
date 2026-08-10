from __future__ import annotations

import subprocess
from pathlib import Path

from agent_orchestrator.config import Config, ensure_runtime_dirs


class WorkspaceError(RuntimeError):
    pass


def branch_name_for_task(task_id: str) -> str:
    return f"agent/{task_id}"


def workspace_path_for_task(config: Config, task_id: str) -> Path:
    return config.workspaces_dir / task_id / "repo"


def create_workspace(config: Config, task_id: str, repo_path: Path) -> tuple[Path, str]:
    ensure_runtime_dirs(config)
    repo = repo_path.expanduser().resolve()
    if not repo.exists():
        raise WorkspaceError(f"repo does not exist: {repo}")
    if not (repo / ".git").exists():
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            raise WorkspaceError(f"not a git repository: {repo}")

    workspace = workspace_path_for_task(config, task_id)
    branch = branch_name_for_task(task_id)
    if workspace.exists():
        return workspace, branch

    workspace.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", str(workspace), "-b", branch],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise WorkspaceError(result.stderr.strip() or result.stdout.strip() or "git worktree add failed")
    return workspace, branch

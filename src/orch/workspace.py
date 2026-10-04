from __future__ import annotations

import subprocess
from pathlib import Path

from orch.config import Config, ensure_runtime_dirs
from orch.models import Engine


class WorkspaceError(RuntimeError):
    pass


def branch_name_for_task(task_id: str, engine: Engine | None = None) -> str:
    """Name the branch after the engine so parallel attempts at one task stay distinct.

    Without the engine segment, dispatching the same task to codex and grok to compare
    their diffs would collide on one branch name.
    """
    if engine is None:
        return f"agent/{task_id}"
    return f"agent/{engine.value}/{task_id}"


def workspace_path_for_task(config: Config, task_id: str) -> Path:
    return config.workspaces_dir / task_id / "repo"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def create_workspace(
    config: Config,
    task_id: str,
    repo_path: Path,
    engine: Engine | None = None,
    base_ref: str | None = None,
) -> tuple[Path, str]:
    """Create an isolated git worktree for one task.

    The worktree is the containment that actually holds: whatever an engine does, it
    does on its own branch, and the original checkout is never touched.
    """
    ensure_runtime_dirs(config)
    repo = repo_path.expanduser().resolve()
    if not repo.exists():
        raise WorkspaceError(f"repo does not exist: {repo}")
    if not (repo / ".git").exists():
        result = _git(repo, "rev-parse", "--show-toplevel")
        if result.returncode != 0:
            raise WorkspaceError(f"not a git repository: {repo}")

    workspace = workspace_path_for_task(config, task_id)
    branch = branch_name_for_task(task_id, engine)
    if workspace.exists():
        return workspace, branch

    workspace.parent.mkdir(parents=True, exist_ok=True)
    args = ["worktree", "add", str(workspace), "-b", branch]
    if base_ref:
        args.append(base_ref)
    result = _git(repo, *args)
    if result.returncode != 0:
        raise WorkspaceError(
            result.stderr.strip() or result.stdout.strip() or "git worktree add failed"
        )
    return workspace, branch


def remove_workspace(
    config: Config,
    task_id: str,
    repo_path: Path,
    branch_name: str | None = None,
    delete_branch: bool = False,
) -> None:
    """Tear a worktree down. Parallel dispatch accumulates these quickly.

    Deleting the branch is opt-in because the branch is the only place the work
    survives once the worktree is gone.
    """
    repo = repo_path.expanduser().resolve()
    workspace = workspace_path_for_task(config, task_id)
    if workspace.exists():
        result = _git(repo, "worktree", "remove", "--force", str(workspace))
        if result.returncode != 0:
            raise WorkspaceError(result.stderr.strip() or "git worktree remove failed")
    _git(repo, "worktree", "prune")

    if delete_branch and branch_name:
        _git(repo, "branch", "-D", branch_name)

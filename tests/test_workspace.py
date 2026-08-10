from pathlib import Path
import subprocess

import pytest

from agent_orchestrator.config import Config
from agent_orchestrator.workspace import (
    WorkspaceError,
    branch_name_for_task,
    create_workspace,
    workspace_path_for_task,
)


def init_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)
    (path / "README.md").write_text("# test\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True, stdout=subprocess.PIPE)


def test_workspace_path_and_branch_name(tmp_path: Path) -> None:
    config = Config(runtime_root=tmp_path)

    assert workspace_path_for_task(config, "task-0001") == tmp_path / "workspaces" / "task-0001" / "repo"
    assert branch_name_for_task("task-0001") == "agent/task-0001"


def test_create_workspace_with_git_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "source"
    repo.mkdir()
    init_repo(repo)
    config = Config(runtime_root=tmp_path / "runtime")

    workspace, branch = create_workspace(config, "task-0001", repo)

    assert workspace.exists()
    assert branch == "agent/task-0001"
    result = subprocess.run(
        ["git", "-C", str(workspace), "branch", "--show-current"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    assert result.stdout.strip() == "agent/task-0001"


def test_create_workspace_fails_for_missing_repo(tmp_path: Path) -> None:
    config = Config(runtime_root=tmp_path / "runtime")

    with pytest.raises(WorkspaceError):
        create_workspace(config, "task-0001", tmp_path / "missing")

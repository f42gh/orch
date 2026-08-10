"""Regression tests for what the v0 diff capture lost.

`git diff` reports neither staged nor untracked changes. Since creating new files is
the most common thing these agents do, the v0 capture silently dropped most of the work
it was supposed to hand to a reviewer.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

from agent_orchestrator.config import Config
from agent_orchestrator.models import Engine, Priority, Risk, Task, TaskKind, TaskStatus
from agent_orchestrator.result import changed_files, diff_stat, save_git_diff, write_result_json


def init_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)
    (path / "README.md").write_text("# test\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"], cwd=path, check=True, stdout=subprocess.DEVNULL
    )


def make_task(workspace: Path) -> Task:
    now = datetime.now(UTC)
    return Task(
        id="task-0001",
        repo_path=workspace,
        task="do the thing",
        risk=Risk.NORMAL,
        priority=Priority.NORMAL,
        status=TaskStatus.NEEDS_REVIEW,
        created_at=now,
        updated_at=now,
        kind=TaskKind.IMPLEMENT,
        engine=Engine.CODEX,
        workspace_path=workspace,
        branch_name="agent/codex/task-0001",
    )


def test_diff_includes_newly_created_files(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    init_repo(workspace)
    (workspace / "new_module.py").write_text("def added():\n    return 1\n", encoding="utf-8")

    diff = save_git_diff(Config(runtime_root=tmp_path / "runtime"), "task-0001", workspace)
    text = diff.read_text(encoding="utf-8")

    assert "new_module.py" in text
    assert "def added():" in text


def test_diff_includes_staged_changes(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    init_repo(workspace)
    (workspace / "README.md").write_text("# test\nstaged line\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=workspace, check=True)

    diff = save_git_diff(Config(runtime_root=tmp_path / "runtime"), "task-0001", workspace)

    assert "staged line" in diff.read_text(encoding="utf-8")


def test_diff_includes_unstaged_changes(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    init_repo(workspace)
    (workspace / "README.md").write_text("# test\nunstaged line\n", encoding="utf-8")

    diff = save_git_diff(Config(runtime_root=tmp_path / "runtime"), "task-0001", workspace)

    assert "unstaged line" in diff.read_text(encoding="utf-8")


def test_clean_workspace_produces_an_empty_diff(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    init_repo(workspace)

    diff = save_git_diff(Config(runtime_root=tmp_path / "runtime"), "task-0001", workspace)

    assert diff.read_text(encoding="utf-8").strip() == ""


def test_changed_files_and_stat_see_new_files(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    init_repo(workspace)
    (workspace / "added.py").write_text("x = 1\n", encoding="utf-8")
    save_git_diff(Config(runtime_root=tmp_path / "runtime"), "task-0001", workspace)

    assert "added.py" in changed_files(workspace)
    assert "added.py" in diff_stat(workspace)


def test_result_json_records_engine_cost_and_diffstat(tmp_path: Path) -> None:
    import json

    workspace = tmp_path / "ws"
    workspace.mkdir()
    init_repo(workspace)
    (workspace / "added.py").write_text("x = 1\n", encoding="utf-8")

    config = Config(runtime_root=tmp_path / "runtime")
    task = make_task(workspace)
    save_git_diff(config, task.id, workspace)

    path = write_result_json(
        config,
        task,
        TaskStatus.NEEDS_REVIEW,
        "added a module",
        warnings=["check this"],
        usage={"input_tokens": 10},
        cost_usd=0.25,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["engine"] == "codex"
    assert payload["kind"] == "implement"
    assert payload["cost_usd"] == 0.25
    assert payload["branch_name"] == "agent/codex/task-0001"
    assert "added.py" in payload["changed_files"]
    assert "added.py" in payload["diffstat"]
    assert payload["warnings"] == ["check this"]

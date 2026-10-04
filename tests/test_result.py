"""Regression tests for what the v0 diff capture lost.

`git diff` reports neither staged nor untracked changes. Since creating new files is
the most common thing these agents do, the v0 capture silently dropped most of the work
it was supposed to hand to a reviewer.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

from orch.config import Config
from orch.models import Engine, Priority, Risk, Task, TaskKind, TaskStatus
from orch.result import (
    changed_files,
    diff_numstat,
    diff_stat,
    save_git_diff,
    write_result_json,
)


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


def test_numstat_counts_created_modified_and_deleted_files(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    init_repo(workspace)
    (workspace / "removed.txt").write_text("one\ntwo\n", encoding="utf-8")
    subprocess.run(["git", "add", "removed.txt"], cwd=workspace, check=True)
    subprocess.run(
        ["git", "commit", "-m", "add removable file"],
        cwd=workspace,
        check=True,
        stdout=subprocess.DEVNULL,
    )

    (workspace / "README.md").write_text("# changed\n", encoding="utf-8")
    (workspace / "added.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    (workspace / "removed.txt").unlink()

    stat = diff_numstat(workspace)

    assert stat.files_changed == 3
    assert stat.insertions == 3
    assert stat.deletions == 3
    assert set(stat.files) == {"README.md", "added.py", "removed.txt"}


def test_numstat_counts_binary_files_without_line_totals(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    init_repo(workspace)
    (workspace / "image.bin").write_bytes(b"\x00\x01binary payload")

    stat = diff_numstat(workspace)

    assert stat.files_changed == 1
    assert stat.insertions == 0
    assert stat.deletions == 0
    assert stat.files == ("image.bin",)


def test_changed_files_and_stat_see_new_files(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    init_repo(workspace)
    (workspace / "removed.txt").write_text("remove me\n", encoding="utf-8")
    subprocess.run(["git", "add", "removed.txt"], cwd=workspace, check=True)
    subprocess.run(
        ["git", "commit", "-m", "add removable file"],
        cwd=workspace,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    (workspace / "README.md").write_text("# modified\n", encoding="utf-8")
    (workspace / "added.py").write_text("x = 1\n", encoding="utf-8")
    (workspace / "removed.txt").unlink()
    save_git_diff(Config(runtime_root=tmp_path / "runtime"), "task-0001", workspace)

    assert changed_files(workspace) == ["README.md", "added.py", "removed.txt"]
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
    assert payload["changed_files"] == ["added.py"]
    assert isinstance(payload["changed_files"], list)
    assert isinstance(payload["diffstat"], str)
    assert "added.py" in payload["diffstat"]
    assert payload["diff_numstat"] == {
        "files_changed": 1,
        "insertions": 1,
        "deletions": 0,
    }
    assert payload["warnings"] == ["check this"]


def test_test_run_bytecode_stays_out_of_the_review_diff(tmp_path: Path) -> None:
    """Observed in a real run: an engine ran the tests it wrote and the .pyc files
    landed in the diff as binary blobs a reviewer then had to wade through."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    init_repo(workspace)
    (workspace / "real_change.py").write_text("x = 1\n", encoding="utf-8")
    cache = workspace / "__pycache__"
    cache.mkdir()
    (cache / "calc.cpython-312.pyc").write_bytes(b"\x00\x01binary")
    (workspace / ".pytest_cache").mkdir()
    (workspace / ".pytest_cache" / "lastfailed").write_text("{}", encoding="utf-8")

    diff = save_git_diff(Config(runtime_root=tmp_path / "runtime"), "task-0001", workspace)
    text = diff.read_text(encoding="utf-8")

    assert "real_change.py" in text
    assert "__pycache__" not in text
    assert ".pytest_cache" not in text


def test_tracked_files_are_never_filtered_out(tmp_path: Path) -> None:
    """The exclusions only apply to the untracked sweep; a repo that tracks a path
    still gets its changes reviewed."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    init_repo(workspace)
    vendored = workspace / "node_modules"
    vendored.mkdir()
    (vendored / "patched.js").write_text("original\n", encoding="utf-8")
    subprocess.run(["git", "add", "-f", "node_modules/patched.js"], cwd=workspace, check=True)
    subprocess.run(
        ["git", "commit", "-m", "vendor"], cwd=workspace, check=True, stdout=subprocess.DEVNULL
    )
    (vendored / "patched.js").write_text("patched by the agent\n", encoding="utf-8")

    diff = save_git_diff(Config(runtime_root=tmp_path / "runtime"), "task-0001", workspace)

    assert "patched by the agent" in diff.read_text(encoding="utf-8")


def test_capturing_a_diff_does_not_touch_the_worktree_index(tmp_path: Path) -> None:
    """Staging into the real index makes the exclusions stop applying on the next call,
    and leaves a human opening the worktree with a staged mess the agent never made."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    init_repo(workspace)
    (workspace / "added.py").write_text("x = 1\n", encoding="utf-8")
    config = Config(runtime_root=tmp_path / "runtime")

    save_git_diff(config, "task-0001", workspace)

    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=workspace, capture_output=True, text=True, check=True
    )
    assert status.stdout.strip() == "?? added.py"  # still untracked, nothing staged


def test_exclusions_still_apply_on_a_second_capture(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    init_repo(workspace)
    (workspace / "real.py").write_text("x = 1\n", encoding="utf-8")
    (workspace / "__pycache__").mkdir()
    (workspace / "__pycache__" / "real.cpython-312.pyc").write_bytes(b"\x00binary")
    config = Config(runtime_root=tmp_path / "runtime")

    save_git_diff(config, "task-0001", workspace)
    diff = save_git_diff(config, "task-0001", workspace)

    text = diff.read_text(encoding="utf-8")
    assert "real.py" in text
    assert "__pycache__" not in text


def test_numstat_reports_the_new_path_for_a_rename(tmp_path: Path) -> None:
    """`git diff --numstat` without -z renders a rename as "old => new", naming no file.

    changed_files() derives from numstat, and its consumers expect a path they can open.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    init_repo(workspace)
    (workspace / "old.py").write_text("a\nb\nc\nd\ne\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=workspace, check=True)
    subprocess.run(
        ["git", "commit", "-m", "add old"], cwd=workspace, check=True, stdout=subprocess.DEVNULL
    )
    subprocess.run(["git", "mv", "old.py", "new.py"], cwd=workspace, check=True)
    (workspace / "new.py").write_text("a\nb\nc\nd\nf\n", encoding="utf-8")

    stat = diff_numstat(workspace)

    assert stat.files == ("new.py",)
    assert "=>" not in stat.files[0]
    assert changed_files(workspace) == ["new.py"]
    assert (stat.insertions, stat.deletions) == (1, 1)


def test_numstat_handles_a_path_containing_a_space(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    init_repo(workspace)
    (workspace / "two words.txt").write_text("hello\n", encoding="utf-8")

    stat = diff_numstat(workspace)

    assert "two words.txt" in stat.files

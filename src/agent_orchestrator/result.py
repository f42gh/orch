from __future__ import annotations

import os
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from agent_orchestrator.config import Config
from agent_orchestrator.logging_utils import task_log_dir, write_json
from agent_orchestrator.models import Engine, Task, TaskStatus


#: Machine-generated caches that agents create as a side effect of running tests.
#:
#: Observed in a real run: grok ran the test suite it wrote, and the resulting .pyc
#: files landed in the review diff as binary blobs. These are never hand-edited, so
#: excluding them from the untracked sweep costs nothing and keeps the diff readable.
#: Anything the repository already tracks still shows up, whatever its path.
#: The `glob` magic is required, not cosmetic: without it `**/` does not match a
#: top-level directory, so `.pytest_cache/` at the repository root slips through.
NOISE_PATHSPECS: tuple[str, ...] = (
    ":(exclude,glob)**/__pycache__/**",
    ":(exclude,glob)**/*.py[co]",
    ":(exclude,glob)**/.pytest_cache/**",
    ":(exclude,glob)**/.mypy_cache/**",
    ":(exclude,glob)**/.ruff_cache/**",
    ":(exclude,glob)**/node_modules/**",
    ":(exclude,glob)**/.DS_Store",
)


def _git(
    workspace_path: Path, *args: str, index_file: Path | None = None
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if index_file is not None:
        env["GIT_INDEX_FILE"] = str(index_file)
    return subprocess.run(
        ["git", "-C", str(workspace_path), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=env,
    )


@contextmanager
def _scratch_index(workspace_path: Path) -> Iterator[Path | None]:
    """A throwaway index seeded from HEAD, so diffing never mutates the worktree.

    Registering untracked files has to happen somewhere, and doing it in the real index
    has two costs that showed up in practice: the exclusions below stop applying on any
    later call, because a path added once is tracked from then on; and a human opening
    the worktree afterwards finds a staged mess that the agent did not create.
    """
    with tempfile.TemporaryDirectory(prefix="orch-index-") as directory:
        index = Path(directory) / "index"
        seeded = _git(workspace_path, "read-tree", "HEAD", index_file=index)
        yield index if seeded.returncode == 0 else None


def save_git_diff(config: Config, task_id: str, workspace_path: Path) -> Path:
    """Capture everything the agent changed, including files it newly created.

    `git diff` alone reports neither staged nor untracked changes, and creating new
    files is the single most common thing these agents do — so an unqualified diff
    silently loses most of the work. Registering untracked paths with --intent-to-add
    first, then diffing against HEAD, covers all three cases.
    """
    diff_path = task_log_dir(config, task_id) / "diff.patch"
    result = _diff(workspace_path, "diff", "HEAD")
    diff_path.write_text(
        result.stdout if result.returncode == 0 else result.stderr,
        encoding="utf-8",
    )
    return diff_path


def diff_stat(workspace_path: Path) -> str:
    result = _diff(workspace_path, "diff", "--stat", "HEAD")
    return result.stdout.strip() if result.returncode == 0 else ""


def changed_files(workspace_path: Path) -> list[str]:
    result = _diff(workspace_path, "diff", "--name-only", "HEAD")
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def _diff(workspace_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a diff command against a scratch index that knows about new files."""
    with _scratch_index(workspace_path) as index:
        if index is None:  # no HEAD yet; fall back to whatever git can tell us
            return _git(workspace_path, *args)
        _git(
            workspace_path,
            "add",
            "--all",
            "--intent-to-add",
            "--",
            ".",
            *NOISE_PATHSPECS,
            index_file=index,
        )
        return _git(workspace_path, *args, index_file=index)


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

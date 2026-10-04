"""The MCP tools are exercised in-process; worker spawning is stubbed.

These check the control-plane contract Claude depends on: dispatch returns without
waiting, adopt does not touch the real repository unless asked, and cancel leaves the
evidence in place.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from orch.config import Config
from orch.db import TaskStore
from orch.mcp_server import MAX_WAIT_S, DispatchError, build_server, wait_budget
from orch.models import Engine, Priority, Risk, TaskKind, TaskStatus


def init_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=path, check=True)
    (path / "README.md").write_text("# test\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, stdout=subprocess.DEVNULL)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Config, Path, list[str]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)

    config = Config(runtime_root=tmp_path / "runtime", routing_path=tmp_path / "absent.toml")
    TaskStore(config)

    spawned: list[str] = []
    monkeypatch.setattr(
        "orch.dispatch.spawn_worker",
        lambda cfg, task_id: (spawned.append(task_id), 4242)[1],
    )
    # Pretend every engine is installed so routing is exercised, not the machine.
    # Dispatch resolves engines through its own module; orch_engines through this one.
    fake_probe = lambda refresh=False: {engine: _fake_caps(engine) for engine in Engine}  # noqa: E731
    monkeypatch.setattr("orch.dispatch.probe_all", fake_probe)
    monkeypatch.setattr("orch.mcp_server.probe_all", fake_probe)
    return config, repo, spawned


def _fake_caps(engine: Engine):
    from orch.engines.base import Capabilities

    return Capabilities(
        engine=engine,
        path=f"/usr/bin/{engine.value}",
        version="1.0",
        structured_output=True,
        reports_cost=True,
    )


def tool(server, name: str):
    """Reach the plain function behind a registered MCP tool."""
    return server._tool_manager._tools[name].fn  # noqa: SLF001


def test_dispatch_queues_and_starts_without_waiting(env) -> None:
    config, repo, spawned = env
    server = build_server(config)

    result = tool(server, "orch_dispatch")(repo=str(repo), task="add a module")

    assert result["task_id"] == "task-0001"
    assert result["engine"] == "codex"  # implement routes to codex
    assert result["worker_pid"] == 4242
    assert spawned == ["task-0001"]

    stored = TaskStore(config).get_task("task-0001")
    assert stored is not None
    assert stored.status == TaskStatus.QUEUED  # the worker claims it, not dispatch
    assert stored.kind == TaskKind.IMPLEMENT


def test_kind_selects_the_engine_and_engine_overrides_it(env) -> None:
    config, repo, _ = env
    server = build_server(config)
    dispatch = tool(server, "orch_dispatch")

    assert dispatch(repo=str(repo), task="look at this", kind="review")["engine"] == "grok"
    assert dispatch(repo=str(repo), task="check the ui", kind="ui_verify")["engine"] == "antigravity"
    assert dispatch(repo=str(repo), task="build it", engine="claude")["engine"] == "claude"


def test_dispatch_rejects_a_missing_repo(env) -> None:
    config, _, _ = env
    server = build_server(config)

    with pytest.raises(DispatchError, match="repo does not exist"):
        tool(server, "orch_dispatch")(repo="/nope/not/here", task="x")


def test_dispatch_rejects_an_unknown_kind(env) -> None:
    config, repo, _ = env
    server = build_server(config)

    with pytest.raises(DispatchError, match="not one of"):
        tool(server, "orch_dispatch")(repo=str(repo), task="x", kind="telepathy")


def test_dispatch_rejects_an_engine_this_machine_lacks(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, repo, _ = env
    monkeypatch.setattr(
        "orch.dispatch.probe_all",
        lambda refresh=False: {Engine.CLAUDE: _fake_caps(Engine.CLAUDE)},
    )
    server = build_server(config)

    with pytest.raises(DispatchError, match="not available"):
        tool(server, "orch_dispatch")(repo=str(repo), task="x", engine="codex")


def test_parallel_dispatch_groups_under_a_parent(env) -> None:
    config, repo, _ = env
    server = build_server(config)
    dispatch = tool(server, "orch_dispatch")

    first = dispatch(repo=str(repo), task="same work", engine="codex")
    dispatch(repo=str(repo), task="same work", engine="grok", parent_id=first["task_id"])

    listed = tool(server, "orch_list")(parent_id=first["task_id"])
    assert [task["engine"] for task in listed["tasks"]] == ["grok"]


def test_engines_reports_the_routing_table(env) -> None:
    config, _, _ = env
    server = build_server(config)

    result = tool(server, "orch_engines")()

    assert {entry["engine"] for entry in result["engines"]} == {e.value for e in Engine}
    routing = {entry["kind"]: entry["engine"] for entry in result["routing"]}
    assert routing["implement"] == "codex"
    assert routing["review"] == "grok"


async def test_wait_returns_finished_tasks_and_reports_the_rest(env) -> None:
    config, repo, _ = env
    server = build_server(config)
    store = TaskStore(config)

    done = tool(server, "orch_dispatch")(repo=str(repo), task="done")
    pending = tool(server, "orch_dispatch")(repo=str(repo), task="pending")
    store.update_task(done["task_id"], status=TaskStatus.NEEDS_REVIEW)

    result = await tool(server, "orch_wait")(
        task_ids=[done["task_id"], pending["task_id"]], timeout_s=1
    )

    assert [task["task_id"] for task in result["finished"]] == [done["task_id"]]
    assert result["still_running"] == [pending["task_id"]]
    assert result["timed_out"] is True


def test_wait_budget_is_clamped_at_both_ends() -> None:
    """A caller asking to wait for hours must not be able to wedge the session."""
    assert wait_budget(10_000) == MAX_WAIT_S
    assert wait_budget(0) == 1.0
    assert wait_budget(-5) == 1.0
    assert wait_budget(30) == 30.0


def test_status_includes_the_live_log_tail(env) -> None:
    config, repo, _ = env
    server = build_server(config)
    created = tool(server, "orch_dispatch")(repo=str(repo), task="work")
    log_path = config.logs_dir / created["task_id"] / "stdout.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("line one\nline two\n", encoding="utf-8")

    result = tool(server, "orch_status")(task_id=created["task_id"])

    assert "line two" in result["stdout_tail"]


def test_status_serializes_model_and_quota_fields(env) -> None:
    config, repo, _ = env
    server = build_server(config)
    created = tool(server, "orch_dispatch")(repo=str(repo), task="work")
    reset = datetime(2026, 8, 18, 0, 47, 55, tzinfo=UTC)
    TaskStore(config).update_task(
        created["task_id"],
        model="gpt-5.6-sol",
        plan_type="plus",
        quota_used_pct=4.0,
        quota_window_minutes=10080,
        quota_resets_at=reset,
    )

    result = tool(server, "orch_status")(task_id=created["task_id"])

    assert result["model"] == "gpt-5.6-sol"
    assert result["plan_type"] == "plus"
    assert result["quota_used_pct"] == 4.0
    assert result["quota_window_minutes"] == 10080
    assert result["quota_resets_at"] == reset.isoformat()


def test_status_of_an_unknown_task_says_so(env) -> None:
    config, _, _ = env
    server = build_server(config)

    with pytest.raises(DispatchError, match="task not found"):
        tool(server, "orch_status")(task_id="task-9999")


def test_diff_is_captured_live_for_a_running_task(env) -> None:
    config, repo, _ = env
    server = build_server(config)
    created = tool(server, "orch_dispatch")(repo=str(repo), task="work")

    # Stand in for a worktree the engine is part way through.
    workspace = config.workspaces_dir / created["task_id"] / "repo"
    workspace.mkdir(parents=True)
    subprocess.run(
        ["git", "worktree", "add", str(workspace), "-b", "agent/codex/x"],
        cwd=repo,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    (workspace / "in_progress.py").write_text("x = 1\n", encoding="utf-8")
    TaskStore(config).update_task(created["task_id"], workspace_path=workspace)

    result = tool(server, "orch_diff")(task_id=created["task_id"])

    assert "in_progress.py" in result["diff"]


def test_diff_truncates_and_says_so(env) -> None:
    config, repo, _ = env
    server = build_server(config)
    created = tool(server, "orch_dispatch")(repo=str(repo), task="work")
    diff_path = config.logs_dir / created["task_id"] / "diff.patch"
    diff_path.parent.mkdir(parents=True, exist_ok=True)
    diff_path.write_text("+" + "x" * 5000 + "\n", encoding="utf-8")

    result = tool(server, "orch_diff")(task_id=created["task_id"], max_bytes=100)

    assert result["truncated"] is True
    assert result["diff_path"] == str(diff_path)


def test_adopt_defaults_to_changing_nothing(env) -> None:
    config, repo, _ = env
    server = build_server(config)
    created = tool(server, "orch_dispatch")(repo=str(repo), task="work")
    _write_patch(config, created["task_id"])

    result = tool(server, "orch_adopt")(task_id=created["task_id"])

    assert result["applied"] is False
    assert not (repo / "added.py").exists()


def test_adopt_apply_writes_into_the_repository(env) -> None:
    config, repo, _ = env
    server = build_server(config)
    created = tool(server, "orch_dispatch")(repo=str(repo), task="work")
    _write_patch(config, created["task_id"])

    result = tool(server, "orch_adopt")(task_id=created["task_id"], strategy="apply")

    assert result["applied"] is True
    assert (repo / "added.py").read_text(encoding="utf-8") == "x = 1\n"


def test_adopt_apply_refuses_to_write_over_uncommitted_work(env) -> None:
    config, repo, _ = env
    server = build_server(config)
    created = tool(server, "orch_dispatch")(repo=str(repo), task="work")
    _write_patch(config, created["task_id"])
    (repo / "README.md").write_text("# edited by the human\n", encoding="utf-8")

    result = tool(server, "orch_adopt")(task_id=created["task_id"], strategy="apply")

    assert result["applied"] is False
    assert "uncommitted" in result["note"]
    assert not (repo / "added.py").exists()


def test_adopt_rejects_an_unknown_strategy(env) -> None:
    config, repo, _ = env
    server = build_server(config)
    created = tool(server, "orch_dispatch")(repo=str(repo), task="work")
    _write_patch(config, created["task_id"])

    with pytest.raises(DispatchError, match="unknown strategy"):
        tool(server, "orch_adopt")(task_id=created["task_id"], strategy="rebase")


def test_cancel_marks_the_task_and_keeps_the_evidence(env) -> None:
    config, repo, _ = env
    server = build_server(config)
    created = tool(server, "orch_dispatch")(repo=str(repo), task="work")
    TaskStore(config).update_task(created["task_id"], status=TaskStatus.RUNNING)

    result = tool(server, "orch_cancel")(task_id=created["task_id"])

    assert result["status"] == TaskStatus.BLOCKED.value
    stored = TaskStore(config).get_task(created["task_id"])
    assert stored is not None
    assert stored.status == TaskStatus.BLOCKED


def test_cancelling_a_finished_task_is_a_no_op(env) -> None:
    config, repo, _ = env
    server = build_server(config)
    created = tool(server, "orch_dispatch")(repo=str(repo), task="work")
    TaskStore(config).update_task(created["task_id"], status=TaskStatus.SUCCEEDED)

    result = tool(server, "orch_cancel")(task_id=created["task_id"])

    assert result["note"] == "already finished"


def _write_patch(config: Config, task_id: str) -> None:
    diff_path = config.logs_dir / task_id / "diff.patch"
    diff_path.parent.mkdir(parents=True, exist_ok=True)
    diff_path.write_text(
        "diff --git a/added.py b/added.py\n"
        "new file mode 100644\n"
        "index 0000000..8b13789\n"
        "--- /dev/null\n"
        "+++ b/added.py\n"
        "@@ -0,0 +1 @@\n"
        "+x = 1\n",
        encoding="utf-8",
    )


def test_dispatch_reports_the_branch_immediately(env) -> None:
    """The worktree is made by the worker, but the caller needs the branch now — the
    playbook has it point reviewers at agent/<engine>/<id> before the task finishes."""
    config, repo, _ = env
    server = build_server(config)

    result = tool(server, "orch_dispatch")(repo=str(repo), task="work", kind="review")

    assert result["branch"] == "agent/grok/task-0001"
    stored = TaskStore(config).get_task("task-0001")
    assert stored is not None
    assert stored.branch_name == "agent/grok/task-0001"


def test_usage_reports_every_engine_without_touching_the_database(env) -> None:
    """The quota tool is a read of engine-owned files, so it must not write a task row."""
    config, _, spawned = env
    server = build_server(config)

    payload = tool(server, "orch_usage")()

    assert [entry["engine"] for entry in payload["engines"]] == [
        "codex",
        "claude",
        "grok",
        "antigravity",
    ]
    # Every entry says when it was read and what it was read from, or why it is empty.
    for entry in payload["engines"]:
        assert entry["source"] is not None or entry["notes"]
        assert "age_seconds" in entry and "expired" in entry
    assert spawned == []
    assert TaskStore(config).list_tasks() == []

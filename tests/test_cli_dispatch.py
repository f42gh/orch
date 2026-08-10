"""`agentctl dispatch` is the machine entry point CAGE calls; its JSON is a contract.

Worker spawning is stubbed: these tests check that dispatch resolves the engine
eagerly, writes the row, and prints what a machine caller needs — not that a real
engine runs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_orchestrator.cli import main
from agent_orchestrator.config import Config
from agent_orchestrator.db import TaskStore
from agent_orchestrator.engines.base import Capabilities
from agent_orchestrator.models import Engine, TaskStatus


def _fake_caps(engine: Engine) -> Capabilities:
    return Capabilities(
        engine=engine,
        path=f"/usr/bin/{engine.value}",
        version="1.0",
        structured_output=True,
        reports_cost=True,
    )


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, list[str]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    runtime = tmp_path / "runtime"

    spawned: list[str] = []
    monkeypatch.setattr(
        "agent_orchestrator.dispatch.spawn_worker",
        lambda cfg, task_id: (spawned.append(task_id), 4242)[1],
    )
    fake_probe = lambda refresh=False: {engine: _fake_caps(engine) for engine in Engine}  # noqa: E731
    monkeypatch.setattr("agent_orchestrator.dispatch.probe_all", fake_probe)
    monkeypatch.setattr("agent_orchestrator.cli.probe_all", fake_probe)
    return runtime, repo, spawned


def run_cli(monkeypatch: pytest.MonkeyPatch, runtime: Path, *argv: str) -> None:
    monkeypatch.setattr(
        "sys.argv", ["agentctl", "--runtime-root", str(runtime), *argv]
    )
    main()


def test_dispatch_json_prints_the_contract_and_starts_a_worker(
    env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime, repo, spawned = env

    run_cli(
        monkeypatch, runtime, "dispatch", "--repo", str(repo), "--task", "add a module", "--json"
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["task_id"] == "task-0001"
    assert payload["engine"] == "codex"  # implement routes to codex
    assert payload["branch"] == "agent/codex/task-0001"
    assert payload["status"] == "queued"  # the worker claims it, not dispatch
    assert payload["worker_pid"] == 4242
    assert spawned == ["task-0001"]

    stored = TaskStore(Config(runtime_root=runtime)).get_task("task-0001")
    assert stored is not None
    assert stored.status == TaskStatus.QUEUED
    assert stored.branch_name == "agent/codex/task-0001"


def test_dispatch_without_json_stays_human_readable(
    env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime, repo, _ = env

    run_cli(monkeypatch, runtime, "dispatch", "--repo", str(repo), "--task", "add a module")

    out = capsys.readouterr().out
    assert "dispatched task-0001" in out
    assert "engine: codex" in out


def test_dispatch_refuses_a_missing_repo_without_writing_a_row(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _, spawned = env

    with pytest.raises(SystemExit, match="repo does not exist"):
        run_cli(monkeypatch, runtime, "dispatch", "--repo", "/nope", "--task", "x", "--json")

    assert spawned == []
    assert TaskStore(Config(runtime_root=runtime)).list_tasks() == []


def test_engines_json_reports_engines_and_routing(
    env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime, _, _ = env

    run_cli(monkeypatch, runtime, "engines", "--json")

    payload = json.loads(capsys.readouterr().out)
    assert {entry["engine"] for entry in payload["engines"]} == {e.value for e in Engine}
    routing = {entry["kind"]: entry["engine"] for entry in payload["routing"]}
    assert routing["implement"] == "codex"
    assert routing["review"] == "grok"
    assert "implement" in payload["kinds"]

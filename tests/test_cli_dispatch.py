"""CLI dispatch and engines: resolve, write the row, print JSON.

Worker spawning is stubbed: these tests check that dispatch resolves the engine
eagerly, writes the row, and prints what a machine caller needs — not that a real
engine runs.
"""

from __future__ import annotations

import json
import signal
import subprocess
from pathlib import Path

import pytest

from agent_orchestrator.cli import main
from agent_orchestrator.config import Config
from agent_orchestrator.db import TaskStore
from agent_orchestrator.dispatch import spawn_worker
from agent_orchestrator.engines.base import Capabilities
from agent_orchestrator.models import Engine, TaskStatus
from agent_orchestrator.views import task_detail


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
        "sys.argv", ["orch", "--runtime-root", str(runtime), *argv]
    )
    main()


def test_dispatch_json_prints_the_task_and_starts_a_worker(
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


def test_dispatch_json_uses_the_shared_task_view(
    env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`orch dispatch --json` is the same payload `orch_dispatch` returns, not a subset."""
    runtime, repo, _ = env

    run_cli(
        monkeypatch, runtime, "dispatch", "--repo", str(repo), "--task", "x", "--json"
    )

    payload = json.loads(capsys.readouterr().out)
    stored = TaskStore(Config(runtime_root=runtime)).get_task("task-0001")
    assert stored is not None
    assert set(payload) == set(task_detail(Config(runtime_root=runtime), stored)) | {
        "worker_pid"
    }
    assert payload["worker_pid"] == 4242
    assert payload["task"] == "x"


def test_dispatch_reports_a_spawn_failure_without_a_traceback(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A queued row already exists when spawning fails, so the message has to say so."""
    runtime, repo, _ = env
    monkeypatch.setattr(
        "agent_orchestrator.dispatch.spawn_worker",
        lambda cfg, task_id: (_ for _ in ()).throw(OSError("Too many open files")),
    )

    with pytest.raises(
        SystemExit,
        match=r"the task is queued.*orch list.*orch daemon run-task",
    ):
        run_cli(monkeypatch, runtime, "dispatch", "--repo", str(repo), "--task", "x")

    assert [task.id for task in TaskStore(Config(runtime_root=runtime)).list_tasks()] == [
        "task-0001"
    ]


def test_add_refuses_a_missing_repo_like_dispatch_does(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`add` used to queue whatever --repo said, failing much later in a worker."""
    runtime, _, _ = env

    with pytest.raises(SystemExit, match="repo does not exist"):
        run_cli(monkeypatch, runtime, "add", "--repo", "/nope", "--task", "x")

    assert TaskStore(Config(runtime_root=runtime)).list_tasks() == []


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


def test_spawn_worker_stops_the_child_when_pid_file_cannot_be_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = Config(runtime_root=tmp_path / "runtime")
    killed: list[tuple[int, signal.Signals]] = []
    waits: list[int] = []

    class FakeProcess:
        pid = 4321

        def wait(self, timeout: int) -> int:
            waits.append(timeout)
            return 0

    monkeypatch.setattr(
        "agent_orchestrator.dispatch.subprocess.Popen",
        lambda *args, **kwargs: FakeProcess(),
    )
    monkeypatch.setattr(
        "agent_orchestrator.dispatch.os.killpg",
        lambda pid, sig: killed.append((pid, sig)),
    )
    original_write_text = Path.write_text

    def write_text(path: Path, data: str, **kwargs) -> int:
        if path.name == "worker.pid":
            raise OSError("pid write failed")
        return original_write_text(path, data, **kwargs)

    monkeypatch.setattr(Path, "write_text", write_text)

    with pytest.raises(OSError, match="pid write failed"):
        spawn_worker(config, "task-0001")

    assert killed == [(4321, signal.SIGTERM)]
    assert waits == [5]


def test_spawn_worker_falls_back_to_process_signals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = Config(runtime_root=tmp_path / "runtime")
    signals: list[str] = []
    waits = 0

    class FakeProcess:
        pid = 4321

        def wait(self, timeout: int) -> int:
            nonlocal waits
            waits += 1
            if waits == 1:
                raise subprocess.TimeoutExpired("agentd", timeout)
            return 0

        def terminate(self) -> None:
            signals.append("terminate")

        def kill(self) -> None:
            signals.append("kill")

    monkeypatch.setattr(
        "agent_orchestrator.dispatch.subprocess.Popen",
        lambda *args, **kwargs: FakeProcess(),
    )
    monkeypatch.setattr(
        "agent_orchestrator.dispatch.os.killpg",
        lambda pid, sig: (_ for _ in ()).throw(PermissionError("denied")),
    )
    original_write_text = Path.write_text

    def write_text(path: Path, data: str, **kwargs) -> int:
        if path.name == "worker.pid":
            raise OSError("pid write failed")
        return original_write_text(path, data, **kwargs)

    monkeypatch.setattr(Path, "write_text", write_text)

    with pytest.raises(OSError, match="pid write failed"):
        spawn_worker(config, "task-0001")

    assert signals == ["terminate", "kill"]
    assert waits == 2

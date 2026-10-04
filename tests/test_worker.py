"""Process-level behaviour of the shared runner, exercised with real subprocesses.

No engine is invoked here — the commands are `cat`, `sh` and `printf` — so these tests
cover the parts that actually bite (stdin, timeouts, process groups) without spending
API calls.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from orch.config import Config
from orch.db import TaskStore
from orch.engines.base import Capabilities, EngineResult, RunSpec
from orch.models import Engine, Priority, Risk, Task, TaskKind, TaskStatus, TokenUsage
from orch.worker import _spawn, run_task


FIXTURES = Path(__file__).parent / "fixtures"


def init_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=path, check=True)
    (path / "README.md").write_text("# test\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, stdout=subprocess.DEVNULL)


def test_stdin_is_closed_so_a_reader_does_not_hang(tmp_path: Path) -> None:
    """`codex exec` waits forever for stdin on a non-TTY; DEVNULL is what prevents it."""
    stdout, _, exit_code, timed_out = _spawn(
        RunSpec(argv=["cat"]), tmp_path, tmp_path / "out.log", tmp_path / "err.log", timeout_s=10
    )

    assert timed_out is False
    assert exit_code == 0
    assert stdout == ""


def test_streams_are_teed_to_disk_while_running(tmp_path: Path) -> None:
    stdout_log = tmp_path / "stdout.log"
    stderr_log = tmp_path / "stderr.log"

    stdout, stderr, exit_code, _ = _spawn(
        RunSpec(argv=["sh", "-c", "printf 'hello\\n'; printf 'oops\\n' >&2"]),
        tmp_path,
        stdout_log,
        stderr_log,
        timeout_s=10,
    )

    assert stdout.strip() == "hello"
    assert stderr.strip() == "oops"
    assert stdout_log.read_text(encoding="utf-8").strip() == "hello"
    assert stderr_log.read_text(encoding="utf-8").strip() == "oops"


def test_a_run_over_budget_is_terminated(tmp_path: Path) -> None:
    started = time.monotonic()

    _, _, _, timed_out = _spawn(
        RunSpec(argv=["sleep", "30"]), tmp_path, tmp_path / "o.log", tmp_path / "e.log", timeout_s=1
    )

    assert timed_out is True
    assert time.monotonic() - started < 15  # killed, not waited out


def test_termination_reaches_child_processes(tmp_path: Path) -> None:
    """These agents spawn shells of their own, so signalling only the parent leaks them."""
    marker = tmp_path / "child.pid"
    script = f"sh -c 'echo $$ > {marker}; sleep 30' & sleep 30"

    _spawn(
        RunSpec(argv=["sh", "-c", script]),
        tmp_path,
        tmp_path / "o.log",
        tmp_path / "e.log",
        timeout_s=1,
    )

    assert marker.exists()
    child_pid = int(marker.read_text(encoding="utf-8").strip())
    time.sleep(0.5)
    assert not _process_alive(child_pid)


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# --- run_task, with a stub engine -----------------------------------------------------


class StubAdapter:
    """An engine that writes a file and reports on it, without calling anything."""

    engine = Engine.CODEX

    def __init__(self, script: str, result: EngineResult | None = None) -> None:
        self.script = script
        self.result = result

    def probe(self) -> Capabilities | None:
        return Capabilities(
            engine=self.engine,
            path="/stub",
            version="0",
            structured_output=True,
            reports_cost=True,
        )

    def build(self, task, policy, workspace, prompt, artifacts) -> RunSpec:
        return RunSpec(argv=["sh", "-c", self.script])

    def parse(self, stdout, stderr, exit_code, spec) -> EngineResult:
        if self.result is not None:
            return EngineResult(
                text=self.result.text,
                exit_code=exit_code,
                session_id=self.result.session_id,
                model=self.result.model,
                usage=self.result.usage,
                cost_usd=self.result.cost_usd,
                warnings=self.result.warnings,
                structured=self.result.structured,
                tokens=self.result.tokens,
            )
        return EngineResult(text=stdout.strip(), exit_code=exit_code, cost_usd=0.5)


@pytest.fixture
def prepared(tmp_path: Path) -> tuple[Config, TaskStore, Task]:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    init_repo(workspace)

    config = Config(runtime_root=tmp_path / "runtime")
    store = TaskStore(config)
    created = store.add_task(workspace, "add a module", Risk.NORMAL, Priority.NORMAL)
    store.update_task(
        created.id,
        engine=Engine.CODEX,
        workspace_path=workspace,
        branch_name="agent/codex/" + created.id,
        status=TaskStatus.RUNNING,
    )
    task = store.get_task(created.id)
    assert task is not None
    return config, store, task


def test_successful_run_records_diff_cost_and_needs_review(
    prepared: tuple[Config, TaskStore, Task], monkeypatch: pytest.MonkeyPatch
) -> None:
    config, store, task = prepared
    adapter = StubAdapter("printf 'wrote it\\n'; printf 'x = 1\\n' > added.py")
    monkeypatch.setattr("orch.worker.get_adapter", lambda engine: adapter)

    status = run_task(config, store, task)

    assert status == TaskStatus.NEEDS_REVIEW
    stored = store.get_task(task.id)
    assert stored is not None
    assert stored.status == TaskStatus.NEEDS_REVIEW
    assert stored.cost_usd == 0.5
    assert stored.exit_code == 0
    assert stored.engine_ms is not None
    assert stored.engine_ms >= 0
    assert stored.tokens is None
    assert stored.files_changed == 1
    assert stored.insertions == 1
    assert stored.deletions == 0

    payload = json.loads((config.logs_dir / task.id / "result.json").read_text(encoding="utf-8"))
    assert payload["engine"] == "codex"
    assert "added.py" in payload["changed_files"]

    diff = (config.logs_dir / task.id / "diff.patch").read_text(encoding="utf-8")
    assert "added.py" in diff


def test_reported_tokens_are_persisted(
    prepared: tuple[Config, TaskStore, Task], monkeypatch: pytest.MonkeyPatch
) -> None:
    config, store, task = prepared
    tokens = TokenUsage(
        input_tokens=11,
        output_tokens=12,
        cache_read_tokens=13,
        cache_write_tokens=14,
        reasoning_tokens=15,
    )
    adapter = StubAdapter(
        "printf 'done\n'",
        EngineResult(text="done", exit_code=0, tokens=tokens),
    )
    monkeypatch.setattr("orch.worker.get_adapter", lambda engine: adapter)

    run_task(config, store, task)

    stored = store.get_task(task.id)
    assert stored is not None
    assert stored.tokens == tokens


def test_codex_rollout_model_and_quota_are_persisted(
    prepared: tuple[Config, TaskStore, Task],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, store, task = prepared
    session_id = "fixture-session-id"
    codex_home = tmp_path / "codex-home"
    rollout_dir = codex_home / "sessions" / "2026" / "08" / "11"
    rollout_dir.mkdir(parents=True)
    rollout = rollout_dir / f"rollout-2026-08-11T10-04-14-{session_id}.jsonl"
    rollout.write_text(
        (FIXTURES / "codex_rollout.jsonl").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    adapter = StubAdapter(
        "printf 'done\n'",
        EngineResult(text="done", exit_code=0, session_id=session_id),
    )
    monkeypatch.setattr("orch.worker.get_adapter", lambda engine: adapter)

    assert run_task(config, store, task) == TaskStatus.NEEDS_REVIEW

    stored = store.get_task(task.id)
    assert stored is not None
    assert stored.model == "gpt-5.6-sol"
    assert stored.plan_type == "plus"
    assert stored.quota_used_pct == 4.0
    assert stored.quota_window_minutes == 10080
    assert stored.quota_resets_at == datetime(2026, 8, 18, 0, 47, 55, tzinfo=UTC)


def test_codex_run_without_a_matching_rollout_finishes_cleanly(
    prepared: tuple[Config, TaskStore, Task],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, store, task = prepared
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty-codex-home"))
    adapter = StubAdapter(
        "printf 'done\n'",
        EngineResult(text="done", exit_code=0, session_id="missing-session"),
    )
    monkeypatch.setattr("orch.worker.get_adapter", lambda engine: adapter)

    assert run_task(config, store, task) == TaskStatus.NEEDS_REVIEW

    stored = store.get_task(task.id)
    assert stored is not None
    assert stored.model is None
    assert stored.plan_type is None
    assert stored.quota_used_pct is None
    assert stored.quota_window_minutes is None
    assert stored.quota_resets_at is None


def test_nonzero_exit_fails_the_task(
    prepared: tuple[Config, TaskStore, Task], monkeypatch: pytest.MonkeyPatch
) -> None:
    config, store, task = prepared
    adapter = StubAdapter("printf 'boom\\n' >&2; exit 3")
    monkeypatch.setattr("orch.worker.get_adapter", lambda engine: adapter)

    assert run_task(config, store, task) == TaskStatus.FAILED

    stored = store.get_task(task.id)
    assert stored is not None
    assert stored.exit_code == 3
    assert stored.error


def test_exit_zero_with_no_output_is_a_failure(
    prepared: tuple[Config, TaskStore, Task], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exactly how the agy non-TTY bug presents. There is nothing for a human to review."""
    config, store, task = prepared
    monkeypatch.setattr(
        "orch.worker.get_adapter", lambda engine: StubAdapter("true")
    )

    assert run_task(config, store, task) == TaskStatus.FAILED


def test_a_leaked_credential_becomes_a_warning(
    prepared: tuple[Config, TaskStore, Task], monkeypatch: pytest.MonkeyPatch
) -> None:
    config, store, task = prepared
    adapter = StubAdapter(
        "printf 'done\\n'; printf \"KEY = 'AKIAIOSFODNN7EXAMPLE'\\n\" > leaked.py"
    )
    monkeypatch.setattr("orch.worker.get_adapter", lambda engine: adapter)

    run_task(config, store, task)

    payload = json.loads((config.logs_dir / task.id / "result.json").read_text(encoding="utf-8"))
    assert any("AWS access key" in warning for warning in payload["warnings"])


def test_a_run_that_overruns_is_failed_and_explained(
    prepared: tuple[Config, TaskStore, Task], monkeypatch: pytest.MonkeyPatch
) -> None:
    config, store, task = prepared
    monkeypatch.setattr(
        "orch.worker.get_adapter", lambda engine: StubAdapter("sleep 30")
    )
    monkeypatch.setattr(
        "orch.worker.resolve_policy",
        lambda kind, risk, engine, table: _fast_timeout_policy(),
    )

    assert run_task(config, store, task) == TaskStatus.FAILED

    payload = json.loads((config.logs_dir / task.id / "result.json").read_text(encoding="utf-8"))
    assert any("timed out" in warning for warning in payload["warnings"])


def _fast_timeout_policy():
    from orch.router import AccessLevel, EnginePolicy

    return EnginePolicy(access=AccessLevel.WORKSPACE_WRITE, max_turns=1, timeout_s=1)


def test_missing_binary_fails_cleanly(
    prepared: tuple[Config, TaskStore, Task], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An uninstalled engine must record a failure, not raise out of the daemon."""
    config, store, task = prepared

    class MissingAdapter(StubAdapter):
        def build(self, task, policy, workspace, prompt, artifacts) -> RunSpec:
            return RunSpec(argv=["definitely-not-a-real-binary-xyz"])

    monkeypatch.setattr(
        "orch.worker.get_adapter", lambda engine: MissingAdapter("")
    )

    assert run_task(config, store, task) == TaskStatus.FAILED
    stored = store.get_task(task.id)
    assert stored is not None
    assert stored.status == TaskStatus.FAILED
    assert stored.engine_ms is not None
    assert stored.engine_ms >= 0
    assert stored.files_changed == 0
    assert stored.insertions == 0
    assert stored.deletions == 0
    assert "failed to start" in (stored.error or "")


def test_artifacts_directory_is_available_to_the_adapter(
    prepared: tuple[Config, TaskStore, Task], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Path] = {}

    class RecordingAdapter(StubAdapter):
        def build(self, task, policy, workspace, prompt, artifacts) -> RunSpec:
            seen["artifacts"] = artifacts
            return RunSpec(argv=["printf", "ok\\n"], files={artifacts / "note.txt": "hi"})

    monkeypatch.setattr(
        "orch.worker.get_adapter", lambda engine: RecordingAdapter("")
    )
    config, store, task = prepared

    run_task(config, store, task)

    assert seen["artifacts"].is_dir()
    assert (seen["artifacts"] / "note.txt").read_text(encoding="utf-8") == "hi"

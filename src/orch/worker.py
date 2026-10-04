"""Run one task with one engine.

Everything process-shaped lives here so the adapters stay pure: spawning, live log
capture, timeouts, and writing the outcome back to the store.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from orch.config import Config
from orch.db import TaskStore
from orch.engines import EngineResult, RunSpec, get_adapter
from orch.logging_utils import append_log, task_log_dir
from orch.models import Engine, Task, TaskStatus
from orch.prompts import build_prompt, schema_for
from orch.result import DiffStat, diff_numstat, save_git_diff, write_result_json
from orch.router import (
    EnginePolicy,
    load_routing_table,
    resolve_policy,
)
from orch.secrets_scan import scan_task_artifacts
from orch.session_logs import read_codex_session

#: How long a killed process gets to exit before SIGKILL.
TERM_GRACE_S = 10.0


class WorkerError(RuntimeError):
    pass


def run_task(config: Config, store: TaskStore, task: Task) -> TaskStatus:
    """Execute `task` and record the outcome. Never raises for engine failures."""
    if task.workspace_path is None:
        raise WorkerError("task.workspace_path is required")
    if task.engine is None:
        raise WorkerError("task.engine is required; the router should have set it")

    log_dir = task_log_dir(config, task.id)
    agent_log = log_dir / "agent.log"
    stdout_log = log_dir / "stdout.log"
    stderr_log = log_dir / "stderr.log"

    table = load_routing_table(config.routing_path)
    policy = resolve_policy(task.kind, task.risk, task.engine, table)
    # The prompt must agree with whether a schema is being enforced, or the model is
    # asked for two incompatible output shapes at once.
    prompt = build_prompt(task, policy.access, structured=schema_for(task.kind) is not None)

    artifacts = log_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)

    adapter = get_adapter(task.engine)
    spec = adapter.build(task, policy, Path(task.workspace_path), prompt, artifacts)
    for path, content in spec.files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    _log_header(agent_log, task, policy, spec, prompt)

    # Adapter-reported durations exist for Claude and Antigravity, but timing only the
    # spawn is comparable across all four engines and excludes worktree and prompt setup.
    engine_started = time.monotonic()
    try:
        stdout, stderr, exit_code, timed_out = _spawn(
            spec,
            Path(task.workspace_path),
            stdout_log,
            stderr_log,
            policy.timeout_s,
        )
    except OSError as exc:
        engine_ms = int((time.monotonic() - engine_started) * 1000)
        message = f"failed to start {task.engine.value}: {exc}"
        append_log(stderr_log, message)
        store.update_task(task.id, engine_ms=engine_ms)
        return _finish(config, store, task, TaskStatus.FAILED, message, warnings=[str(exc)])
    engine_ms = int((time.monotonic() - engine_started) * 1000)

    result = adapter.parse(stdout, stderr, exit_code, spec)
    warnings = list(result.warnings)
    if timed_out:
        warnings.append(f"timed out after {policy.timeout_s}s and was terminated")

    save_git_diff(config, task.id, Path(task.workspace_path))
    warnings.extend(scan_task_artifacts(config, task.id))

    status = _status_for(result, timed_out)
    summary = result.text.strip() or f"{task.engine.value} produced no output"
    result_fields: dict[str, object] = {
        "exit_code": exit_code,
        "engine_ms": engine_ms,
    }
    for field, value in (
        ("engine_session_id", result.session_id),
        ("model", result.model),
        ("cost_usd", result.cost_usd),
    ):
        if value is not None:
            result_fields[field] = value
    if task.engine is Engine.CODEX and result.session_id is not None:
        # Codex is the only engine whose model and plan are absent from stdout but
        # present on disk, so this filesystem-specific enrichment stays in the worker.
        try:
            session_info = read_codex_session(result.session_id)
        except Exception:  # noqa: BLE001 - optional telemetry must never fail the task
            session_info = None
        if session_info is not None:
            for field, value in (
                ("model", session_info.model),
                ("plan_type", session_info.plan_type),
                ("quota_used_pct", session_info.quota_used_pct),
                ("quota_window_minutes", session_info.quota_window_minutes),
                ("quota_resets_at", session_info.quota_resets_at),
            ):
                if value is not None:
                    result_fields[field] = value
    if result.tokens is not None:
        result_fields.update(
            tokens_input=result.tokens.input_tokens,
            tokens_output=result.tokens.output_tokens,
            tokens_cache_read=result.tokens.cache_read_tokens,
            tokens_cache_write=result.tokens.cache_write_tokens,
            tokens_reasoning=result.tokens.reasoning_tokens,
        )
    store.update_task(task.id, **result_fields)
    return _finish(
        config,
        store,
        task,
        status,
        summary,
        warnings=warnings,
        commands_run=[" ".join(spec.argv)],
        result=result,
    )


def _status_for(result: EngineResult, timed_out: bool) -> TaskStatus:
    if timed_out or result.exit_code != 0:
        return TaskStatus.FAILED
    if not result.text.strip():
        # Exit 0 with nothing to show is a failure, not a success: it is how the agy
        # non-TTY bug presents, and there is nothing for a human to review.
        return TaskStatus.FAILED
    return TaskStatus.NEEDS_REVIEW


def _finish(
    config: Config,
    store: TaskStore,
    task: Task,
    status: TaskStatus,
    summary: str,
    warnings: list[str] | None = None,
    commands_run: list[str] | None = None,
    result: EngineResult | None = None,
) -> TaskStatus:
    workspace = Path(task.workspace_path) if task.workspace_path else None
    diff = (
        diff_numstat(workspace)
        if workspace is not None and workspace.exists()
        else DiffStat(files_changed=0, insertions=0, deletions=0, files=())
    )
    write_result_json(
        config,
        task,
        status,
        summary,
        commands_run=commands_run,
        warnings=warnings,
        engine=task.engine,
        usage=result.usage if result else None,
        cost_usd=result.cost_usd if result else None,
        structured=result.structured if result else None,
        diff=diff,
    )
    error = summary if status == TaskStatus.FAILED else None
    store.update_task(
        task.id,
        status=status,
        result_summary=summary,
        error=error,
        files_changed=diff.files_changed,
        insertions=diff.insertions,
        deletions=diff.deletions,
    )
    return status


def _log_header(path: Path, task: Task, policy: EnginePolicy, spec: RunSpec, prompt: str) -> None:
    append_log(path, f"engine={task.engine.value if task.engine else '?'}")
    append_log(path, f"kind={task.kind.value} risk={task.risk.value} access={policy.access.value}")
    append_log(path, f"max_turns={policy.max_turns} timeout_s={policy.timeout_s}")
    append_log(path, f"argv={spec.argv}")
    append_log(path, "prompt:")
    append_log(path, prompt)


def _spawn(
    spec: RunSpec,
    cwd: Path,
    stdout_log: Path,
    stderr_log: Path,
    timeout_s: int,
) -> tuple[str, str, int, bool]:
    """Start the engine, tee its streams to disk, and enforce the timeout.

    stdin is closed for every engine: `codex exec` blocks forever on a non-TTY
    otherwise, and no engine here needs it.
    """
    env = {**os.environ, **spec.env}
    process = subprocess.Popen(
        spec.argv,
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        env=env,
        start_new_session=True,
    )

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    readers = [
        threading.Thread(target=_tee, args=(process.stdout, stdout_chunks, stdout_log), daemon=True),
        threading.Thread(target=_tee, args=(process.stderr, stderr_chunks, stderr_log), daemon=True),
    ]
    for reader in readers:
        reader.start()

    timed_out = False
    try:
        process.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate(process)

    for reader in readers:
        reader.join(timeout=5)

    return "".join(stdout_chunks), "".join(stderr_chunks), process.returncode or 0, timed_out


def _tee(stream, sink: list[str], path: Path) -> None:
    """Mirror a stream to memory and to disk so a running task can be followed live."""
    if stream is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for line in stream:
            sink.append(line)
            handle.write(line)
            handle.flush()


def _terminate(process: subprocess.Popen[str]) -> None:
    """Signal the whole process group; these agents spawn shells of their own."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        process.terminate()

    deadline = time.monotonic() + TERM_GRACE_S
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return
        time.sleep(0.1)

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        process.kill()
    process.poll()

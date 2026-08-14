"""Public CLI and MCP contracts for persistent Runs and sealed Batches."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_orchestrator.cli import (
    _interactive_routes,
    _load_tasks_file,
    _print_workflow,
    _print_workflow_dispatch,
    _route_overrides_from_flags,
    build_parser,
    main,
    run_start_wizard,
)
from agent_orchestrator.config import Config
from agent_orchestrator.db import TaskStore
from agent_orchestrator.dispatch import DispatchError
from agent_orchestrator.engines.base import Capabilities
from agent_orchestrator.mcp_server import build_server
from agent_orchestrator.models import Engine, FallbackMode, TaskKind, TaskStatus
from agent_orchestrator.workflows import WorkflowError


def tool(server, name: str):
    return server._tool_manager._tools[name].fn  # noqa: SLF001


def _capability(engine: Engine) -> Capabilities:
    return Capabilities(
        engine=engine,
        path=f"/usr/bin/{engine.value}",
        version="1.0",
        structured_output=True,
        reports_cost=True,
    )


@pytest.fixture
def workflow_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Config, TaskStore, Path, list[str]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = Config(
        runtime_root=tmp_path / "runtime",
        routing_path=tmp_path / "absent-routing.toml",
    )
    store = TaskStore(config)
    spawned: list[str] = []
    monkeypatch.setattr(
        "agent_orchestrator.workflows.probe_all",
        lambda refresh=False: {engine: _capability(engine) for engine in Engine},
    )
    monkeypatch.setattr(
        "agent_orchestrator.workflows.spawn_worker",
        lambda cfg, task_id: (spawned.append(task_id), 4000 + len(spawned))[1],
    )
    return config, store, repo, spawned


def test_cli_parser_exposes_documented_commands_and_positional_run_id() -> None:
    parser = build_parser()

    run = parser.parse_args(
        [
            "run",
            "dispatch",
            "run-0001",
            "--task",
            "work",
            "--parent",
            "task-0001",
        ]
    )
    batch = parser.parse_args(
        ["batch", "dispatch", "--repo", "/repo", "--tasks-file", "-"]
    )
    install = parser.parse_args(
        [
            "install-claude-command",
            "--target",
            "/tmp/orch.md",
            "--locale",
            "ja",
            "--force",
        ]
    )
    stats = parser.parse_args(
        [
            "stats",
            "--repo",
            "/repo",
            "--workflow",
            "run-0001",
            "--engine",
            "agy",
            "--kind",
            "implement",
            "--since",
            "2026-01-01T00:00:00+00:00",
            "--until",
            "2026-02-01T00:00:00+00:00",
            "--group-by",
            "engine",
            "--json",
        ]
    )

    assert (run.run_id, run.parent) == ("run-0001", "task-0001")
    assert batch.tasks_file == "-"
    assert install.force is True
    assert install.locale == "ja"
    assert stats.command == "stats"
    assert stats.workflow_id == "run-0001"
    assert stats.engine == "agy"
    assert stats.group_by == "engine"
    assert stats.json is True
    assert parser.parse_args(["start"]).command == "start"
    assert parser.parse_args(["run", "create", "--repo", "/repo"]).run_command == "create"
    assert parser.parse_args(["run", "list"]).run_command == "list"
    assert parser.parse_args(["run", "show", "run-0001"]).run_command == "show"
    assert parser.parse_args(["run", "close", "run-0001"]).run_command == "close"
    assert parser.parse_args(["batch", "list"]).batch_command == "list"
    assert parser.parse_args(["batch", "show", "batch-0001"]).batch_command == "show"


def test_cli_route_flags_are_strict_and_canonicalize_agy() -> None:
    routes = _route_overrides_from_flags(
        ["ui_verify=agy", "implement=codex"],
        ["implement=claude,agy"],
    )

    assert routes[TaskKind.UI_VERIFY].primary is Engine.ANTIGRAVITY
    assert routes[TaskKind.IMPLEMENT].fallback_mode is FallbackMode.MANUAL
    assert routes[TaskKind.IMPLEMENT].fallbacks == (
        Engine.CLAUDE,
        Engine.ANTIGRAVITY,
    )

    with pytest.raises(WorkflowError, match="matching explicit --route"):
        _route_overrides_from_flags([], ["review=grok"])
    with pytest.raises(WorkflowError, match="duplicate --route"):
        _route_overrides_from_flags(["review=grok", "review=codex"], [])
    with pytest.raises(WorkflowError, match="duplicate --fallback"):
        _route_overrides_from_flags(
            ["review=grok"], ["review=codex", "review=claude"]
        )
    with pytest.raises(WorkflowError, match="non-empty engine list"):
        _route_overrides_from_flags(["review=grok"], ["review=codex,,claude"])


def test_tasks_file_accepts_stdin_and_validates_objects() -> None:
    requests = _load_tasks_file(
        "-",
        stdin=io.StringIO(
            json.dumps(
                [
                    {"task": "first", "kind": "review", "risk": "read_only"},
                    {"task": "second"},
                ]
            )
        ),
    )

    assert [request.task for request in requests] == ["first", "second"]
    assert requests[0].kind is TaskKind.REVIEW
    with pytest.raises(WorkflowError, match="at least one"):
        _load_tasks_file("-", stdin=io.StringIO("[]"))
    with pytest.raises(WorkflowError, match="unknown fields"):
        _load_tasks_file("-", stdin=io.StringIO('[{"task":"x","engine":"grok"}]'))


def test_start_rejects_non_tty_with_actionable_alternatives(
    workflow_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, store, _, _ = workflow_env
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    with pytest.raises(SystemExit, match="run create.*batch dispatch"):
        run_start_wizard(config, store)


def test_start_cli_rejects_non_tty_before_creating_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    with pytest.raises(SystemExit, match="run create.*batch dispatch"):
        main(["--runtime-root", str(runtime), "start"])

    assert not runtime.exists()


def test_start_keeps_json_stdout_clean_with_injected_streams(workflow_env) -> None:
    config, store, repo, _ = workflow_env
    answers = "\n".join([str(repo), "run", *("n" for _ in TaskKind)]) + "\n"
    stdout = io.StringIO()
    prompts = io.StringIO()

    payload = run_start_wizard(
        config,
        store,
        as_json=True,
        stdin=io.StringIO(answers),
        stdout=stdout,
        prompt_output=prompts,
    )

    assert json.loads(stdout.getvalue())["workflow_id"] == payload["workflow_id"]
    assert "Repository" not in stdout.getvalue()
    assert "Repository" in prompts.getvalue()
    assert "Installed engines:" in prompts.getvalue()
    assert "antigravity:" in prompts.getvalue()


def test_start_manual_fallback_rejects_empty_csv_members(workflow_env) -> None:
    config, _, _, _ = workflow_env

    with pytest.raises(WorkflowError, match="non-empty engine list"):
        _interactive_routes(
            config,
            stdin=io.StringIO("y\n\nmanual\ncodex,,grok\n"),
            prompt_output=io.StringIO(),
        )


def test_mcp_registers_exact_workflow_tool_names(workflow_env) -> None:
    config, _, _, _ = workflow_env
    names = set(build_server(config)._tool_manager._tools)  # noqa: SLF001

    assert {
        "orch_run_create",
        "orch_run_dispatch",
        "orch_run_close",
        "orch_batch_dispatch",
        "orch_workflow_list",
        "orch_workflow_show",
    } <= names
    assert "orch_dispatch" in names


def test_mcp_registers_orch_stats(workflow_env) -> None:
    config, _, _, _ = workflow_env
    names = set(build_server(config)._tool_manager._tools)  # noqa: SLF001

    assert "orch_stats" in names
    assert "orch_usage" in names


def test_cli_stats_json_is_parseable_and_accepts_agy_alias(
    workflow_env,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, store, repo, _ = workflow_env
    task = store.add_task(repo, "work", engine=Engine.ANTIGRAVITY)
    store.set_status(task.id, TaskStatus.NEEDS_REVIEW)
    monkeypatch.setattr("agent_orchestrator.cli.load_config", lambda _root=None: config)

    main(["stats", "--engine", "agy", "--group-by", "engine", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert payload["totals"]["tasks"] == 1
    assert payload["totals"]["success_rate"] == 1.0
    assert list(payload["groups"]) == ["antigravity"]


def test_cli_stats_rejects_a_bad_iso_datetime(
    workflow_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _, _, _ = workflow_env
    monkeypatch.setattr("agent_orchestrator.cli.load_config", lambda _root=None: config)

    with pytest.raises(SystemExit, match="invalid --since datetime.*ISO 8601"):
        main(["stats", "--since", "not-a-date"])


def test_cli_stats_human_output_marks_partial_cost_and_uses_seconds(
    workflow_env,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, store, repo, _ = workflow_env
    task = store.add_task(repo, "work", engine=Engine.CODEX)
    store.update_task(task.id, status=TaskStatus.NEEDS_REVIEW, engine_ms=1_500)
    monkeypatch.setattr("agent_orchestrator.cli.load_config", lambda _root=None: config)

    main(["stats"])
    output = capsys.readouterr().out

    assert "cost_usd: 0.0000 (0/1 terminal tasks reported; no cost from codex)" in output
    assert "engine_s_total: 1.5" in output
    assert "engine_ms" not in output


def test_cli_stats_human_output_renders_quota_windows(
    workflow_env,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, store, repo, _ = workflow_env
    codex = store.add_task(repo, "codex", engine=Engine.CODEX)
    grok = store.add_task(repo, "grok", engine=Engine.GROK)
    reset = datetime(2026, 8, 18, 0, 47, 55, tzinfo=UTC)
    store.update_task(
        codex.id,
        status=TaskStatus.NEEDS_REVIEW,
        plan_type="plus",
        quota_used_pct=4.0,
        quota_window_minutes=10080,
        quota_resets_at=reset,
    )
    store.update_task(
        grok.id,
        status=TaskStatus.NEEDS_REVIEW,
        plan_type="team",
        quota_used_pct=8.0,
        quota_window_minutes=300,
    )
    monkeypatch.setattr("agent_orchestrator.cli.load_config", lambda _root=None: config)

    main(["stats"])
    output = capsys.readouterr().out

    assert (
        "quota: codex 4.0% of a 7d window "
        "(plan=plus, resets 2026-08-18T00:47Z)" in output
    )
    assert "quota: grok 8.0% of a 5h window (plan=team, resets -)" in output


def test_mcp_run_lifecycle_uses_ordered_snapshot_and_parent(workflow_env) -> None:
    config, _, repo, _ = workflow_env
    server = build_server(config)

    created = tool(server, "orch_run_create")(
        repo=str(repo),
        routes={"ui_verify": "agy", "implement": "codex"},
        fallbacks={"implement": ["claude", "agy"]},
    )
    dispatched = tool(server, "orch_run_dispatch")(
        run_id=created["workflow_id"],
        task="work",
        kind="implement",
        priority="high",
        parent_id="task-parent",
        base_ref="main",
    )
    shown = tool(server, "orch_workflow_show")(created["workflow_id"])
    listed = tool(server, "orch_workflow_list")("run")
    closed = tool(server, "orch_run_close")(created["workflow_id"])

    assert [route["kind"] for route in created["routes"]] == [kind.value for kind in TaskKind]
    ui_route = next(route for route in created["routes"] if route["kind"] == "ui_verify")
    assert ui_route["primary"] == "antigravity"
    assert dispatched["parent_id"] == "task-parent"
    assert dispatched["spawn_error"] is None
    assert shown["task_ids"] == [dispatched["task_id"]]
    assert shown["tasks"][0]["priority"] == "high"
    assert shown["tasks"][0]["base_ref"] == "main"
    assert shown["totals"]["tasks"] == 1
    assert [workflow["workflow_id"] for workflow in listed["workflows"]] == [
        created["workflow_id"]
    ]
    assert closed["status"] == "closed"
    with pytest.raises(DispatchError, match="not one of"):
        tool(server, "orch_workflow_list")("other")


def test_cli_run_lifecycle_uses_public_json_contract(
    workflow_env,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, _, repo, _ = workflow_env
    monkeypatch.setattr("agent_orchestrator.cli.load_config", lambda _root=None: config)

    main(
        [
            "run",
            "create",
            "--repo",
            str(repo),
            "--route",
            "ui_verify=agy",
            "--json",
        ]
    )
    created = json.loads(capsys.readouterr().out)
    main(
        [
            "run",
            "dispatch",
            created["workflow_id"],
            "--task",
            "work",
            "--parent",
            "task-parent",
            "--base-ref",
            "main",
            "--json",
        ]
    )
    dispatched = json.loads(capsys.readouterr().out)
    main(["run", "show", created["workflow_id"], "--json"])
    shown = json.loads(capsys.readouterr().out)
    main(["run", "list", "--json"])
    listed = json.loads(capsys.readouterr().out)
    main(["run", "close", created["workflow_id"], "--json"])
    closed = json.loads(capsys.readouterr().out)

    ui_route = next(route for route in created["routes"] if route["kind"] == "ui_verify")
    assert ui_route["primary"] == "antigravity"
    assert dispatched["parent_id"] == "task-parent"
    assert dispatched["spawn_error"] is None
    assert shown["task_ids"] == [dispatched["task_id"]]
    assert listed["workflows"][0]["workflow_id"] == created["workflow_id"]
    assert closed["status"] == "closed"


def test_mcp_batch_preserves_order_and_returns_waitable_task_ids(workflow_env) -> None:
    config, _, repo, _ = workflow_env
    server = build_server(config)

    batch = tool(server, "orch_batch_dispatch")(
        repo=str(repo),
        routes={"ui_verify": "agy"},
        tasks=[
            {"task": "third-looking first", "kind": "review"},
            {"task": "first-looking second", "kind": "implement"},
        ],
    )

    assert batch["type"] == "batch"
    assert batch["task_ids"] == [task["task_id"] for task in batch["tasks"]]
    assert [task["task"] for task in batch["tasks"]] == [
        "third-looking first",
        "first-looking second",
    ]
    assert all(task["worker_pid"] is not None for task in batch["tasks"])
    assert all(task["spawn_error"] is None for task in batch["tasks"])


def test_mcp_batch_reports_partial_spawn_failure_and_keeps_order(
    workflow_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _, repo, spawned = workflow_env

    def spawn_worker(_config: Config, task_id: str) -> int:
        spawned.append(task_id)
        if len(spawned) == 2:
            raise OSError("cannot spawn")
        return 5000 + len(spawned)

    monkeypatch.setattr("agent_orchestrator.workflows.spawn_worker", spawn_worker)
    batch = tool(build_server(config), "orch_batch_dispatch")(
        repo=str(repo),
        tasks=[{"task": "one"}, {"task": "two"}, {"task": "three"}],
    )

    assert batch["task_ids"] == [task["task_id"] for task in batch["tasks"]]
    assert batch["task_ids"] == spawned
    assert [task["worker_pid"] for task in batch["tasks"]] == [5001, None, 5003]
    assert [task["spawn_error"] for task in batch["tasks"]] == [
        None,
        "OSError: cannot spawn",
        None,
    ]


def test_cli_batch_reports_partial_spawn_failure_and_keeps_json_contract(
    workflow_env,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, _, repo, spawned = workflow_env
    tasks_path = repo.parent / "tasks.json"
    tasks_path.write_text(
        json.dumps([{"task": "one"}, {"task": "two"}, {"task": "three"}]),
        encoding="utf-8",
    )

    def spawn_worker(_config: Config, task_id: str) -> int:
        spawned.append(task_id)
        if len(spawned) == 2:
            raise OSError("cannot spawn")
        return 6000 + len(spawned)

    monkeypatch.setattr("agent_orchestrator.cli.load_config", lambda _root=None: config)
    monkeypatch.setattr("agent_orchestrator.workflows.spawn_worker", spawn_worker)
    main(
        [
            "batch",
            "dispatch",
            "--repo",
            str(repo),
            "--tasks-file",
            str(tasks_path),
            "--json",
        ]
    )
    batch = json.loads(capsys.readouterr().out)

    assert batch["task_ids"] == [task["task_id"] for task in batch["tasks"]]
    assert batch["task_ids"] == spawned
    assert [task["worker_pid"] for task in batch["tasks"]] == [6001, None, 6003]
    assert [task["spawn_error"] for task in batch["tasks"]] == [
        None,
        "OSError: cannot spawn",
        None,
    ]


def test_human_batch_output_surfaces_spawn_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _print_workflow(
        {
            "workflow_id": "batch-0001",
            "type": "batch",
            "status": "sealed",
            "repo": "/repo",
            "routes": [],
            "tasks": [
                {
                    "task_id": "task-0001",
                    "engine": "codex",
                    "task": "work",
                    "spawn_error": "OSError: cannot spawn",
                }
            ],
        }
    )

    assert "spawn_error: OSError: cannot spawn" in capsys.readouterr().out


def test_human_run_dispatch_output_surfaces_spawn_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _print_workflow_dispatch(
        {
            "task_id": "task-0001",
            "engine": "codex",
            "branch": "orch/task-0001",
            "worker_pid": None,
            "spawn_error": "OSError: cannot spawn",
        }
    )

    assert "spawn_error: OSError: cannot spawn" in capsys.readouterr().out


def test_mcp_rejects_fallback_without_explicit_route(workflow_env) -> None:
    config, _, repo, _ = workflow_env

    with pytest.raises(DispatchError, match="matching explicit routes entry"):
        tool(build_server(config), "orch_run_create")(
            repo=str(repo), fallbacks={"review": ["grok"]}
        )


def test_legacy_cli_and_mcp_accept_agy_but_serialize_canonical_name(
    workflow_env,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, _, repo, _ = workflow_env
    monkeypatch.setattr(
        "agent_orchestrator.dispatch.probe_all",
        lambda refresh=False: {engine: _capability(engine) for engine in Engine},
    )
    monkeypatch.setattr("agent_orchestrator.dispatch.spawn_worker", lambda cfg, task_id: 4242)

    main(
        [
            "--runtime-root",
            str(config.runtime_root),
            "dispatch",
            "--repo",
            str(repo),
            "--task",
            "cli",
            "--engine",
            "agy",
            "--json",
        ]
    )
    cli_payload = json.loads(capsys.readouterr().out)
    mcp_payload = tool(build_server(config), "orch_dispatch")(
        repo=str(repo), task="mcp", engine="agy"
    )

    assert cli_payload["engine"] == "antigravity"
    assert mcp_payload["engine"] == "antigravity"


def test_cli_install_command_wiring(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "commands" / "orch.md"

    main(["install-claude-command", "--target", str(target), "--locale", "ja"])

    assert target.exists()
    assert "argument-hint: <依頼する作業>" in target.read_text(encoding="utf-8")
    assert f"installed Claude command: {target}" in capsys.readouterr().out

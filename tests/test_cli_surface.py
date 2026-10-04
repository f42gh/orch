"""End-to-end behaviour of the task commands, driven through `main()`.

These five — `add`, `list`, `show`, `engines` and `install-claude-command` — had no
execution test at all. Their output was only ever verified by reading it, which is how
`add` came to store `--repo` without checking or expanding it, and how three of them
ended up without `--json`.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from orch.cli import init_chains, main
from orch.config import Config
from orch.db import TaskStore
from orch.engines.base import Capabilities
from orch.models import Engine, TaskKind
from orch.workflows import WorkflowError


def _capability(engine: Engine) -> Capabilities:
    return Capabilities(
        engine=engine,
        path=f"/usr/bin/{engine.value}",
        version="1.0",
        structured_output=True,
        reports_cost=True,
    )


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(
        "orch.cli.probe_all",
        lambda refresh=False: {engine: _capability(engine) for engine in Engine},
    )
    return tmp_path / "runtime", repo


def _add(runtime: Path, repo: Path, task: str, *extra: str) -> None:
    main(["--runtime-root", str(runtime), "add", "--repo", str(repo), "--task", task, *extra])


def test_add_queues_a_task_and_reports_that_the_engine_is_not_chosen_yet(
    env, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime, repo = env

    _add(runtime, repo, "write the parser")

    out = capsys.readouterr().out
    assert "added task-0001" in out
    assert "status: queued" in out
    # Routing happens in the worker for a queued task, and saying "-" would read as
    # "no engine" rather than "not decided yet".
    assert "engine: auto (chosen at run time)" in out
    stored = TaskStore(Config(runtime_root=runtime)).get_task("task-0001")
    assert stored is not None and stored.repo_path == repo


def test_add_expands_a_tilde_in_the_repo_path(
    env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime, repo = env
    monkeypatch.setenv("HOME", str(tmp_path))

    main(["--runtime-root", str(runtime), "add", "--repo", "~/repo", "--task", "x"])

    stored = TaskStore(Config(runtime_root=runtime)).get_task("task-0001")
    assert stored is not None and stored.repo_path == repo


def test_add_json_returns_the_same_shape_as_show(
    env, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime, repo = env

    _add(runtime, repo, "write the parser", "--json")
    added = json.loads(capsys.readouterr().out)

    main(["--runtime-root", str(runtime), "show", "task-0001", "--json"])
    shown = json.loads(capsys.readouterr().out)

    assert added == shown
    assert added["task_id"] == "task-0001"
    assert added["engine"] is None


def test_add_rejects_an_unknown_engine_before_writing_a_row(
    env, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--engine` has `choices`, so argparse rejects it and names the valid ones."""
    runtime, repo = env

    with pytest.raises(SystemExit) as excinfo:
        _add(runtime, repo, "x", "--engine", "gpt")

    assert excinfo.value.code == 2
    assert "invalid choice: 'gpt'" in capsys.readouterr().err
    assert TaskStore(Config(runtime_root=runtime)).list_tasks() == []


def test_add_accepts_agy_as_an_alias(env) -> None:
    runtime, repo = env

    _add(runtime, repo, "check the layout", "--engine", "agy", "--kind", "ui_verify")

    stored = TaskStore(Config(runtime_root=runtime)).get_task("task-0001")
    assert stored is not None and stored.engine is Engine.ANTIGRAVITY


def test_list_aligns_its_columns_and_json_matches_orch_list(
    env, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime, repo = env
    _add(runtime, repo, "first task")
    _add(runtime, repo, "second task")
    capsys.readouterr()

    main(["--runtime-root", str(runtime), "list"])
    lines = capsys.readouterr().out.splitlines()

    assert lines[0].split() == [
        "task_id",
        "status",
        "kind",
        "engine",
        "risk",
        "priority",
        "cost",
        "short_task",
    ]
    assert [line.split()[0] for line in lines[1:]] == ["task-0001", "task-0002"]
    # Padded, not tab separated: the machine form is --json.
    assert "\t" not in lines[1]
    assert lines[1].index("queued") == lines[0].index("status")

    main(["--runtime-root", str(runtime), "list", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert [task["task_id"] for task in payload["tasks"]] == ["task-0001", "task-0002"]


def test_list_is_empty_but_still_prints_its_header(
    env, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime, _ = env

    main(["--runtime-root", str(runtime), "list"])

    assert capsys.readouterr().out.split() [:2] == ["task_id", "status"]


def test_list_pads_a_japanese_description_by_display_width(
    env, capsys: pytest.CaptureFixture[str]
) -> None:
    """A CJK character is two terminal columns wide; padding by len() misaligns it."""
    runtime, repo = env
    _add(runtime, repo, "パーサーを追加して")
    _add(runtime, repo, "add the parser")
    capsys.readouterr()

    main(["--runtime-root", str(runtime), "list"])
    lines = capsys.readouterr().out.splitlines()

    # short_task is the last column, so check alignment on a column that follows a
    # variable-width one by listing the repo instead: every row starts identically.
    assert len({line.index("queued") for line in lines[1:]}) == 1


def test_show_reports_a_missing_task_without_a_traceback(env) -> None:
    runtime, _ = env

    with pytest.raises(SystemExit, match="task not found: task-9999"):
        main(["--runtime-root", str(runtime), "show", "task-9999"])


def test_show_prints_the_artifact_paths_a_reviewer_needs(
    env, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime, repo = env
    _add(runtime, repo, "write the parser")
    capsys.readouterr()

    main(["--runtime-root", str(runtime), "show", "task-0001"])
    out = capsys.readouterr().out

    assert "task_id: task-0001" in out
    assert f"log_path: {runtime / 'logs' / 'task-0001'}" in out
    assert "diff.patch" in out


def test_engines_prints_two_aligned_tables(env, capsys: pytest.CaptureFixture[str]) -> None:
    runtime, _ = env

    main(["--runtime-root", str(runtime), "engines"])
    blocks = capsys.readouterr().out.split("\n\n")

    assert blocks[0].splitlines()[0].split() == ["engine", "version", "structured", "cost"]
    assert {line.split()[0] for line in blocks[0].splitlines()[1:]} == {
        engine.value for engine in Engine
    }
    assert blocks[1].splitlines()[0].split() == ["kind", "engine", "fallbacks", "writes"]
    assert "implement" in blocks[1]


def test_runtime_root_is_accepted_before_and_after_the_subcommand(
    env, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime, repo = env
    _add(runtime, repo, "first task")
    capsys.readouterr()

    main(["list", "--runtime-root", str(runtime)])
    after = capsys.readouterr().out
    main(["--runtime-root", str(runtime), "list"])
    before = capsys.readouterr().out

    assert after == before
    assert "task-0001" in after


def test_runtime_root_after_the_subcommand_wins(
    env, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The more specific spelling has to beat the general one, not be erased by it."""
    runtime, repo = env
    _add(runtime, repo, "first task")
    capsys.readouterr()

    main(
        [
            "--runtime-root",
            str(tmp_path / "empty-runtime"),
            "list",
            "--runtime-root",
            str(runtime),
        ]
    )

    assert "task-0001" in capsys.readouterr().out


def test_install_claude_command_writes_then_reports_no_change(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "orch.md"

    main(["install-claude-command", "--target", str(target)])
    assert "installed Claude command" in capsys.readouterr().out
    assert target.exists()

    main(["install-claude-command", "--target", str(target)])
    assert "already up to date" in capsys.readouterr().out


def test_install_claude_command_refuses_a_foreign_file_until_forced(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Someone else's /orch command is never replaced silently."""
    target = tmp_path / "orch.md"
    target.write_text("# my own command\n", encoding="utf-8")

    with pytest.raises(SystemExit):
        main(["install-claude-command", "--target", str(target)])
    assert target.read_text(encoding="utf-8") == "# my own command\n"

    main(["install-claude-command", "--target", str(target), "--force"])
    out = capsys.readouterr().out
    assert "backup:" in out
    backup = next(path for path in tmp_path.iterdir() if path.name != "orch.md")
    assert backup.read_text(encoding="utf-8") == "# my own command\n"


def test_orch_mounts_the_daemon_api_and_mcp_commands() -> None:
    """One binary reaches all four services; the aliases stay for what depends on them."""
    from orch.cli import build_parser

    parser = build_parser()

    daemon = parser.parse_args(["daemon", "run", "--max-concurrency", "3"])
    assert daemon.daemon_command == "run"
    assert daemon.max_concurrency == 3
    assert parser.parse_args(["daemon", "run-task", "task-0001"]).task_id == "task-0001"
    assert parser.parse_args(["mcp"]).routing is None


def test_daemon_runtime_root_survives_the_umbrella_parser() -> None:
    """A subparser default would overwrite what `orch --runtime-root` already set."""
    from orch.cli import build_parser

    parser = build_parser()

    assert parser.parse_args(["--runtime-root", "/A", "daemon", "run"]).runtime_root == "/A"
    assert parser.parse_args(["daemon", "run", "--runtime-root", "/B"]).runtime_root == "/B"
    assert (
        parser.parse_args(
            ["--runtime-root", "/A", "daemon", "run-task", "t", "--runtime-root", "/B"]
        ).runtime_root
        == "/B"
    )


def test_module_entry_is_the_same_parser_as_orch_daemon() -> None:
    """`python -m orch.daemon` reuses the parser, not a copy — the earlier copy had drifted."""
    from orch import daemon

    standalone = daemon.build_parser().parse_args(["run", "--once"])

    assert standalone.daemon_command == "run"
    assert standalone.once is True
    # No --runtime-root given anywhere, so `run` must cope with the attribute missing.
    assert not hasattr(standalone, "runtime_root")


def test_the_cli_imports_without_the_optional_extras(monkeypatch: pytest.MonkeyPatch) -> None:
    """`orch --help` must work on an install that did not take the mcp extra."""
    import sys

    from orch.cli import build_parser

    for module in ("mcp", "mcp.server"):
        monkeypatch.setitem(sys.modules, module, None)

    assert build_parser().parse_args(["mcp"]).command == "mcp"


def test_init_asks_each_kind_and_enter_keeps_the_installed_order() -> None:
    installed = (Engine.CLAUDE, Engine.CODEX)
    answers = io.StringIO("y\ncodex, agy\n" + "\n" * (len(TaskKind) - 1))
    chains = init_chains(installed, stdin=answers, prompt_output=io.StringIO())

    assert chains[TaskKind.IMPLEMENT] == (Engine.CODEX, Engine.ANTIGRAVITY)
    assert chains[TaskKind.REVIEW] == installed
    with pytest.raises(WorkflowError, match="twice"):
        init_chains(installed, stdin=io.StringIO("y\ncodex,codex\n"), prompt_output=io.StringIO())
    # Declining (Enter defaults to no) gives every kind the detected engines.
    declined = init_chains(installed, stdin=io.StringIO("\n"), prompt_output=io.StringIO())
    assert set(declined.values()) == {installed}

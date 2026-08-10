"""Adapters are tested against output recorded from the real CLIs.

The fixtures in tests/fixtures/ came from actual runs (see docs/engine-capabilities.md),
so these tests catch a vendor changing their output shape without spending API calls.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_orchestrator.engines.antigravity import AntigravityAdapter
from agent_orchestrator.engines.base import Capabilities, RunSpec
from agent_orchestrator.engines.claude import ClaudeAdapter
from agent_orchestrator.engines.codex import CodexAdapter
from agent_orchestrator.engines.grok import GrokAdapter
from agent_orchestrator.models import Engine, Priority, Risk, Task, TaskKind, TaskStatus
from agent_orchestrator.router import AccessLevel, EnginePolicy

FIXTURES = Path(__file__).parent / "fixtures"


def make_task(kind: TaskKind = TaskKind.IMPLEMENT, risk: Risk = Risk.NORMAL) -> Task:
    now = datetime.now(UTC)
    return Task(
        id="task-0001",
        repo_path=Path("/repo"),
        task="do the thing",
        risk=risk,
        priority=Priority.NORMAL,
        status=TaskStatus.RUNNING,
        created_at=now,
        updated_at=now,
        kind=kind,
        workspace_path=Path("/ws"),
    )


def policy(access: AccessLevel = AccessLevel.WORKSPACE_WRITE, **kwargs: object) -> EnginePolicy:
    defaults: dict[str, object] = {
        "access": access,
        "max_turns": 40,
        "timeout_s": 1800,
        "deny_rules": ("Bash(git push:*)",),
        "allow_dangerous": False,
    }
    defaults.update(kwargs)
    return EnginePolicy(**defaults)  # type: ignore[arg-type]


# --- codex ---------------------------------------------------------------------------


def test_codex_parses_recorded_jsonl(tmp_path: Path) -> None:
    last_message = tmp_path / "last.md"
    last_message.write_text("README.md, calc.py", encoding="utf-8")
    spec = RunSpec(argv=[], last_message_path=last_message)

    result = CodexAdapter().parse(
        (FIXTURES / "codex_exec.jsonl").read_text(encoding="utf-8"), "", 0, spec
    )

    assert result.text == "README.md, calc.py"
    assert result.session_id == "019fea4e-fc67-71f0-b4da-d38860067426"
    assert result.usage is not None
    assert result.usage["input_tokens"] == 29879
    # codex reports no cost; inventing one would corrupt the budget view.
    assert result.cost_usd is None
    assert result.warnings == ()


def test_codex_prefers_the_last_message_file_over_the_stream(tmp_path: Path) -> None:
    last_message = tmp_path / "last.md"
    last_message.write_text("authoritative answer", encoding="utf-8")
    stdout = '{"type":"item.completed","item":{"type":"agent_message","text":"streamed"}}'

    result = CodexAdapter().parse(stdout, "", 0, RunSpec(argv=[], last_message_path=last_message))

    assert result.text == "authoritative answer"


def test_codex_survives_a_truncated_stream(tmp_path: Path) -> None:
    stdout = (
        '{"type":"thread.started","thread_id":"abc"}\n'
        '{"type":"item.completed","item":{"type":"agent_message","text":"partial work"}}\n'
        '{"type":"turn.comp'  # killed mid-line
    )

    result = CodexAdapter().parse(stdout, "", 0, RunSpec(argv=[]))

    assert result.text == "partial work"
    assert result.session_id == "abc"
    assert any("unparseable JSONL" in warning for warning in result.warnings)


def test_codex_command_maps_access_to_sandbox(tmp_path: Path) -> None:
    adapter = CodexAdapter()

    read_only = adapter.build(make_task(), policy(AccessLevel.READ_ONLY), Path("/ws"), "p", tmp_path)
    writing = adapter.build(make_task(), policy(), Path("/ws"), "p", tmp_path)

    assert "--sandbox" in read_only.argv
    assert read_only.argv[read_only.argv.index("--sandbox") + 1] == "read-only"
    assert writing.argv[writing.argv.index("--sandbox") + 1] == "workspace-write"
    assert writing.argv[writing.argv.index("--cd") + 1] == "/ws"
    assert writing.last_message_path is not None


def test_codex_review_gets_a_schema_file(tmp_path: Path) -> None:
    spec = CodexAdapter().build(
        make_task(TaskKind.REVIEW), policy(AccessLevel.READ_ONLY), Path("/ws"), "p", tmp_path
    )

    assert "--output-schema" in spec.argv
    schema_path = Path(spec.argv[spec.argv.index("--output-schema") + 1])
    assert schema_path in spec.files
    assert '"findings"' in spec.files[schema_path]


# --- grok ----------------------------------------------------------------------------


def test_grok_parses_recorded_json() -> None:
    result = GrokAdapter().parse(
        (FIXTURES / "grok_result.json").read_text(encoding="utf-8"), "", 0, RunSpec(argv=[])
    )

    assert "calc.py" in result.text
    assert result.session_id
    assert result.cost_usd is not None and result.cost_usd > 0
    assert result.usage is not None
    assert result.warnings == ()


def test_grok_drops_cost_when_the_server_calls_it_partial() -> None:
    stdout = '{"text":"ok","stopReason":"end_turn","total_cost_usd":0.5,"cost_is_partial":true}'

    result = GrokAdapter().parse(stdout, "", 0, RunSpec(argv=[]))

    assert result.cost_usd is None
    assert any("partial" in warning for warning in result.warnings)


def test_grok_flags_an_early_stop() -> None:
    stdout = '{"text":"ok","stopReason":"max_tokens"}'

    result = GrokAdapter().parse(stdout, "", 0, RunSpec(argv=[]))

    assert any("max_tokens" in warning for warning in result.warnings)


def test_grok_reports_an_error_object() -> None:
    stdout = '{"type":"error","message":"Couldn\'t start session"}'

    result = GrokAdapter().parse(stdout, "", 1, RunSpec(argv=[]))

    assert result.exit_code == 1
    assert "Couldn't start session" in result.text


def test_grok_command_disables_nested_subagents_and_passes_deny_rules(tmp_path: Path) -> None:
    spec = GrokAdapter().build(make_task(), policy(), Path("/ws"), "p", tmp_path)

    assert "--no-subagents" in spec.argv
    assert spec.argv[spec.argv.index("--sandbox") + 1] == "workspace"
    assert spec.argv[spec.argv.index("--deny") + 1] == "Bash(git push:*)"
    assert spec.argv[spec.argv.index("--max-turns") + 1] == "40"


# --- antigravity ---------------------------------------------------------------------


def test_antigravity_parses_plain_text() -> None:
    adapter = AntigravityAdapter(capabilities=_agy_capabilities(structured=False))

    result = adapter.parse(
        (FIXTURES / "agy_print.txt").read_text(encoding="utf-8"), "", 0, RunSpec(argv=[])
    )

    assert "calc.py" in result.text
    assert result.session_id is None
    assert result.cost_usd is None


def test_antigravity_notices_it_worked_on_the_wrong_tree() -> None:
    adapter = AntigravityAdapter(capabilities=_agy_capabilities(structured=False))

    result = adapter.parse(
        (FIXTURES / "agy_print_no_workspace.txt").read_text(encoding="utf-8"), "", 0, RunSpec(argv=[])
    )

    assert any("--add-dir" in warning for warning in result.warnings)


def test_antigravity_treats_empty_output_as_the_known_bug() -> None:
    adapter = AntigravityAdapter(capabilities=_agy_capabilities(structured=False))

    result = adapter.parse("", "", 0, RunSpec(argv=[]))

    assert any("no output" in warning for warning in result.warnings)


def test_antigravity_always_passes_add_dir(tmp_path: Path) -> None:
    adapter = AntigravityAdapter(capabilities=_agy_capabilities(structured=False))

    spec = adapter.build(make_task(TaskKind.UI_VERIFY), policy(), Path("/ws"), "p", tmp_path)

    assert spec.argv[spec.argv.index("--add-dir") + 1] == "/ws"
    assert "--sandbox" in spec.argv
    # This build has no --output-format, so the adapter must not pass one.
    assert "--output-format" not in spec.argv


def test_antigravity_promotes_itself_when_the_binary_grows_json(tmp_path: Path) -> None:
    adapter = AntigravityAdapter(capabilities=_agy_capabilities(structured=True))

    spec = adapter.build(make_task(TaskKind.UI_VERIFY), policy(), Path("/ws"), "p", tmp_path)

    assert spec.argv[spec.argv.index("--output-format") + 1] == "json"


def _agy_capabilities(structured: bool) -> Capabilities:
    return Capabilities(
        engine=Engine.ANTIGRAVITY,
        path="/usr/local/bin/agy",
        version="1.0.12",
        structured_output=structured,
        reports_cost=False,
    )


# --- claude --------------------------------------------------------------------------


def test_claude_parses_recorded_json() -> None:
    result = ClaudeAdapter().parse(
        (FIXTURES / "claude_result.json").read_text(encoding="utf-8"), "", 0, RunSpec(argv=[])
    )

    assert result.text == "calc.py, README.md"
    assert result.session_id == "9a97504e-3898-47fa-b5c2-413b906a3d78"
    assert result.cost_usd == pytest.approx(0.0955896)
    assert result.warnings == ()


def test_claude_surfaces_permission_denials() -> None:
    stdout = '{"result":"blocked","is_error":false,"permission_denials":[{"tool":"Bash"}]}'

    result = ClaudeAdapter().parse(stdout, "", 0, RunSpec(argv=[]))

    assert any("denied by permissions" in warning for warning in result.warnings)


def test_claude_maps_access_to_permission_mode(tmp_path: Path) -> None:
    adapter = ClaudeAdapter()

    read_only = adapter.build(make_task(), policy(AccessLevel.READ_ONLY), Path("/ws"), "p", tmp_path)
    writing = adapter.build(make_task(), policy(), Path("/ws"), "p", tmp_path)

    assert read_only.argv[read_only.argv.index("--permission-mode") + 1] == "plan"
    assert writing.argv[writing.argv.index("--permission-mode") + 1] == "acceptEdits"


# --- shared safety invariant ---------------------------------------------------------


#: The flag that actually removes each engine's containment.
#:
#: grok is deliberately absent from this list for `--permission-mode bypassPermissions`:
#: it always runs always-approve, because for grok the kernel sandbox profile is the
#: real limit and approval prompts only hang an unattended process. Its escape is
#: `--sandbox off`. claude has no OS sandbox at all, so for claude the permission mode
#: *is* the containment.
ESCAPE_MARKERS: dict[str, tuple[str, ...]] = {
    "codex": ("--dangerously-bypass-approvals-and-sandbox", "danger-full-access"),
    "grok": ("--sandbox off",),
    "antigravity": ("--dangerously-skip-permissions",),
    "claude": ("bypassPermissions",),
}

ADAPTERS = {
    "codex": CodexAdapter(),
    "grok": GrokAdapter(),
    "antigravity": AntigravityAdapter(capabilities=_agy_capabilities(False)),
    "claude": ClaudeAdapter(),
}


@pytest.mark.parametrize("name", list(ADAPTERS))
@pytest.mark.parametrize("access", list(AccessLevel))
def test_no_adapter_escapes_its_containment_unless_opted_in(
    name: str, access: AccessLevel, tmp_path: Path
) -> None:
    """AccessLevel.FULL on its own must not be enough — `allow_dangerous` gates it."""
    spec = ADAPTERS[name].build(make_task(), policy(access), Path("/ws"), "p", tmp_path)

    joined = " ".join(spec.argv)
    for marker in ESCAPE_MARKERS[name]:
        assert marker not in joined


@pytest.mark.parametrize("name", list(ADAPTERS))
def test_opting_in_does_reach_the_engine(name: str, tmp_path: Path) -> None:
    """The opt-in has to actually work, or routing.toml would be a silent no-op."""
    unsandboxed = policy(AccessLevel.FULL, allow_dangerous=True)

    spec = ADAPTERS[name].build(make_task(), unsandboxed, Path("/ws"), "p", tmp_path)

    joined = " ".join(spec.argv)
    assert any(marker in joined for marker in ESCAPE_MARKERS[name])

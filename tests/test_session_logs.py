from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_orchestrator.session_logs import CodexSessionInfo, read_codex_session


FIXTURE = Path(__file__).parent / "fixtures" / "codex_rollout.jsonl"
SESSION_ID = "fixture-session-id"


def _rollout_path(home: Path, session_id: str = SESSION_ID) -> Path:
    directory = home / "sessions" / "2026" / "08" / "11"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"rollout-2026-08-11T10-04-14-{session_id}.jsonl"


def _install_fixture(home: Path, session_id: str = SESSION_ID) -> Path:
    path = _rollout_path(home, session_id)
    path.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def test_reads_model_effort_and_the_last_quota_snapshot(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    _install_fixture(home)

    info = read_codex_session(SESSION_ID, home)

    assert info == CodexSessionInfo(
        model="gpt-5.6-sol",
        effort="xhigh",
        plan_type="plus",
        quota_used_pct=4.0,
        quota_window_minutes=10080,
        quota_resets_at=datetime(2026, 8, 18, 0, 47, 55, tzinfo=UTC),
    )


def test_unknown_session_returns_none(tmp_path: Path) -> None:
    assert read_codex_session("unknown-session", tmp_path) is None


def test_empty_rollout_stays_distinct_from_no_rollout(tmp_path: Path) -> None:
    _rollout_path(tmp_path).write_text("", encoding="utf-8")

    info = read_codex_session(SESSION_ID, tmp_path)

    assert info == CodexSessionInfo(None, None, None, None, None, None)


def test_missing_rate_limits_keeps_the_model(tmp_path: Path) -> None:
    path = _rollout_path(tmp_path)
    path.write_text(
        "\n".join(
            (
                '{"type":"turn_context","payload":{"model":"gpt-test","effort":"low"}}',
                '{"type":"event_msg","payload":{"type":"token_count","info":{}}}',
            )
        ),
        encoding="utf-8",
    )

    info = read_codex_session(SESSION_ID, tmp_path)

    assert info is not None
    assert info.model == "gpt-test"
    assert info.effort == "low"
    assert info.plan_type is None
    assert info.quota_used_pct is None
    assert info.quota_window_minutes is None
    assert info.quota_resets_at is None


def test_truncated_final_line_returns_recovered_fields(tmp_path: Path) -> None:
    path = _rollout_path(tmp_path)
    path.write_text(
        "\n".join(
            (
                '{"type":"turn_context","payload":{"model":"gpt-test","effort":"medium"}}',
                '{"type":"event_msg","payload":{"type":"token_count","rate_limits":'
                '{"plan_type":"plus","primary":{"used_percent":2.0}}}}',
                '{"type":"event_msg","payload":{"type":"token_co',
            )
        ),
        encoding="utf-8",
    )

    info = read_codex_session(SESSION_ID, tmp_path)

    assert info is not None
    assert info.model == "gpt-test"
    assert info.plan_type == "plus"
    assert info.quota_used_pct == 2.0


def test_codex_home_environment_variable_is_honoured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "configured-codex-home"
    _install_fixture(home)
    monkeypatch.setenv("CODEX_HOME", str(home))

    info = read_codex_session(SESSION_ID)

    assert info is not None
    assert info.model == "gpt-5.6-sol"

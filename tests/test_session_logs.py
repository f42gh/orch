from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from orch.session_logs import CodexSessionInfo, read_codex_session


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


#: Enough of a rollout to be found and parsed, for tests about which file is selected.
MINIMAL_ROLLOUT: list[dict[str, object]] = [
    {"type": "turn_context", "payload": {"model": "gpt-5.6-sol", "effort": "xhigh"}}
]


def _write_rollout(home: Path, session_id: str, records: list[dict[str, object]]) -> Path:
    path = _rollout_path(home, session_id)
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )
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


def test_a_shorter_id_does_not_match_a_longer_session(tmp_path: Path) -> None:
    """`run-abc` is a genuine tail of the file named for session `team-run-abc`.

    A suffix test therefore attaches the wrong run's model and quota to the task.
    """
    home = tmp_path / "codex"
    _write_rollout(home, "team-run-abc", MINIMAL_ROLLOUT)

    assert read_codex_session("run-abc", home) is None
    assert read_codex_session("team-run-abc", home) is not None


def test_glob_metacharacters_in_an_id_do_not_act_as_wildcards(tmp_path: Path) -> None:
    """The id is engine output, not something this process generated."""
    home = tmp_path / "codex"
    _write_rollout(home, "team-run-abc", MINIMAL_ROLLOUT)

    assert read_codex_session("team*abc", home) is None
    assert read_codex_session("team-run-ab[c]", home) is None


def test_a_later_event_without_rate_limits_keeps_the_last_good_snapshot(
    tmp_path: Path,
) -> None:
    """A trailing token_count carrying only `info` is missing data, not a correction."""
    home = tmp_path / "codex"
    _write_rollout(
        home,
        "sess-1",
        [
            {"type": "turn_context", "payload": {"model": "gpt-5.6-sol"}},
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "rate_limits": {
                        "plan_type": "plus",
                        "primary": {
                            "used_percent": 4.0,
                            "window_minutes": 10080,
                            "resets_at": 1787014075,
                        },
                    },
                },
            },
            {"type": "event_msg", "payload": {"type": "token_count", "info": {}}},
        ],
    )

    info = read_codex_session("sess-1", home)

    assert info is not None
    assert info.plan_type == "plus"
    assert info.quota_used_pct == 4.0
    assert info.quota_window_minutes == 10080


def test_a_whole_number_window_written_as_a_float_still_parses(tmp_path: Path) -> None:
    """Dropping it would leave used_percent parsed beside a null window."""
    home = tmp_path / "codex"
    _write_rollout(
        home,
        "sess-2",
        [
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "rate_limits": {
                        "plan_type": "pro",
                        "primary": {"used_percent": 4, "window_minutes": 300.0},
                    },
                },
            }
        ],
    )

    info = read_codex_session("sess-2", home)

    assert info is not None
    assert info.quota_window_minutes == 300

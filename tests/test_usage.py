from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_orchestrator import usage as usage_module
from agent_orchestrator.cli import main
from agent_orchestrator.models import Engine
from agent_orchestrator.usage import (
    UsageReport,
    UsageWindow,
    collect_usage,
    read_antigravity_usage,
    read_claude_usage,
    read_codex_usage,
    read_grok_usage,
)


FIXTURES = Path(__file__).parent / "fixtures"
CODEX_ROLLOUT = FIXTURES / "codex_rollout.jsonl"
CLAUDE_CACHE = FIXTURES / "claude_usage_cache.json"
GROK_LOG = FIXTURES / "grok_unified_log.jsonl"

NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _installed_engines(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let a test's answer depend on what happens to be on the test machine."""
    monkeypatch.setattr(usage_module, "which", lambda binary: f"/usr/local/bin/{binary}")


def _write_rollout(home: Path, name: str, lines: str, *, mtime: float) -> Path:
    directory = home / "sessions" / "2026" / "08" / "11"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-2026-08-11T10-04-14-{name}.jsonl"
    path.write_text(lines, encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def _token_count(used_percent: float, *, plan: str = "plus", timestamp: str) -> str:
    record = {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "rate_limits": {
                "primary": {
                    "used_percent": used_percent,
                    "window_minutes": 10080,
                    "resets_at": 1787229512,
                },
                "plan_type": plan,
            },
        },
    }
    return json.dumps(record) + "\n"


def _grok_home(tmp_path: Path, text: str | None = None) -> Path:
    home = tmp_path / "grok"
    logs = home / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    if text is not None:
        (logs / "unified.jsonl").write_text(text, encoding="utf-8")
    return home


def test_codex_reports_the_last_reading_in_the_rollout(tmp_path: Path) -> None:
    _write_rollout(tmp_path, "one", CODEX_ROLLOUT.read_text(encoding="utf-8"), mtime=1000)

    result = read_codex_usage(tmp_path)

    assert result.plan == "plus"
    assert result.windows == (
        UsageWindow(
            label="primary",
            used_pct=4.0,
            window_minutes=10080,
            resets_at=datetime(2026, 8, 18, 0, 47, 55, tzinfo=UTC),
        ),
    )
    assert result.observed_at == datetime(2026, 8, 11, 1, 14, 33, 254000, tzinfo=UTC)
    assert result.notes == ()


def test_codex_picks_the_most_recently_written_rollout(tmp_path: Path) -> None:
    """Sessions are resumed, so the newest reading is not in the newest-named file.

    The filename carries the time the session started; a resumed session keeps writing
    to it for days. Ordering by name would report a week-old percentage as current.
    """
    _write_rollout(
        tmp_path, "old-name", _token_count(9.0, timestamp="2026-08-14T11:00:00.000Z"), mtime=9000
    )
    _write_rollout(
        tmp_path, "new-name", _token_count(2.0, timestamp="2026-08-11T09:00:00.000Z"), mtime=1000
    )

    result = read_codex_usage(tmp_path)

    assert [window.used_pct for window in result.windows] == [9.0]


def test_codex_skips_a_rollout_that_carries_no_reading(tmp_path: Path) -> None:
    _write_rollout(
        tmp_path,
        "newest",
        json.dumps({"type": "turn_context", "payload": {"model": "gpt-5.6-sol"}}) + "\n",
        mtime=9000,
    )
    _write_rollout(
        tmp_path, "older", _token_count(6.0, timestamp="2026-08-11T09:00:00.000Z"), mtime=1000
    )

    result = read_codex_usage(tmp_path)

    assert [window.used_pct for window in result.windows] == [6.0]


def test_codex_ignores_a_reading_older_than_the_tail_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tail bound is a real limit, so it must fail loudly rather than half-read.

    A record split by the start of the window decodes as garbage; dropping that first
    partial line is what keeps a truncated number from being reported as a reading.
    """
    monkeypatch.setattr(usage_module, "TAIL_BYTES", 120)
    padding = json.dumps({"type": "event_msg", "payload": {"type": "agent_message"}}) + "\n"
    _write_rollout(
        tmp_path,
        "one",
        _token_count(5.0, timestamp="2026-08-11T09:00:00.000Z") + padding * 4,
        mtime=1000,
    )

    result = read_codex_usage(tmp_path)

    assert result.windows == ()
    assert "rate_limits" in result.notes[0]


def test_codex_without_rollouts_says_where_it_looked(tmp_path: Path) -> None:
    result = read_codex_usage(tmp_path)

    assert result.windows == ()
    assert result.source is None
    assert str(tmp_path / "sessions") in result.notes[0]


def test_codex_reports_a_credit_balance_only_when_there_is_one(tmp_path: Path) -> None:
    record = json.loads(_token_count(1.0, timestamp="2026-08-11T09:00:00.000Z"))
    record["payload"]["rate_limits"]["credits"] = {
        "has_credits": True,
        "unlimited": False,
        "balance": "42",
    }
    _write_rollout(tmp_path, "one", json.dumps(record) + "\n", mtime=1000)

    result = read_codex_usage(tmp_path)

    assert [(window.label, window.detail) for window in result.windows] == [
        ("primary", None),
        ("credits", "balance 42"),
    ]


def test_claude_reads_every_window_the_cache_holds(tmp_path: Path) -> None:
    result = read_claude_usage(CLAUDE_CACHE)

    assert result.plan == "claude_pro"
    assert result.observed_at == datetime(2026, 8, 11, 13, 21, 30, 97000, tzinfo=UTC)
    assert [(window.label, window.used_pct, window.window_minutes) for window in result.windows] == [
        ("five_hour", 1.0, 300),
        ("seven_day", 57.0, 10080),
        # An unfamiliar window keeps its name and reports no length, because the name
        # is the only thing claude says about it.
        ("nimbus_quill", 0.0, None),
        ("extra_usage", 85.93, None),
    ]
    assert result.windows[-1].detail == "8593/10000 credits, disabled (out_of_credits)"


def test_claude_cache_older_than_its_reset_is_marked_expired() -> None:
    result = read_claude_usage(CLAUDE_CACHE)

    assert result.expired(NOW) is True
    assert result.describe(NOW)["age_seconds"] == pytest.approx(254309.903, abs=1)


def test_claude_without_a_cached_reading_still_reports_the_plan(tmp_path: Path) -> None:
    path = tmp_path / "claude.json"
    path.write_text(json.dumps({"oauthAccount": {"organizationType": "claude_max"}}))

    result = read_claude_usage(path)

    assert result.plan == "claude_max"
    assert result.windows == ()
    assert "/usage" in result.notes[0]


def test_claude_with_no_config_file_reports_the_path(tmp_path: Path) -> None:
    result = read_claude_usage(tmp_path / "absent.json")

    assert result.windows == ()
    assert "absent.json" in result.notes[0]


def test_grok_takes_the_last_billing_snapshot(tmp_path: Path) -> None:
    home = _grok_home(tmp_path, GROK_LOG.read_text(encoding="utf-8"))

    result = read_grok_usage(home)

    assert result.plan == "SuperGrok"
    assert result.observed_at == datetime(2026, 8, 11, 21, 23, 14, 225000, tzinfo=UTC)
    # 3.0 belongs to the later entry; 1.0 was logged by the same process minutes before.
    assert result.windows == (
        UsageWindow(
            label="credits",
            used_pct=3.0,
            window_minutes=10080,
            resets_at=datetime(2026, 8, 12, 21, 13, 46, 692431, tzinfo=UTC),
            detail="on-demand 25/500",
        ),
    )


def test_grok_ignores_unparseable_lines(tmp_path: Path) -> None:
    text = GROK_LOG.read_text(encoding="utf-8") + '{"ts":"2026-08-11T21:24:00.000Z"\n'
    home = _grok_home(tmp_path, text)

    result = read_grok_usage(home)

    assert [window.used_pct for window in result.windows] == [3.0]


def test_grok_without_a_billing_entry_names_the_log(tmp_path: Path) -> None:
    home = _grok_home(tmp_path, "")

    result = read_grok_usage(home)

    assert result.windows == ()
    assert str(home / "logs" / "unified.jsonl") in result.notes[0]


def test_antigravity_is_listed_with_the_reason_it_is_empty() -> None:
    result = read_antigravity_usage()

    assert result.engine is Engine.ANTIGRAVITY
    assert result.windows == ()
    assert result.notes and "no quota" in result.notes[0]


def test_collect_lists_every_engine_in_a_stable_order(tmp_path: Path) -> None:
    report = collect_usage(
        now=NOW,
        codex_home_path=tmp_path / "codex",
        claude_config_path=CLAUDE_CACHE,
        grok_home_path=_grok_home(tmp_path, GROK_LOG.read_text(encoding="utf-8")),
    )
    payload = report.describe()

    assert [entry["engine"] for entry in payload["engines"]] == [
        "codex",
        "claude",
        "grok",
        "antigravity",
    ]
    assert payload["collected_at"] == NOW.isoformat()
    assert all(entry["installed"] for entry in payload["engines"])
    # An engine that reported nothing still appears, with a note instead of numbers.
    assert payload["engines"][0]["windows"] == []
    assert payload["engines"][0]["notes"]


def _fixture_report(tmp_path: Path) -> UsageReport:
    return collect_usage(
        now=NOW,
        codex_home_path=tmp_path / "codex",
        claude_config_path=CLAUDE_CACHE,
        grok_home_path=_grok_home(tmp_path, GROK_LOG.read_text(encoding="utf-8")),
    )


def test_usage_command_lists_every_engine_and_flags_the_stale_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "agent_orchestrator.cli.collect_usage", lambda: _fixture_report(tmp_path)
    )
    monkeypatch.setattr(
        "sys.argv",
        ["orch", "--runtime-root", str(tmp_path / "runtime"), "usage"],
    )

    main()

    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split()[:4] == ["engine", "installed", "plan", "window"]
    footer = ("source:", "stale:", "note:")
    rows = {
        line.split()[0]
        for line in lines[1:]
        if line and not line.startswith(footer)
    }
    assert rows == {"codex", "claude", "grok", "antigravity"}
    claude_row = next(line for line in lines if line.startswith("claude "))
    # The reading is three days old and its window has turned over since.
    assert "elapsed" in claude_row
    assert any(line.startswith("stale: claude ") for line in lines)
    assert any("agy reports no quota" in line for line in lines)


def test_usage_command_does_not_create_the_runtime_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reading a quota must stay read-only: no runtime root, no database, no worktree."""
    runtime = tmp_path / "runtime"
    monkeypatch.setattr(
        "agent_orchestrator.cli.collect_usage", lambda: _fixture_report(tmp_path)
    )
    monkeypatch.setattr("sys.argv", ["orch", "--runtime-root", str(runtime), "usage", "--json"])

    main()

    payload = json.loads(capsys.readouterr().out)
    assert [entry["engine"] for entry in payload["engines"]] == [
        "codex",
        "claude",
        "grok",
        "antigravity",
    ]
    assert not runtime.exists()


def test_a_reading_ahead_of_its_reset_is_not_expired(tmp_path: Path) -> None:
    _write_rollout(
        tmp_path, "one", _token_count(8.0, timestamp="2026-08-14T11:00:00.000Z"), mtime=9000
    )

    result = read_codex_usage(tmp_path)

    assert result.expired(NOW) is False
    assert result.describe(NOW)["age_seconds"] == pytest.approx(3600, abs=1)

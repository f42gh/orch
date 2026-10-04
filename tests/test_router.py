from pathlib import Path

import pytest

from orch.models import Engine, Risk, TaskKind
from orch.router import (
    DEFAULT_TABLE,
    AccessLevel,
    RoutingError,
    load_routing_table,
    resolve_access,
    resolve_engine,
    resolve_policy,
)


ALL = set(Engine)


def test_each_kind_routes_to_its_specialist() -> None:
    assert resolve_engine(TaskKind.IMPLEMENT, ALL) == Engine.CODEX
    assert resolve_engine(TaskKind.REFACTOR, ALL) == Engine.CODEX
    assert resolve_engine(TaskKind.TEST, ALL) == Engine.CODEX
    assert resolve_engine(TaskKind.REVIEW, ALL) == Engine.GROK
    assert resolve_engine(TaskKind.INVESTIGATE, ALL) == Engine.GROK
    assert resolve_engine(TaskKind.UI_VERIFY, ALL) == Engine.ANTIGRAVITY


def test_missing_engine_falls_back_in_table_order() -> None:
    without_codex = ALL - {Engine.CODEX}
    assert resolve_engine(TaskKind.IMPLEMENT, without_codex) == Engine.CLAUDE
    assert resolve_engine(TaskKind.REFACTOR, without_codex) == Engine.GROK

    # antigravity is the flakiest engine, so ui_verify degrading to claude is the
    # behaviour that keeps the orchestrator usable when agy is not installed.
    assert resolve_engine(TaskKind.UI_VERIFY, {Engine.CLAUDE, Engine.CODEX}) == Engine.CLAUDE


def test_falls_back_to_any_installed_engine_when_table_is_exhausted() -> None:
    assert resolve_engine(TaskKind.UI_VERIFY, {Engine.GROK}) == Engine.GROK


def test_no_engine_available_is_an_error() -> None:
    with pytest.raises(RoutingError, match="no coding agent CLI"):
        resolve_engine(TaskKind.IMPLEMENT, set())


def test_explicit_request_wins_when_available() -> None:
    assert resolve_engine(TaskKind.REVIEW, ALL, requested=Engine.CODEX) == Engine.CODEX


def test_explicit_request_for_a_missing_engine_is_an_error() -> None:
    with pytest.raises(RoutingError, match="not available"):
        resolve_engine(TaskKind.REVIEW, {Engine.CLAUDE}, requested=Engine.CODEX)


def test_only_writing_kinds_at_normal_risk_may_write() -> None:
    assert resolve_access(TaskKind.IMPLEMENT, Risk.NORMAL) == AccessLevel.WORKSPACE_WRITE
    assert resolve_access(TaskKind.REVIEW, Risk.NORMAL) == AccessLevel.READ_ONLY
    # read_only and high are both non-writing, so a write-shaped kind is still clamped.
    assert resolve_access(TaskKind.IMPLEMENT, Risk.READ_ONLY) == AccessLevel.READ_ONLY
    assert resolve_access(TaskKind.IMPLEMENT, Risk.HIGH) == AccessLevel.READ_ONLY


def test_policy_carries_budget_and_never_allows_danger_by_default() -> None:
    policy = resolve_policy(TaskKind.IMPLEMENT, Risk.NORMAL, Engine.CODEX)

    assert policy.access == AccessLevel.WORKSPACE_WRITE
    assert policy.max_turns == 40
    assert policy.timeout_s == 1800
    assert policy.allow_dangerous is False
    assert "Bash(git push:*)" in policy.deny_rules


def test_read_only_risk_gets_the_tighter_budget() -> None:
    policy = resolve_policy(TaskKind.REVIEW, Risk.READ_ONLY, Engine.GROK)

    assert policy.max_turns == 12
    assert policy.timeout_s == 600


def test_missing_routing_file_yields_the_default_table(tmp_path: Path) -> None:
    assert load_routing_table(tmp_path / "absent.toml") is DEFAULT_TABLE
    assert load_routing_table(None) is DEFAULT_TABLE


def test_routing_file_overrides_only_what_it_mentions(tmp_path: Path) -> None:
    path = tmp_path / "routing.toml"
    path.write_text(
        """
        [kinds.implement]
        engine = "grok"
        fallbacks = ["claude"]

        [risk.normal]
        max_turns = 5
        """,
        encoding="utf-8",
    )

    table = load_routing_table(path)

    assert resolve_engine(TaskKind.IMPLEMENT, ALL, table=table) == Engine.GROK
    # Untouched entries keep their defaults.
    assert resolve_engine(TaskKind.REVIEW, ALL, table=table) == Engine.GROK
    assert resolve_policy(TaskKind.IMPLEMENT, Risk.NORMAL, Engine.GROK, table).max_turns == 5
    assert resolve_policy(TaskKind.IMPLEMENT, Risk.NORMAL, Engine.GROK, table).timeout_s == 1800


def test_dangerous_access_requires_explicit_opt_in(tmp_path: Path) -> None:
    path = tmp_path / "routing.toml"
    path.write_text(
        """
        [engines.codex]
        allow_dangerous = true
        """,
        encoding="utf-8",
    )

    table = load_routing_table(path)

    opted_in = resolve_policy(TaskKind.IMPLEMENT, Risk.NORMAL, Engine.CODEX, table)
    assert opted_in.allow_dangerous is True
    assert opted_in.access == AccessLevel.FULL

    # The opt-in is per engine, and never escalates a read-only task.
    assert resolve_policy(TaskKind.IMPLEMENT, Risk.NORMAL, Engine.GROK, table).allow_dangerous is False
    assert resolve_policy(TaskKind.REVIEW, Risk.NORMAL, Engine.CODEX, table).access == AccessLevel.READ_ONLY


def test_invalid_routing_file_names_the_offending_key(tmp_path: Path) -> None:
    path = tmp_path / "routing.toml"
    path.write_text(
        """
        [kinds.implement]
        engine = "gpt5"
        """,
        encoding="utf-8",
    )

    with pytest.raises(RoutingError, match="kinds.implement.engine"):
        load_routing_table(path)

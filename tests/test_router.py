import tomllib
from pathlib import Path

import pytest

from orch.models import Engine, Risk, TaskKind
from orch.router import (
    DEFAULT_TABLE,
    AccessLevel,
    RoutingError,
    RoutingTable,
    load_routing_table,
    resolve_access,
    resolve_engine,
    EngineRoute,
    default_chain,
    render_routing_toml,
    resolve_policy,
    write_routes,
)


ALL = set(Engine)


@pytest.fixture
def table(routing_file: Path) -> RoutingTable:
    return load_routing_table(routing_file)


def test_each_kind_routes_to_its_configured_engine(table: RoutingTable) -> None:
    assert resolve_engine(TaskKind.IMPLEMENT, ALL, table=table) == Engine.CODEX
    assert resolve_engine(TaskKind.REVIEW, ALL, table=table) == Engine.GROK
    assert resolve_engine(TaskKind.UI_VERIFY, ALL, table=table) == Engine.ANTIGRAVITY


def test_missing_engine_falls_back_in_table_order(table: RoutingTable) -> None:
    without_codex = ALL - {Engine.CODEX}
    assert resolve_engine(TaskKind.IMPLEMENT, without_codex, table=table) == Engine.CLAUDE
    assert resolve_engine(TaskKind.REFACTOR, without_codex, table=table) == Engine.GROK


def test_falls_back_to_any_installed_engine_when_table_is_exhausted(
    table: RoutingTable,
) -> None:
    assert resolve_engine(TaskKind.UI_VERIFY, {Engine.GROK}, table=table) == Engine.GROK


def test_no_routing_file_means_no_route_until_init() -> None:
    with pytest.raises(RoutingError, match="orch init"):
        resolve_engine(TaskKind.IMPLEMENT, ALL)
    # An explicit engine still runs without any configuration.
    assert resolve_engine(TaskKind.IMPLEMENT, ALL, requested=Engine.CLAUDE) == Engine.CLAUDE


def test_init_draft_routes_every_kind_without_preferring_an_engine() -> None:
    chain = default_chain({Engine.GROK, Engine.CLAUDE})
    draft = tomllib.loads(render_routing_toml({kind: chain for kind in TaskKind}))

    assert set(draft["kinds"]) == {kind.value for kind in TaskKind}
    assert draft["kinds"]["implement"] == {"engine": "claude", "fallbacks": ["grok"]}
    with pytest.raises(RoutingError, match="no coding agent CLI"):
        default_chain(set())


def test_writing_routes_keeps_the_dangerous_opt_in_and_budgets(tmp_path: Path) -> None:
    path = tmp_path / "routing.toml"
    path.write_text(
        """deny_rules = ["Bash(git push:*)"]

[kinds.implement]
engine = "codex"

[engines.codex]
allow_dangerous = true

[kinds.review]
engine = "grok"

[risk.normal]
max_turns = 5
""",
        encoding="utf-8",
    )

    table = write_routes(path, {TaskKind.IMPLEMENT: (Engine.CLAUDE, Engine.CODEX)})

    assert table.routes[TaskKind.IMPLEMENT] == EngineRoute(Engine.CLAUDE, (Engine.CODEX,))
    assert table.routes[TaskKind.REVIEW].engine == Engine.GROK
    assert table.dangerous_engines == frozenset({Engine.CODEX})
    assert table.budgets[Risk.NORMAL][0] == 5
    assert table.deny_rules == ("Bash(git push:*)",)
    with pytest.raises(RoutingError, match="at least one"):
        write_routes(path, {TaskKind.TEST: ()})


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


def test_routing_file_sets_only_what_it_mentions(tmp_path: Path) -> None:
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
    # Kinds the file does not mention are unrouted; budgets keep their defaults.
    with pytest.raises(RoutingError, match="no route for review"):
        resolve_engine(TaskKind.REVIEW, ALL, table=table)
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

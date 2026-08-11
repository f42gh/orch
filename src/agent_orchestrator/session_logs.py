"""Read metadata that engines omit from their captured stdout.

Adapters stay pure by accepting argv and bytes without reaching into undeclared
filesystem state. Codex writes its model and account quota only to its rollout, so
that filesystem access lives here and is called by the worker after parsing stdout.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class CodexSessionInfo:
    model: str | None
    effort: str | None
    plan_type: str | None
    quota_used_pct: float | None
    quota_window_minutes: int | None
    quota_resets_at: datetime | None


def codex_home(home: Path | None = None) -> Path:
    """Resolve the explicit home, Codex's `CODEX_HOME`, or its default directory."""
    if home is not None:
        return home.expanduser().resolve()
    configured = os.environ.get("CODEX_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path("~/.codex").expanduser().resolve()


def read_codex_session(
    session_id: str,
    home: Path | None = None,
) -> CodexSessionInfo | None:
    """Recover model and quota metadata without letting rollout damage escape."""
    # Rollout filenames use local time while task timestamps are UTC. Searching all
    # date directories measured 0.03s across 277 files and avoids midnight misses.
    try:
        root = codex_home(home)
        matches = sorted(root.glob(f"sessions/*/*/*/rollout-*-{session_id}.jsonl"))
    except (OSError, RuntimeError, ValueError):
        return None
    if not matches:
        return None

    model: str | None = None
    effort: str | None = None
    saw_turn_context = False
    plan_type: str | None = None
    quota_used_pct: float | None = None
    quota_window_minutes: int | None = None
    quota_resets_at: datetime | None = None

    try:
        with matches[0].open(encoding="utf-8", errors="replace") as rollout:
            for line in rollout:
                record = _load_record(line)
                if record is None:
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue
                if record.get("type") == "turn_context" and not saw_turn_context:
                    saw_turn_context = True
                    model = _string(payload.get("model"))
                    effort = _string(payload.get("effort"))
                if payload.get("type") != "token_count":
                    continue
                plan_type = None
                quota_used_pct = None
                quota_window_minutes = None
                quota_resets_at = None
                rate_limits = payload.get("rate_limits")
                if not isinstance(rate_limits, dict):
                    continue
                plan_type = _string(rate_limits.get("plan_type"))
                primary = rate_limits.get("primary")
                if not isinstance(primary, dict):
                    continue
                quota_used_pct = _float(primary.get("used_percent"))
                quota_window_minutes = _int(primary.get("window_minutes"))
                quota_resets_at = _unix_datetime(primary.get("resets_at"))
    except (OSError, ValueError):
        pass

    return CodexSessionInfo(
        model=model,
        effort=effort,
        plan_type=plan_type,
        quota_used_pct=quota_used_pct,
        quota_window_minutes=quota_window_minutes,
        quota_resets_at=quota_resets_at,
    )


def _load_record(line: str) -> dict[str, Any] | None:
    try:
        record = json.loads(line)
    except (json.JSONDecodeError, RecursionError):
        return None
    return record if isinstance(record, dict) else None


def _string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _float(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _unix_datetime(value: object) -> datetime | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OSError, OverflowError, ValueError):
        return None

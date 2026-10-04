"""Read what each engine records about the account quota it has already spent.

`stats` answers "what did the tasks in this database cost"; this module answers "how
much of each subscription is left", which is account-wide and mostly spent outside
orch. None of it comes from an API: every engine writes its own reading to disk, so
this reads those files and nothing else. No subprocess is spawned and no credential
file is opened — a usage listing must never be able to spend quota or leak a token.

What each engine actually offers, measured on this machine rather than taken from
documentation (see docs/engine-capabilities.md):

- codex writes `rate_limits` into every `token_count` event of its session rollout:
  the plan, a used percentage, the window length and a reset instant.
- claude caches the reading behind `/usage` in `~/.claude.json`, with the fetch time,
  so its age is knowable and often large.
- grok logs a billing snapshot to `~/.grok/logs/unified.jsonl` on startup: the tier,
  a credit percentage and the billing period whose end is the reset.
- antigravity offers nothing at all. It is listed with that as its reason rather than
  omitted, because a missing engine and an engine that cannot report look identical
  in a table that only shows what it found.

Every reading therefore carries where it came from and when it was taken. A stale
percentage presented as current is worse than no percentage.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from orch.engines.antigravity import BINARY as ANTIGRAVITY_BINARY
from orch.engines.base import which
from orch.engines.claude import BINARY as CLAUDE_BINARY
from orch.engines.codex import BINARY as CODEX_BINARY
from orch.engines.grok import BINARY as GROK_BINARY
from orch.models import Engine
from orch.session_logs import codex_home


#: How much of a log's tail is searched for the last reading. The records wanted here
#: are written at the end of a session, and a codex rollout can reach tens of MB, so
#: reading whole files would make a listing cost seconds of I/O for older data.
TAIL_BYTES = 512 * 1024

#: How many rollouts are opened, newest first, before codex is reported as unread. A
#: run that predates the newest few rollouts is too old to be worth reporting anyway.
CODEX_ROLLOUT_SCAN = 10

CLAUDE_CONFIG = "~/.claude.json"
GROK_HOME = "~/.grok"
GROK_BILLING_MESSAGE = "billing: fetched credits config"

#: What makes each engine write a fresh reading. None of them can be asked directly,
#: so an old figure is fixed by using the engine, not by re-running this listing.
CODEX_REFRESH_HINT = "codex records a reading on every turn it runs"
CLAUDE_REFRESH_HINT = "Claude Code refreshes the cache while it runs, or on /usage"
GROK_REFRESH_HINT = "grok logs a billing snapshot each time it starts"

#: Claude names its windows rather than stating their length; these two names do state
#: it. Anything else keeps its name and reports an unknown window rather than guessing.
CLAUDE_WINDOW_MINUTES = {"five_hour": 300}
CLAUDE_SEVEN_DAY_PREFIX = "seven_day"
SEVEN_DAY_MINUTES = 7 * 24 * 60

#: Keys of `utilization` that are not themselves windows.
CLAUDE_NON_WINDOW_KEYS = frozenset(
    {"extra_usage", "spend", "limits", "member_dashboard_available"}
)


@dataclass(frozen=True, slots=True)
class UsageWindow:
    """One limit an engine reports against, named the way the engine names it."""

    label: str
    #: Percent of the window consumed, 0-100, or None when the engine reports a
    #: balance without a percentage.
    used_pct: float | None
    window_minutes: int | None
    resets_at: datetime | None
    #: Anything the engine reports that is not a percentage, e.g. a credit balance.
    detail: str | None = None

    def describe(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "used_pct": self.used_pct,
            "window_minutes": self.window_minutes,
            "resets_at": self.resets_at.isoformat() if self.resets_at else None,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class EngineUsage:
    """Everything one engine will say about its account, plus why it said nothing."""

    engine: Engine
    installed: bool
    plan: str | None
    windows: tuple[UsageWindow, ...]
    #: The file the reading came from, so a surprising number can be checked.
    source: str | None
    #: When the engine took the reading, not when this process read the file.
    observed_at: datetime | None
    notes: tuple[str, ...] = ()
    #: What makes this engine write a fresh reading, for when the one found is old.
    refresh_hint: str | None = None

    def age(self, now: datetime) -> timedelta | None:
        return None if self.observed_at is None else now - self.observed_at

    def expired(self, now: datetime) -> bool:
        """True when a window has reset since the reading, so it overstates usage.

        Worth stating outright. A percentage whose window has already turned over is
        not a small error: quota that reads as spent has in fact come back.
        """
        return any(
            window.resets_at is not None and window.resets_at <= now
            for window in self.windows
        )

    def describe(self, now: datetime) -> dict[str, Any]:
        age = self.age(now)
        return {
            "engine": self.engine.value,
            "installed": self.installed,
            "plan": self.plan,
            "windows": [window.describe() for window in self.windows],
            "source": self.source,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "age_seconds": age.total_seconds() if age is not None else None,
            "expired": self.expired(now),
            "refresh_hint": self.refresh_hint,
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class UsageReport:
    collected_at: datetime
    engines: tuple[EngineUsage, ...]

    def describe(self) -> dict[str, Any]:
        return {
            "collected_at": self.collected_at.isoformat(),
            "engines": [usage.describe(self.collected_at) for usage in self.engines],
        }


def collect_usage(
    *,
    now: datetime | None = None,
    codex_home_path: Path | None = None,
    claude_config_path: Path | None = None,
    grok_home_path: Path | None = None,
) -> UsageReport:
    """Read every engine's usage record. Engines are listed even when they say nothing."""
    collected_at = now or datetime.now(UTC)
    return UsageReport(
        collected_at=collected_at,
        engines=(
            read_codex_usage(codex_home_path),
            read_claude_usage(claude_config_path),
            read_grok_usage(grok_home_path),
            read_antigravity_usage(),
        ),
    )


def read_codex_usage(home: Path | None = None) -> EngineUsage:
    """Take the newest `rate_limits` snapshot codex left in a session rollout."""
    installed = which(CODEX_BINARY) is not None
    try:
        root = codex_home(home)
        rollouts = sorted(
            root.glob("sessions/*/*/*/rollout-*.jsonl"),
            key=lambda path: _mtime(path),
            reverse=True,
        )[:CODEX_ROLLOUT_SCAN]
    except (OSError, RuntimeError, ValueError):
        rollouts = []

    for path in rollouts:
        found = _codex_reading(path)
        if found is None:
            continue
        observed_at, rate_limits = found
        return EngineUsage(
            engine=Engine.CODEX,
            installed=installed,
            plan=_string(rate_limits.get("plan_type")),
            windows=_codex_windows(rate_limits),
            source=str(path),
            observed_at=observed_at,
            notes=(),
            refresh_hint=CODEX_REFRESH_HINT,
        )

    if not rollouts:
        note = f"no codex rollouts under {codex_home(home) / 'sessions'}"
    else:
        note = (
            f"no rate_limits record in the last {TAIL_BYTES // 1024} KB of the "
            f"{len(rollouts)} most recently written rollouts"
        )
    return EngineUsage(
        engine=Engine.CODEX,
        installed=installed,
        plan=None,
        windows=(),
        source=None,
        observed_at=None,
        notes=(note,),
        refresh_hint=CODEX_REFRESH_HINT,
    )


def read_claude_usage(config_path: Path | None = None) -> EngineUsage:
    """Read the `/usage` reading Claude Code cached, and say how old it is.

    The cache is only refreshed while Claude Code runs, so an old fetch time is normal
    and is the whole reason `observed_at` exists. Only the plan is taken from the
    account block; the identifiers beside it are none of this tool's business.
    """
    installed = which(CLAUDE_BINARY) is not None
    path = (config_path or Path(CLAUDE_CONFIG)).expanduser()
    config = _load_json_file(path)
    if config is None:
        return EngineUsage(
            engine=Engine.CLAUDE,
            installed=installed,
            plan=None,
            windows=(),
            source=None,
            observed_at=None,
            notes=(f"{path} is missing or unreadable",),
            refresh_hint=CLAUDE_REFRESH_HINT,
        )

    account = config.get("oauthAccount")
    plan = _string(account.get("organizationType")) if isinstance(account, dict) else None

    cached = config.get("cachedUsageUtilization")
    if not isinstance(cached, dict):
        return EngineUsage(
            engine=Engine.CLAUDE,
            installed=installed,
            plan=plan,
            windows=(),
            source=str(path),
            observed_at=None,
            notes=(
                "no cached usage reading; open /usage in Claude Code once to record one",
            ),
            refresh_hint=CLAUDE_REFRESH_HINT,
        )

    utilization = cached.get("utilization")
    windows = _claude_windows(utilization) if isinstance(utilization, dict) else ()
    notes = () if windows else ("the cached usage reading holds no window",)
    return EngineUsage(
        engine=Engine.CLAUDE,
        installed=installed,
        plan=plan,
        windows=windows,
        source=str(path),
        observed_at=_millis_datetime(cached.get("fetchedAtMs")),
        notes=notes,
        refresh_hint=CLAUDE_REFRESH_HINT,
    )


def read_grok_usage(home: Path | None = None) -> EngineUsage:
    """Take the last billing snapshot grok logged; its period end is the reset."""
    installed = which(GROK_BINARY) is not None
    root = (home or Path(GROK_HOME)).expanduser()
    log_path = root / "logs" / "unified.jsonl"

    record = _last_json_line(
        log_path, match=lambda item: item.get("msg") == GROK_BILLING_MESSAGE
    )
    if record is None:
        return EngineUsage(
            engine=Engine.GROK,
            installed=installed,
            plan=None,
            windows=(),
            source=None,
            observed_at=None,
            notes=(
                f"no {GROK_BILLING_MESSAGE!r} entry in the last "
                f"{TAIL_BYTES // 1024} KB of {log_path}",
            ),
            refresh_hint=GROK_REFRESH_HINT,
        )

    context = record.get("ctx")
    context = context if isinstance(context, dict) else {}
    config = context.get("config")
    config = config if isinstance(config, dict) else {}
    windows = _grok_windows(config)
    return EngineUsage(
        engine=Engine.GROK,
        installed=installed,
        plan=_string(context.get("subscriptionTier")),
        windows=windows,
        source=str(log_path),
        observed_at=_iso_datetime(record.get("ts")),
        notes=() if windows else ("the billing entry holds no usage figure",),
        refresh_hint=GROK_REFRESH_HINT,
    )


def read_antigravity_usage() -> EngineUsage:
    """Report the absence itself: agy has no usage surface to read."""
    return EngineUsage(
        engine=Engine.ANTIGRAVITY,
        installed=which(ANTIGRAVITY_BINARY) is not None,
        plan=None,
        windows=(),
        source=None,
        observed_at=None,
        notes=(
            "agy reports no quota: its JSON result carries tokens only, and it writes "
            "no account reading to disk",
        ),
    )


def _codex_reading(path: Path) -> tuple[datetime | None, dict[str, Any]] | None:
    """Find the last `token_count` event in a rollout's tail that carries limits.

    Scanning backwards stops at the first hit, which is the most recent reading. A
    later `token_count` without `rate_limits` is missing information rather than a
    correction, so it is skipped rather than treated as an empty reading.
    """
    for line in reversed(_tail_lines(path, TAIL_BYTES)):
        record = _load_json_object(line)
        if record is None:
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "token_count":
            continue
        rate_limits = payload.get("rate_limits")
        if not isinstance(rate_limits, dict):
            continue
        return _iso_datetime(record.get("timestamp")) or _mtime_datetime(path), rate_limits
    return None


def _codex_windows(rate_limits: dict[str, Any]) -> tuple[UsageWindow, ...]:
    """Keep codex's own names. It calls them primary and secondary and does not say
    what either measures, so naming them here would be invention."""
    windows: list[UsageWindow] = []
    for label in ("primary", "secondary"):
        limit = rate_limits.get(label)
        if not isinstance(limit, dict):
            continue
        used = _float(limit.get("used_percent"))
        resets_at = _unix_datetime(limit.get("resets_at"))
        if used is None and resets_at is None:
            continue
        windows.append(
            UsageWindow(
                label=label,
                used_pct=used,
                window_minutes=_int(limit.get("window_minutes")),
                resets_at=resets_at,
            )
        )

    credits = rate_limits.get("credits")
    if isinstance(credits, dict):
        balance = _string(credits.get("balance"))
        if credits.get("unlimited") is True:
            windows.append(
                UsageWindow("credits", None, None, None, detail="unlimited")
            )
        elif credits.get("has_credits") is True or (balance and balance != "0"):
            windows.append(
                UsageWindow("credits", None, None, None, detail=f"balance {balance}")
            )
    return tuple(windows)


def _claude_windows(utilization: dict[str, Any]) -> tuple[UsageWindow, ...]:
    windows: list[UsageWindow] = []
    for label, value in utilization.items():
        if label in CLAUDE_NON_WINDOW_KEYS or not isinstance(value, dict):
            continue
        used = _float(value.get("utilization"))
        if used is None:
            continue
        windows.append(
            UsageWindow(
                label=label,
                used_pct=used,
                window_minutes=_claude_window_minutes(label),
                resets_at=_iso_datetime(value.get("resets_at")),
                detail=_claude_dollars(value),
            )
        )

    extra = utilization.get("extra_usage")
    if isinstance(extra, dict):
        window = _claude_extra_usage(extra)
        if window is not None:
            windows.append(window)
    return tuple(windows)


def _claude_window_minutes(label: str) -> int | None:
    if label.startswith(CLAUDE_SEVEN_DAY_PREFIX):
        return SEVEN_DAY_MINUTES
    return CLAUDE_WINDOW_MINUTES.get(label)


def _claude_dollars(value: dict[str, Any]) -> str | None:
    used = _float(value.get("used_dollars"))
    limit = _float(value.get("limit_dollars"))
    if used is None and limit is None:
        return None
    return f"${used or 0:.2f} of ${limit:.2f}" if limit is not None else f"${used:.2f}"


def _claude_extra_usage(extra: dict[str, Any]) -> UsageWindow | None:
    """Pay-as-you-go credits, which are spent rather than reset, so they carry no
    reset instant. Whether they are enabled decides if the balance can be used at all,
    which matters more than the percentage."""
    used = _float(extra.get("utilization"))
    if used is None:
        return None
    details: list[str] = []
    balance = _credit_amount(extra.get("used_credits"), extra.get("monthly_limit"))
    if balance is not None:
        details.append(balance)
    if extra.get("is_enabled") is False:
        reason = _string(extra.get("disabled_reason"))
        details.append(f"disabled ({reason})" if reason else "disabled")
    return UsageWindow(
        label="extra_usage",
        used_pct=used,
        window_minutes=None,
        resets_at=None,
        detail=", ".join(details) or None,
    )


def _credit_amount(used: object, limit: object) -> str | None:
    used_value = _float(used)
    limit_value = _float(limit)
    if used_value is None or limit_value is None:
        return None
    # The API reports minor units; the currency and its exponent travel beside them,
    # so they are printed as the counts they are rather than converted to a currency.
    return f"{used_value:.0f}/{limit_value:.0f} credits"


def _grok_windows(config: dict[str, Any]) -> tuple[UsageWindow, ...]:
    used = _float(config.get("creditUsagePercent"))
    period = config.get("currentPeriod")
    period = period if isinstance(period, dict) else {}
    start = _iso_datetime(period.get("start") or config.get("billingPeriodStart"))
    end = _iso_datetime(period.get("end") or config.get("billingPeriodEnd"))
    if used is None and end is None:
        return ()

    # The window is the reported period, not an assumption about grok's plan: the two
    # ends of it are logged, so its length is a subtraction rather than a guess.
    minutes = int((end - start).total_seconds() // 60) if start and end else None
    details: list[str] = []
    on_demand_used = _grok_value(config.get("onDemandUsed"))
    on_demand_cap = _grok_value(config.get("onDemandCap"))
    if on_demand_cap:
        details.append(f"on-demand {on_demand_used or 0:.0f}/{on_demand_cap:.0f}")
    prepaid = _grok_value(config.get("prepaidBalance"))
    if prepaid:
        details.append(f"prepaid {prepaid:.0f}")

    return (
        UsageWindow(
            label="credits",
            used_pct=used,
            window_minutes=minutes,
            resets_at=end,
            detail=", ".join(details) or None,
        ),
    )


def _grok_value(value: object) -> float | None:
    """grok wraps its money-ish counters as `{"val": 0}`."""
    if isinstance(value, dict):
        return _float(value.get("val"))
    return _float(value)


def _tail_lines(path: Path, tail_bytes: int) -> list[str]:
    """Return the complete lines at the end of a file, without reading all of it."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            start = max(0, handle.tell() - tail_bytes)
            handle.seek(start)
            chunk = handle.read()
    except OSError:
        return []
    lines = chunk.decode("utf-8", errors="replace").splitlines()
    if start > 0 and lines:
        # The window almost certainly opened in the middle of a record.
        del lines[0]
    return lines


def _last_json_line(
    path: Path, match: Callable[[dict[str, Any]], bool] | None = None
) -> dict[str, Any] | None:
    for line in reversed(_tail_lines(path, TAIL_BYTES)):
        record = _load_json_object(line)
        if record is None:
            continue
        if match is None or match(record):
            return record
    return None


def _load_json_file(path: Path) -> dict[str, Any] | None:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _load_json_object(line: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(line)
    except (ValueError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _mtime_datetime(path: Path) -> datetime | None:
    stamp = _mtime(path)
    return datetime.fromtimestamp(stamp, tz=UTC) if stamp else None


def _string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _float(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _iso_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    aware = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return aware.astimezone(UTC)


def _unix_datetime(value: object) -> datetime | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OSError, OverflowError, ValueError):
        return None


def _millis_datetime(value: object) -> datetime | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return _unix_datetime(value / 1000)

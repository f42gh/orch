"""Post-run inspection of what an engine produced.

The v0 guardrail ran as a Claude SDK `can_use_tool` hook, which none of the CLI engines
expose. Prevention now comes from the OS sandbox, the engine deny rules and the
worktree; this module is the layer that runs afterwards and says what got through.

It never blocks anything — it annotates the result so a human looks before merging.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path

from orch.config import Config
from orch.logging_utils import task_log_dir


#: Added lines that look like a credential landed in the diff.
SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key id"),
    (re.compile(r"\bghp_[A-Za-z0-9]{36}\b"), "GitHub personal access token"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), "OpenAI-style API key"),
    (re.compile(r"\bxai-[A-Za-z0-9_-]{20,}\b"), "xAI API key"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"), "Anthropic API key"),
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"), "private key block"),
    (
        re.compile(r"""(?i)\b(?:api[_-]?key|secret|password|passwd|token)\b\s*[:=]\s*["']?[A-Za-z0-9/+_-]{16,}"""),
        "hardcoded credential assignment",
    ),
)

#: Commands that were supposed to be impossible. Seeing one means a layer above failed.
ESCAPE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bgit\s+push\b"), "git push was attempted"),
    (re.compile(r"\bgh\s+pr\s+(?:create|merge)\b"), "a pull request operation was attempted"),
    (re.compile(r"(^|\s)sudo\s"), "sudo was attempted"),
    (re.compile(r"\bkubectl\s+apply\b|\bterraform\s+apply\b"), "a deploy command was attempted"),
)

#: Below this length a random-looking string is too short to judge.
MIN_ENTROPY_LENGTH = 32
ENTROPY_THRESHOLD = 4.2
MAX_FINDINGS = 20


def shannon_entropy(value: str) -> float:
    if not value:
        return 0.0
    counts = Counter(value)
    length = len(value)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def scan_diff(diff_text: str) -> list[str]:
    """Look for credentials among the lines the agent *added*."""
    findings: list[str] = []
    seen: set[str] = set()

    for line in diff_text.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        added = line[1:]
        for pattern, label in SECRET_PATTERNS:
            if pattern.search(added):
                _record(findings, seen, f"possible {label} added in the diff")

        for token in re.findall(r"[A-Za-z0-9/+_=-]{%d,}" % MIN_ENTROPY_LENGTH, added):
            if shannon_entropy(token) >= ENTROPY_THRESHOLD:
                _record(findings, seen, "high-entropy string added in the diff")
                break

    return findings[:MAX_FINDINGS]


def scan_logs(log_text: str) -> list[str]:
    findings: list[str] = []
    seen: set[str] = set()
    for pattern, label in ESCAPE_PATTERNS:
        if pattern.search(log_text):
            _record(findings, seen, label)
    return findings


def scan_task_artifacts(config: Config, task_id: str) -> list[str]:
    """Scan a finished task's diff and logs. Returns warnings for `result.json`."""
    log_dir = task_log_dir(config, task_id)
    findings: list[str] = []

    diff_path = log_dir / "diff.patch"
    if diff_path.exists():
        findings.extend(scan_diff(_read(diff_path)))

    for name in ("stdout.log", "stderr.log"):
        path = log_dir / name
        if path.exists():
            findings.extend(f"{finding} (see {name})" for finding in scan_logs(_read(path)))

    return findings


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _record(findings: list[str], seen: set[str], message: str) -> None:
    if message not in seen:
        seen.add(message)
        findings.append(message)

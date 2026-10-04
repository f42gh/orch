"""Google Antigravity CLI adapter (`agy -p`).

The weakest of the four engines, and the measurements say so — see
docs/engine-capabilities.md:

- agy 1.0.12 emits plain text only. `--output-format` is absent from the binary even
  though the published docs describe it, so `probe()` looks at `--help` and promotes
  this adapter to a JSON path automatically once a future build grows the flag.
- agy ignores the process working directory. Started inside a repository with no flags
  it inspected its own scratch directory instead, so `--add-dir` is mandatory.
- The upstream non-TTY stdout bug does not reproduce on 1.0.12: a plain pipe returned
  the full response. No PTY wrapper is used. Empty output is still treated as failure
  because that is exactly how the bug presents if it comes back.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from orch.engines.base import (
    Capabilities,
    EngineResult,
    RunSpec,
    read_help,
    read_usage_int,
    read_version,
    which,
)
from orch.models import Engine, Task, TokenUsage
from orch.router import EnginePolicy


BINARY = "agy"

#: CSI / OSC escape sequences, stripped in case a future build decides to colour output.
ANSI_PATTERN = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


class AntigravityAdapter:
    engine = Engine.ANTIGRAVITY

    def __init__(self, capabilities: Capabilities | None = None) -> None:
        # `build` needs to know whether this build speaks JSON, so the probe result is
        # cached rather than re-spawning `agy --help` for every task. Tests inject it.
        self._capabilities = capabilities
        self._probed = capabilities is not None

    def probe(self) -> Capabilities | None:
        if not self._probed:
            self._capabilities = self._probe_uncached()
            self._probed = True
        return self._capabilities

    def _probe_uncached(self) -> Capabilities | None:
        path = which(BINARY)
        if path is None:
            return None
        help_text = read_help([path, "--help"])
        structured = "--output-format" in help_text
        notes = [
            "ignores the process working directory; --add-dir is required",
            "reports no session id, usage or cost",
        ]
        if not structured:
            notes.append("this build has no --output-format; output is parsed as plain text")
        return Capabilities(
            engine=self.engine,
            path=path,
            version=read_version([path, "--version"]),
            structured_output=structured,
            reports_cost=False,
            notes=tuple(notes),
        )

    def build(
        self,
        task: Task,
        policy: EnginePolicy,
        workspace: Path,
        prompt: str,
        artifacts: Path,
    ) -> RunSpec:
        argv = [BINARY, "--print", prompt, "--add-dir", str(workspace)]

        # agy has no read-only mode and no per-level sandbox, so `--sandbox` is the only
        # lever available. What actually contains a run is the worktree plus the
        # post-run diff check; the prompt carries the read-only instruction.
        if policy.allow_dangerous:
            argv.append("--dangerously-skip-permissions")
        else:
            argv.append("--sandbox")

        capabilities = self.probe()
        if capabilities and capabilities.structured_output:
            argv += ["--output-format", "json"]

        return RunSpec(argv=argv)

    def parse(self, stdout: str, stderr: str, exit_code: int, spec: RunSpec) -> EngineResult:
        warnings: list[str] = []
        text = ANSI_PATTERN.sub("", stdout).strip()

        payload = _maybe_json(text)
        if payload is not None:
            status = str(payload.get("status") or "")
            if status and status != "SUCCESS":
                # agy exits 0 on CANCELED/INTERRUPTED too, so `status` is the only
                # signal that the run did not really finish.
                warnings.append(f"agy reported status {status}")
            usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else None
            tokens = _normalise_tokens(usage, warnings)
            return EngineResult(
                text=str(payload.get("response") or payload.get("text") or text),
                exit_code=exit_code,
                session_id=payload.get("conversation_id"),
                usage=usage,
                warnings=tuple(warnings),
                structured=payload,
                tokens=tokens,
            )

        if not text:
            # This is how upstream issue #76 presents: exit 0 and nothing on stdout.
            warnings.append(
                "agy produced no output; if this recurs, the non-TTY stdout bug is back "
                "and ui_verify should be routed away from antigravity"
            )
            text = stderr.strip()

        if "workspace" in text.lower() and "scratch" in text.lower():
            warnings.append("agy may have worked outside the workspace; check --add-dir")

        return EngineResult(text=text, exit_code=exit_code, warnings=tuple(warnings))


def _normalise_tokens(
    usage: dict[str, Any] | None, warnings: list[str]
) -> TokenUsage | None:
    if usage is None:
        return None

    input_tokens = read_usage_int(usage, "input_tokens")
    cache_read_tokens = read_usage_int(usage, "cache_read_tokens")
    # The fixture proves agy includes cache reads in input: 28,469 input + 204 output
    # equals its reported total of 28,673, while 27,101 cache-read tokens are a subset.
    if cache_read_tokens > input_tokens:
        warnings.append(
            "agy cached-input convention did not hold; input tokens clamped to zero"
        )

    return TokenUsage(
        input_tokens=max(0, input_tokens - cache_read_tokens),
        cache_read_tokens=cache_read_tokens,
        output_tokens=read_usage_int(usage, "output_tokens"),
        reasoning_tokens=read_usage_int(usage, "thinking_tokens"),
    )


def _maybe_json(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if not stripped.startswith("{"):
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None

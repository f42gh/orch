"""Which engines this machine can actually run.

Probing is cached per process: the daemon and the MCP server both ask on every
dispatch, and each probe spawns two subprocesses.
"""

from __future__ import annotations

from agent_orchestrator.engines.antigravity import AntigravityAdapter
from agent_orchestrator.engines.base import Capabilities, EngineAdapter
from agent_orchestrator.engines.claude import ClaudeAdapter
from agent_orchestrator.engines.codex import CodexAdapter
from agent_orchestrator.engines.grok import GrokAdapter
from agent_orchestrator.models import Engine


def build_adapters() -> dict[Engine, EngineAdapter]:
    adapters: list[EngineAdapter] = [
        CodexAdapter(),
        GrokAdapter(),
        AntigravityAdapter(),
        ClaudeAdapter(),
    ]
    return {adapter.engine: adapter for adapter in adapters}


_adapters: dict[Engine, EngineAdapter] = build_adapters()
_capabilities: dict[Engine, Capabilities] | None = None


def get_adapter(engine: Engine) -> EngineAdapter:
    try:
        return _adapters[engine]
    except KeyError:
        raise LookupError(f"no adapter for engine {engine!r}") from None


def probe_all(refresh: bool = False) -> dict[Engine, Capabilities]:
    global _capabilities
    if _capabilities is None or refresh:
        if refresh:
            _adapters.update(build_adapters())
        found: dict[Engine, Capabilities] = {}
        for engine, adapter in _adapters.items():
            capabilities = adapter.probe()
            if capabilities is not None:
                found[engine] = capabilities
        _capabilities = found
    return _capabilities


def available_engines(refresh: bool = False) -> set[Engine]:
    return set(probe_all(refresh))

# Repository Guidelines

## What this is

A local orchestrator that hands coding work to whichever agent CLI suits it — `codex`,
`grok`, `agy` (Antigravity) or `claude` — runs each task in its own git worktree, and
gives the results back for a human to review. Claude Code drives it through the MCP
server in `mcp_server.py`.

## Project structure

- `src/agent_orchestrator/router.py` — picks the engine from the task kind. The heart of
  the thing; change routing here, not in prompts.
- `src/agent_orchestrator/workflows.py` — persists immutable Run/Batch route snapshots,
  resolves their engines and records task membership. Runs accept later tasks until
  closed; Batches never do.
- `src/agent_orchestrator/engines/` — one adapter per CLI. Adapters are pure: they build
  an argv and parse bytes. No spawning, no database, no filesystem beyond declared files.
- `src/agent_orchestrator/command_installer.py` and `templates/orch.md` — safely install
  the Claude Code `/orch` workflow command. Never overwrite a different user command
  without a backup and explicit force.
- `src/agent_orchestrator/worker.py` — everything process-shaped: spawning, live log
  capture, timeouts, writing results back.
- `src/agent_orchestrator/mcp_server.py` — the MCP control plane. Must never print to
  stdout; that is the transport.
- `src/agent_orchestrator/db.py` — SQLite, the single source of truth shared by the MCP
  server, the daemon, the CLI and the React UI.
- `src/agent_orchestrator/stats.py` — pure aggregation over tasks: cost, tokens, engine
  time, diff size. No database, no git, no filesystem; filtering belongs in `list_tasks`.
- `docs/engine-capabilities.md` — what each CLI *actually* does, measured. Read it before
  touching an adapter.
- `ui/` — Deno + React front end for the local API.

## Commands

```bash
uv sync                       # install
uv run pytest                 # tests
uv run agentctl engines       # what this machine has, and the routing table
uv run agentctl install-claude-command
uv run agentctl run create --repo <path> --route implement=codex
uv run agentctl run dispatch run-0001 --task "..." --kind implement
uv run agentctl batch dispatch --repo <path> --route implement=codex --tasks-file tasks.json
uv run agentctl add --repo <path> --task "..." --kind implement
uv run agentctl stats --group-by engine        # cost, tokens, engine time, diff size
uv run agentctl usage                          # each engine's account quota and its reset
uv run agentctl dispatch --repo <path> --task "..." --json   # add + start; the JSON is CAGE's contract
uv run agentd run-task <id>   # run one task
uv run agentd run --max-concurrency 2
uv run agentapi run           # local HTTP API on 127.0.0.1:8765
uv run agentmcp               # MCP server over stdio
```

Run `uv run pytest` before submitting changes.

## Working on the adapters

Vendor documentation describes flags that installed binaries do not always have — `agy`
1.0.12 shipped without the `--output-format` its docs advertised, and grew it in 1.1.11.
So:

- Detect capabilities with `probe()` against `--help`, never from a version number or
  from documentation.
- Add a fixture under `tests/fixtures/` captured from a real run before writing a parser,
  and test the parser against that fixture. Do not write tests that call a live API.
- Record anything surprising in `docs/engine-capabilities.md` with what you observed.

## Safety invariants

These are load-bearing. Changing one is a deliberate decision, not a refactor.

- Engines write only inside their own worktree. The original checkout is never touched
  except by `orch_adopt(strategy="apply")`.
- `--dangerously-*` flags and unsandboxed access require `allow_dangerous` in
  `routing.toml`. `EnginePolicy.effective_access` enforces this even for a hand-built
  policy; `tests/test_engines.py` asserts it per adapter.
- Nothing commits or pushes. Engines are told not to, deny rules block it, and
  `secrets_scan.py` looks for attempts afterwards.
- Every engine is spawned with stdin closed and an explicit working directory.
- Workflow routes choose the engine only. They must never change the access, prompt,
  schema, risk policy or dangerous-access opt-in derived for the task.
- A manual workflow fallback list is strict and exhaustive. Automatic fallback is used
  only when the list is omitted; never silently escape a supplied list.

## Style

Two-space indentation is not used here — this is Python, four spaces, double quotes,
`from __future__ import annotations` at the top of every module. Type-annotate public
functions. Prefer a dataclass over a dict for anything that crosses a module boundary.

Comments explain why, not what. The valuable ones in this codebase all record a
measurement — a flag that hangs, an output shape that differs from the docs — so keep
that bar.

## Commits

Conventional Commit subjects (`feat:`, `fix:`, `docs:`). Explain intent and what was
verified. Never commit `~/agent-runtime/` contents, credentials, or anything from a
worktree.

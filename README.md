# orch

English | [日本語](README.ja.md)

A local agent orchestrator that hands coding work to whichever agent CLI suits it, runs
each task in its own git worktree, and gives the result back for a human to review.

Claude Code is the orchestrator. `codex`, `grok`, `agy` (Antigravity) and `claude` are
workers, reached over an MCP server. A task's *kind* provides the automatic engine
default:

| kind | engine | access |
|---|---|---|
| `implement` / `refactor` / `test` | codex | writes, inside the worktree |
| `review` / `investigate` | grok | read-only |
| `ui_verify` | antigravity | writes, inside the worktree |
| (fallback for all of the above) | claude | |

Anything not installed falls back automatically, so a missing engine degrades instead of
failing. `docs/engine-capabilities.md` records what each CLI actually does, measured
rather than taken from its documentation.

For a new workflow you can replace those defaults with an explicit route table. Choose
the table once, before anything starts:

| workflow | use it when | lifetime |
|---|---|---|
| **Run** | work may be added later, or you choose later work after reviewing an earlier result | saved; accepts additions until you close it |
| **Batch** | the complete set of independent tasks is known now | submitted once; no later additions |

Routes choose engines only. The task kind and risk still determine the prompt, output
schema and access level, so assigning a different engine does not bypass the safety
policy.

A Run preserves routing and task history; it does not merge one task's uncommitted
worktree into the next. If later work needs those code changes, review them first, then
have a human adopt and commit them and pass that commit as `base_ref`.

## Setup

```bash
git clone https://github.com/f42gh/orch
cd orch
uv sync
uv run agentctl engines   # what this machine has, and the routing table
```

Each CLI needs to be installed and authenticated on its own. Nothing here stores
credentials.

### Use it from Claude Code

```bash
claude mcp add orch -s user -- uv run --directory ~/dev/orch agentmcp
```

Then `/orch <what you want done>`, or call the tools directly. See
`docs/CLAUDE-PLAYBOOK.md` for the patterns worth knowing — fan-out, and having one engine
implement while a different one reviews.

Install the repository's `/orch` command template to make that workflow explicit in
Claude Code:

```bash
uv run agentctl install-claude-command
```

The default target is `~/.claude/commands/orch.md`. An identical file is left alone. A
different existing command is never overwritten unless you pass `--force`; forced
replacement first writes a uniquely named backup. Use `--target PATH` to install
somewhere else.

## Use it from the terminal

Create a persistent Run when you expect to add work later:

```bash
uv run agentctl run create --repo ~/dev/my-project \
  --route implement=codex \
  --fallback implement=claude,grok
uv run agentctl run dispatch run-0001 --task "add the parser" --kind implement
uv run agentctl run show run-0001
uv run agentctl run close run-0001
```

Submit a one-shot Batch when every independent task is already known. The tasks file is
a JSON array of task specifications; `--tasks-file -` reads it from stdin.

```bash
uv run agentctl batch dispatch --repo ~/dev/my-project \
  --route implement=codex \
  --fallback implement=claude,grok \
  --tasks-file tasks.json
```

Repeat `--route KIND=ENGINE` for the kinds you want to assign. Unmentioned kinds inherit
the current automatic primary when the workflow is created. Inputs accept `agy` as an
alias; stored data and output always use the canonical name `antigravity`.

Fallback behavior is deliberately different depending on whether you provide a list:

- Omit `--fallback` for a kind to snapshot the current automatic route order. If that
  whole chain is unavailable later, automatic mode still chooses another installed
  engine deterministically.
- Provide a non-empty ordered list to make it strict and exhaustive. If neither the
  primary nor an engine in that list is installed, dispatch fails instead of silently
  choosing another engine. An empty list is invalid.

### Legacy single-task commands

The original single-task commands remain supported:

```bash
uv run agentctl add --repo ~/dev/my-project --task "READMEのセットアップ手順を最新化して"
uv run agentctl add --repo ~/dev/my-project --task "calc.py をレビューして" --kind review --risk read_only
uv run agentctl dispatch --repo ~/dev/my-project --task "..." --json  # add + start in one shot; what CAGE calls

uv run agentd run-task task-0001      # run one
uv run agentd run --max-concurrency 2 # drain the queue

uv run agentctl list
uv run agentctl show task-0001
```

Options for `add` and `dispatch`: `--kind`, `--engine`, `--risk`, `--priority`, `--parent`, `--base-ref`.

## Runtime layout

```text
~/agent-runtime/
  tasks.db                     shared by the MCP server, daemon, API and UI
  workspaces/<task_id>/repo    the git worktree, on branch agent/<engine>/<task_id>
  logs/<task_id>/
    agent.log stdout.log stderr.log
    diff.patch result.json
```

Override with `--runtime-root` or `AGENT_ORCHESTRATOR_RUNTIME_ROOT`.

A finished task lands in `needs_review`, never `succeeded` — nothing marks its own work
as done. `result.json` carries the summary, structured findings for reviews, changed
files, diffstat, token usage, cost where the engine reports it, and warnings.

## Legacy HTTP API and UI

The HTTP API and React UI remain available for the original single-task workflow. They
do not create or manage Runs and Batches; use the MCP tools or `agentctl` for new
workflow orchestration.

```bash
uv run agentapi run            # 127.0.0.1:8765
cd ui && deno task dev
```

## Risk and access

`--risk` sets how much the task may do, independently of its kind:

- `read_only` — reads and searches; writes nothing.
- `normal` — writes, but only inside the worktree.
- `high` — planning only. The prompt forbids implementation and asks for analysis.

Containment is four layers, because the CLI engines expose no in-process hook to block a
tool call the way the Claude SDK does:

1. the OS sandbox each engine offers (kernel-enforced via Seatbelt on macOS),
2. engine deny rules for `git push`, `sudo` and similar,
3. the git worktree, so the original checkout is never touched,
4. a post-run scan of the diff and logs that annotates the result rather than blocking.

Unsandboxed access requires an explicit opt-in in `routing.toml`; nothing reaches for a
`--dangerously-*` flag on its own.

## Tuning the routing

Optional `~/.config/agent-orchestrator/routing.toml`. Only the keys present are
overridden. This table supplies automatic defaults. A Run or Batch snapshots its
materialized routes when it is created, so later edits do not change that workflow:

```toml
[kinds.implement]
engine = "grok"
fallbacks = ["codex", "claude"]

[risk.normal]
max_turns = 60
timeout_s = 2400

[engines.codex]
allow_dangerous = true   # removes the sandbox for codex; think before setting this
```

## Limits

- Generated changes always need a human to read them. `orch_adopt` returns a patch by
  default and writes nothing.
- Nothing commits, pushes or deploys.
- `high` risk produces a plan, not an implementation.
- Concurrent tasks are isolated while running, but two patches that edit the same
  function still conflict at adoption time.
- Only grok and claude report what a run cost; codex and antigravity do not, so the
  totals are partial by construction.

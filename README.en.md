# orch

English | [日本語](README.md)

A local agent orchestrator that hands coding work to whichever agent CLI suits it, runs
each task in its own git worktree, and gives the result back for a human to review.

Claude Code is the orchestrator. `codex`, `grok`, `agy` (Antigravity) and `claude` are
workers, reached over an MCP server. You can choose the engine assignment for every
Run, Batch, or single task. When you leave a kind on automatic routing, these defaults
apply:

| kind | automatic route | access |
|---|---|---|
| `implement` | codex → claude → grok | writes, inside the worktree |
| `refactor` | codex → grok → claude | writes, inside the worktree |
| `test` | codex → claude → grok | writes, inside the worktree |
| `review` | grok → codex → claude | read-only |
| `investigate` | grok → claude → codex | read-only |
| `ui_verify` | antigravity → claude | writes, inside the worktree |

In automatic mode, an exhausted chain falls back deterministically to another installed
engine, so work can proceed as long as at least one worker CLI is available. The
[engine capability notes](docs/engine-capabilities.md) record what each CLI actually
does, measured rather than taken from its documentation.

These are defaults, not fixed assignments. Use the interactive `orch start`, pass an
exact `--engine` for one task, or define a route table for a Run or Batch.

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

Requirements: Python 3.12+, `uv`, Git, and at least one installed and authenticated
worker CLI. Claude Code is required only for the MCP-driven workflow.

```bash
git clone https://github.com/f42gh/orch
cd orch
uv sync                           # creates .venv/bin/orch
uv tool install -e ".[mcp,api]"   # puts it on PATH; -e keeps it live against this checkout
orch engines                      # what this machine has, and the routing table
```

Skip `uv tool install` if you would rather write `uv run orch engines` each time, or put
`.venv/bin` on your PATH yourself.

Everything is under the one `orch` command:

| | |
|---|---|
| `orch add` / `dispatch` / `list` / `show` | queue and inspect tasks |
| `orch run` / `batch` / `start` | Run and Batch workflows |
| `orch stats` / `usage` / `engines` | aggregates and what this machine has |
| `orch daemon run` | the worker that drains the queue |
| `orch api` | local HTTP API for the UI |
| `orch mcp` | MCP server over stdio |

The older `agentctl`, `agentd`, `agentapi` and `agentmcp` names remain as aliases, so
existing registrations and scripts keep working.

Each CLI needs to be installed and authenticated on its own. Nothing here stores
credentials.

### Use it from Claude Code

Register the MCP server and install the repository's `/orch` command template:

```bash
claude mcp add orch -s user -- orch mcp
orch install-claude-command
```

Pass `--locale ja` to install the Japanese command template instead. It localizes the
autocomplete description, argument hint, confirmation prompts, and final report.

Start a new Claude Code session, then run `/orch <what you want done>`. Before dispatching
anything, `/orch` shows the available engines, proposes a Run or Batch and its
assignments, and waits for your confirmation. You can also call the MCP tools directly.
Replace `/absolute/path/to/orch` with this checkout's absolute path. The installer only
places the command file; it does not register the MCP server, and the command expects the
server name `orch` used above.

The direct workflow tools are `orch_run_create`, `orch_run_dispatch` and
`orch_run_close` for Runs, `orch_batch_dispatch` for a Batch, and
`orch_workflow_list`/`orch_workflow_show` for resuming or inspecting saved workflows.
`orch_status`, `orch_wait`, `orch_result` and `orch_diff` follow one task through to its
patch, `orch_adopt` takes the work out of the worktree, and `orch_stats` returns the
aggregation described below — cost, tokens, durations and quota. `orch_usage` reports what
is left on each engine's account and when it resets.
See the [Claude Code playbook](docs/CLAUDE-PLAYBOOK.md) for patterns such as fan-out and
having one engine implement while a different one reviews.

The default target is `~/.claude/commands/orch.md`. An identical file is left alone. A
different existing command is never overwritten unless you pass `--force`; forced
replacement first writes a uniquely named backup. Use `--target PATH` to install
somewhere else. The supported template locales are `en` (default) and `ja`.

## Use it from the terminal

For an interactive start where you choose the assignments for this use, run:

```bash
orch start
```

The wizard detects installed engines, asks whether this is a persistent Run or a
one-shot Batch, shows the automatic routes, and lets you override each task kind before
anything starts. It requires a TTY. The Run path creates an empty Run and prints its
`workflow_id`; add the first task with `run dispatch` as shown below. For scripts and
repeatable commands, use the explicit forms below.

Create a persistent Run when you expect to add work later:

```bash
orch run create --repo ~/dev/my-project \
  --route implement=grok \
  --fallback implement=codex,claude

# Copy workflow_id from the create output, for example run-0007.
orch run dispatch run-0007 --task "add the parser" --kind implement
orch run show run-0007
orch run list
orch run close run-0007
```

Submit a one-shot Batch when every independent task is already known. The tasks file is
a JSON array of task specifications; `--tasks-file -` reads it from stdin.

```json
[
  {
    "task": "add the parser",
    "kind": "implement",
    "risk": "normal",
    "priority": "high",
    "base_ref": "main"
  },
  {
    "task": "review the authentication flow",
    "kind": "review"
  }
]
```

Allowed task keys are `task`, `kind`, `risk`, `priority`, `parent_id` and `base_ref`.
Choose engines at workflow level with `--route`; an `engine` key is not accepted in a
Batch task object.

```bash
orch batch dispatch --repo ~/dev/my-project \
  --route implement=grok \
  --fallback implement=codex,claude \
  --tasks-file tasks.json
orch batch list
```

Repeat `--route KIND=ENGINE` for the kinds you want to assign. Unmentioned kinds inherit
the current automatic primary when the workflow is created. Inputs accept `agy` as an
alias; stored data and output always use the canonical name `antigravity`.

Fallback behavior is deliberately different depending on whether you provide a list:

- Omit `--fallback` for a kind to snapshot the current automatic route order. If that
  whole chain is unavailable later, automatic mode still chooses another installed
  engine deterministically. With a custom primary, the persisted explicit candidates
  are that primary followed by the current configured primary and fallbacks with
  duplicates removed; the persisted automatic mode then permits deterministic
  any-installed rescue.
- Provide a non-empty ordered list to make it strict and exhaustive. If neither the
  primary nor an engine in that list is installed, dispatch fails instead of silently
  choosing another engine. The list requires a matching `--route`, must not contain the
  primary or duplicates, and cannot be empty.

`run show` and `batch show` display the saved routes, fallback mode and actual engine
selected for each task.

### Legacy single-task commands

The original single-task commands remain supported:

```bash
orch add --repo ~/dev/my-project --task "READMEのセットアップ手順を最新化して"
orch add --repo ~/dev/my-project --task "calc.py をレビューして" --kind review --risk read_only
orch dispatch --repo ~/dev/my-project --task "review the parser" --kind review --engine codex
orch dispatch --repo ~/dev/my-project --task "..." --json  # add + start in one shot

orch daemon run-task task-0001      # run one
orch daemon run --max-concurrency 2 # drain the queue

orch list
orch show task-0001
orch list --json           # the same shape orch_list returns
orch show task-0001 --json
```

Options for `add` and `dispatch`: `--kind`, `--engine`, `--risk`, `--priority`, `--parent`, `--base-ref`.
An explicit `--engine` wins over the kind's automatic route for that task; immediate
dispatch fails early if that engine is not installed. Both commands check that
`--repo` exists, and expand `~`.

Every command that returns a result takes `--json`. The human tables are printed
with aligned columns, so read them with `--json` when a machine is reading.
`--runtime-root` may go before or after the subcommand; the later one wins if you
write both. `orch --help` ends with worked examples.

## What a run cost

Every finished task records what it spent: cost where the engine reports it, normalised
token counts, the engine's wall time, and how much code moved. `orch stats` adds them
up, and `orch_stats` returns the same figures to Claude.

```bash
orch stats                          # everything this machine has ever run
orch stats --workflow run-0001      # one Run or Batch
orch stats --group-by engine        # per-engine breakdown
orch stats --since 2026-08-01 --repo ~/dev/my-project
orch stats --json                   # one JSON object
```

Filters: `--repo`, `--workflow`, `--engine`, `--kind`, `--since`, `--until`. `--group-by`
takes `engine`, `model`, `kind`, `status` or `repo`. `run show` and `batch show` carry
the same totals for their own tasks.

```
tasks: 5
by_status: needs_review=4, failed=1
success_rate: 80.0%
cost_usd: 0.3971 (1/5 terminal tasks reported; no cost from codex)
tokens: 2304070 (2/5 tasks reported; input=167635, output=39667, cache_read=2096768, ...)
engine_s_total: 908.5
engine_s_p50: 285.5
```

Two things the numbers mean, both of which are easy to misread:

- **The cost total is partial by construction.** Only grok and claude report cost, so the
  figure never appears without the fraction of tasks it covers and the engines missing
  from it. `orch engines` shows which is which.
- **`needs_review` is the successful outcome.** Nothing marks its own work as done, so a
  clean run stops there and counts as completed; `succeeded` is only ever set by hand.

`engine_s_*` measures the engine process alone. It excludes worktree creation and queue
waiting, which `orch_status` reports separately as `queue_wait_s`.

Token counts are normalised across the four engines, whose field names all differ and
which disagree about whether their input count already includes cache reads.
`input_tokens` here always means non-cached input. See `docs/engine-capabilities.md` for
the per-engine mapping and the arithmetic it was derived from.

### Models and quota

`--group-by model` splits the same figures by the model that actually ran, which is what
you want when the same kind of task went out at different reasoning efforts. grok and
claude name their model on stdout; codex names it only in its own session rollout, which
orch reads back through the session id it already stores. antigravity names it nowhere
reachable, so those tasks show `-`.

For an engine on a subscription rather than API billing, dollars are the wrong unit — codex
reports its plan and how much of the current window it has consumed, so `stats` shows that
directly instead of estimating a price:

```
quota: codex 4.0% of a 7d window (plan=plus, resets 2026-08-18T00:47Z)
```

This is a **snapshot of the account, not a per-task cost, and it is never summed.**
`used_percent` is account-global and quantised to whole points, so two tasks running at
once cannot be told apart in it and a short task does not move it at all. Per-task
consumption is what the token counts are for.

## What is left on each engine

Where `stats` answers "what did the tasks in this database cost", `orch usage` answers
"how much of each subscription is left" — account-wide, including everything spent outside
orch. `orch_usage` returns the same listing to Claude.

```bash
orch usage          # all four engines
orch usage --json   # one JSON object
```

```
engine       installed  plan        window          used   resets_at          in      observed  detail
codex        yes        plus        primary (7d)    8.0%   2026-08-20T12:38Z  5d23h   53m ago   -
claude       yes        claude_pro  five_hour (5h)  1.0%   2026-08-11T17:40Z  elapsed 3d0h ago  -
claude       yes        claude_pro  seven_day (7d)  57.0%  2026-08-11T23:00Z  elapsed 3d0h ago  -
claude       yes        claude_pro  extra_usage     85.9%  -                  -       3d0h ago  8593/10000 credits, disabled (out_of_credits)
grok         yes        SuperGrok   credits (7d)    3.0%   2026-08-12T21:13Z  elapsed 2d16h ago -
antigravity  yes        -           -               -      -                  -       -         -

source: codex /Users/f42/.codex/sessions/2026/08/12/rollout-…jsonl
stale: claude a window reset after this reading, so real usage is lower than shown — …
note: antigravity agy reports no quota: its JSON result carries tokens only, …
```

No engine offers an API for this. All that exists is **the file each engine writes for
itself**, so `usage` reads those and nothing else: it spawns no process and opens no auth
file — a command for checking quota should not be able to spend it, and has no reason to
read a token. Per-engine sources and the measurements behind them are in
`docs/engine-capabilities.md`.

That makes *when the reading was taken* matter as much as the number, so every row carries
its age and the file it came from:

- **codex** records one every turn, so it is almost always current.
- **claude** caches the `/usage` reading and only refreshes it while Claude Code runs. A
  reading several days old is normal.
- **grok** writes one each time it starts.
- **antigravity** reports nothing, and is listed with that as its reason rather than
  omitted — a missing row cannot be told apart from an engine that is not installed.

A row whose `in` reads `elapsed` had its window reset **after** the reading, so the
percentage beside it has already come back; a `stale:` line then says so and how to
refresh it.

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
do not create or manage Runs and Batches; use the MCP tools or `orch` for new
workflow orchestration.

```bash
uv sync --extra api
orch api                # 127.0.0.1:8765
cd ui && deno task dev
```

Workflow member tasks can appear as ordinary flat tasks, but the API and UI do not
manage workflow IDs, route snapshots, Run closing or Batch sealing. Point every process
at the same runtime root. See the [UI README](ui/README.md) for frontend setup.

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
- Only grok and claude report what a run cost; codex and antigravity do not, so any
  dollar total is partial by construction. codex reports a subscription plan and quota
  instead, which `stats` shows as a separate snapshot rather than converting to a price.
- Quota is account-wide and quantised to whole points, so it is never attributed to a
  single task and never summed. Per-task consumption is read from the token counts.
- antigravity reports no model name anywhere reachable, so those tasks group under `-`.

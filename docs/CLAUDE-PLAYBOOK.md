# Orchestration playbook

How to use the `orch` MCP tools from Claude Code. Claude is the orchestrator; the other
CLIs are workers. Nothing here is automatic — you decide what to delegate.

## The shape of every workflow

```
orch_engines          see what this machine has and how auto routing starts
confirm with user     Run or Batch, primary routes, and fallback mode/order
orch_run_* / batch    persist a Run or submit one complete Batch
orch_wait             join point once tasks have been dispatched
orch_result + diff    read what every engine actually did
orch_adopt            take the reviewed patch, or don't
```

Dispatch returns in milliseconds. A real task takes minutes. Never sit in a poll loop on
`orch_status` when `orch_wait` will block for you. `orch_workflow_list` and
`orch_workflow_show` let you find and inspect saved workflows after the calling session
has ended.

Inspect every dispatch response before waiting. A non-null `spawn_error` means that task
was saved but its detached worker did not start; it remains `queued` for `orch daemon` to
recover. Report it and do not include that ID in `orch_wait` until a daemon has started
it, otherwise the wait can only time out.

Before dispatching, split the request into tasks and make dependencies visible. Then ask
the user to explicitly confirm:

1. **Run or Batch.** A Run accepts later additions and is the right shape for dependent
   stages. A Batch is one submission whose complete task set is already known and
   independent.
2. **Primary routes.** Choose an engine for every kind the plan uses. `agy` is accepted as
   an input alias; stored values and responses use `antigravity`.
3. **Fallbacks.** Omit a kind from `fallbacks` to snapshot the automatic order, or provide
   a non-empty ordered list to make it strict and exhaustive. A strict list never falls
   through to an unlisted installed engine, and an empty list is invalid.

Do not dispatch while any of those choices is implicit.

## Kinds and route safety

When a route is not specified, `kind` is looked up in `routing.toml`; `orch_engines`
shows the current table. If a kind you need is missing, ask the user how they want work
split across the installed engines, then call `orch_routing_set`. With no preference,
route every kind to every installed engine.

| kind | use it when |
|---|---|
| `implement` | writing new behaviour |
| `refactor` | changing structure, not behaviour |
| `test` | adding or fixing tests |
| `review` | you want findings on code that already exists |
| `investigate` | you need to understand something before deciding |
| `ui_verify` | it has to be checked in a browser |

`review` and `investigate` run read-only and cannot modify anything, so they are cheap to
reach for and safe to run against a repository you care about.

An explicit route changes the worker, not the safety policy. Kind and risk still control
the prompt, structured output and write access. Missing kinds are materialized from the
current routing table when a workflow is created, and the resulting route table is
snapshotted so later `routing.toml` edits do not alter that workflow.

## Pattern: a persistent Run

Use a Run when more work may arrive or you will choose later work after reviewing an
earlier result. Routes cannot be edited after creation; close the Run once no more tasks
will be added.

```
run = orch_run_create(
    repo="~/dev/thing",
    routes={"implement": "codex", "review": "grok"},
    fallbacks={"implement": ["claude", "grok"]},
)
impl = orch_run_dispatch(
    run_id=run["workflow_id"], task="add the parser", kind="implement"
)
orch_wait(task_ids=[impl["task_id"]], timeout_s=120)
orch_result(task_id=impl["task_id"]); orch_diff(task_id=impl["task_id"])
orch_run_close(run_id=run["workflow_id"])
```

Run membership and `parent_id` preserve orchestration history; they do not transfer
uncommitted worktree changes. A returned branch points at the task's base commit because
engines never commit. If a later worker needs the earlier code, pause for human review,
adoption and commit, then dispatch it with that commit as `base_ref`.

Write the task text the way you would brief a competent stranger: what to change, what
not to change, and how to tell it worked. The engine sees the repository and your task
text, and nothing of this conversation.

## Pattern: a one-shot Batch

Use a Batch for a complete set of independent tasks. The entire request is validated
before any task is queued; tasks cannot be added to the Batch later.

```
batch = orch_batch_dispatch(
    repo="~/dev/thing",
    routes={"implement": "codex"},
    tasks=[
        {"task": "add the parser", "kind": "implement"},
        {"task": "add the serializer", "kind": "implement"},
        {"task": "document the format", "kind": "implement"},
    ],
)
orch_wait(task_ids=batch["task_ids"], timeout_s=120)
```

Only fan out over pieces that do not touch the same files. Separate worktrees mean they
cannot corrupt each other mid-run, but two patches that both edit one function will
conflict when you adopt the second.

## Legacy pattern: direct single-task dispatch

`orch_dispatch` remains supported for existing callers and one-off single tasks. It uses
the kind route unless `engine` explicitly overrides it.

```
task = orch_dispatch(repo="~/dev/thing", task="…", kind="implement")
orch_wait(task_ids=[task["task_id"]], timeout_s=120)
orch_diff(task_id=task["task_id"])
```

Direct dispatch is also still useful for comparing two engines on exactly the same task:

```
a = orch_dispatch(repo=…, task=T, kind="implement", engine="codex")
b = orch_dispatch(
    repo=…, task=T, kind="implement", engine="grok", parent_id=a["task_id"]
)
orch_wait(task_ids=[a["task_id"], b["task_id"]], timeout_s=120)
orch_diff(task_id=a["task_id"]); orch_diff(task_id=b["task_id"])
```

This costs about twice as much, so keep it for decisions that are hard to reverse. Read
both diffs and pick one — do not try to merge them.

## Reading a result

`orch_result` gives you the engine's summary, `changed_files`, `diffstat`, token usage,
cost where the engine reports it, and `warnings`.

Take `warnings` seriously. They are the layer that runs after the sandbox:

- a credential pattern in the added lines,
- an attempted `git push` or `sudo` in the logs,
- `stopped early: …`, meaning the engine ran out of turns rather than finishing,
- `agy reported status CANCELED`, which exits 0 and would otherwise look like success.

A task that finishes lands in `needs_review`, never `succeeded`. Nothing marks its own
work as done.

## Adopting

`orch_adopt(task_id)` returns the patch and changes nothing. That is the default and it
is usually where you should stop — report what the engine did and let the human decide.

`orch_adopt(task_id, strategy="apply")` writes the patch into the real repository's
working tree. It refuses if that tree has uncommitted changes, and it never commits. Only
reach for it after you have read the diff.

The work survives in its retained worktree and `logs/<task-id>/diff.patch`. The named
branch (`agent/<engine>/<task-id>`) identifies the worktree but does not itself contain
the uncommitted changes.

## When something goes wrong

- **`not available`** on dispatch — that engine is not installed. `orch_engines` lists
  what is; drop the `engine` argument and let the router choose.
- **Exit 0 with no output** — the task is marked failed on purpose. For antigravity this
  is how the known non-TTY stdout bug presents. Re-run on `claude`.
- **`stopped early: …`** — the turn budget ran out. Either the task was too big for one
  dispatch, or the engine looped. Split the task rather than raising the budget.
- **A task is stuck** — `orch_cancel` stops it and leaves the worktree in place so you
  can still read `orch_diff` and see how far it got.

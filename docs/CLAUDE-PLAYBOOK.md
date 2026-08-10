# Orchestration playbook

How to use the `orch` MCP tools from Claude Code. Claude is the orchestrator; the other
CLIs are workers. Nothing here is automatic — you decide what to delegate.

## The shape of every workflow

```
orch_engines        see what this machine has
orch_dispatch       queue work; returns a task_id immediately, does not wait
orch_wait           join point once you have dispatched everything
orch_diff           read what the engine actually did
orch_adopt          take it, or don't
```

`orch_dispatch` returns in milliseconds. A real task takes minutes. Never sit in a poll
loop on `orch_status` when `orch_wait` will block for you.

## Choosing a kind

`kind` selects the engine; you rarely need to name an engine yourself.

| kind | goes to | use it when |
|---|---|---|
| `implement` | codex | writing new behaviour |
| `refactor` | codex | changing structure, not behaviour |
| `test` | codex | adding or fixing tests |
| `review` | grok | you want findings on code that already exists |
| `investigate` | grok | you need to understand something before deciding |
| `ui_verify` | antigravity | it has to be checked in a browser |

`review` and `investigate` run read-only and cannot modify anything, so they are cheap to
reach for and safe to run against a repository you care about.

## Pattern: delegate one thing

Use when the work is well-specified and you would rather spend your own context on the
review than on the typing.

```
orch_dispatch(repo="~/dev/thing", task="…", kind="implement")
orch_wait(task_ids=["task-0007"], timeout_s=120)
orch_diff(task_id="task-0007")
```

Write the task text the way you would brief a competent stranger: what to change, what
not to change, and how to tell it worked. The engine sees the repository and your task
text, and nothing of this conversation.

## Pattern: fan out

Independent pieces of work, running at once in separate worktrees.

```
a = orch_dispatch(repo=…, task="add the parser",     kind="implement")
b = orch_dispatch(repo=…, task="add the serializer", kind="implement")
c = orch_dispatch(repo=…, task="document the format", kind="implement")
orch_wait(task_ids=[a, b, c], timeout_s=120)
```

Only fan out over pieces that do not touch the same files. Separate worktrees mean they
cannot corrupt each other mid-run, but two patches that both edit one function will
conflict when you adopt the second.

## Pattern: implement, then review it with a different engine

The most useful pattern here, and the reason the orchestrator is multi-vendor at all: the
reviewer has no stake in the implementation and did not talk itself into the design.

```
impl = orch_dispatch(repo=…, task="…", kind="implement")          # codex
orch_wait(task_ids=[impl])
orch_dispatch(repo=…, kind="review", parent_id=impl,
              task="Review the change on branch agent/codex/<id>: …")
```

Point the reviewer at the branch, and tell it what you actually doubt. "Review this" gets
you a summary; "check whether the retry loop can double-send" gets you an answer.

## Pattern: two engines, same task

When the approach is genuinely uncertain and you want to compare, not average.

```
a = orch_dispatch(repo=…, task=T, kind="implement", engine="codex")
b = orch_dispatch(repo=…, task=T, kind="implement", engine="grok", parent_id=a)
orch_wait(task_ids=[a, b], timeout_s=120)
orch_diff(a); orch_diff(b)
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

The work also survives on its branch (`agent/<engine>/<task-id>`), so nothing is lost if
you adopt nothing.

## When something goes wrong

- **`not available`** on dispatch — that engine is not installed. `orch_engines` lists
  what is; drop the `engine` argument and let the router choose.
- **Exit 0 with no output** — the task is marked failed on purpose. For antigravity this
  is how the known non-TTY stdout bug presents. Re-run on `claude`.
- **`stopped early: …`** — the turn budget ran out. Either the task was too big for one
  dispatch, or the engine looped. Split the task rather than raising the budget.
- **A task is stuck** — `orch_cancel` stops it and leaves the worktree in place so you
  can still read `orch_diff` and see how far it got.

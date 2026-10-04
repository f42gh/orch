---
description: Plan and delegate coding work through orch workflows
argument-hint: <work to orchestrate>
allowed-tools:
  - mcp__orch__orch_engines
  - mcp__orch__orch_routing_set
  - mcp__orch__orch_dispatch
  - mcp__orch__orch_run_create
  - mcp__orch__orch_run_dispatch
  - mcp__orch__orch_run_close
  - mcp__orch__orch_batch_dispatch
  - mcp__orch__orch_workflow_list
  - mcp__orch__orch_workflow_show
  - mcp__orch__orch_status
  - mcp__orch__orch_wait
  - mcp__orch__orch_result
  - mcp__orch__orch_diff
  - mcp__orch__orch_list
  - mcp__orch__orch_cancel
  - mcp__orch__orch_adopt
  - Read
  - Grep
  - Glob
  - Bash(git status:*)
  - Bash(git log:*)
  - Bash(git diff:*)
  - AskUserQuestion
---

Orchestrate this request with `orch`:

> $ARGUMENTS

Follow this workflow:

1. Inspect enough of the repository to understand the request and acceptance criteria, then split the work into tasks. Mark dependencies explicitly; do not parallelize tasks that depend on one another or are likely to edit the same code.
2. Call `orch_engines` before proposing an assignment; do not assume that every engine is available. If its routing table is empty or lacks a kind you need, set it up first: show the installed engines and ask the user how they want work split across them (for example, which engine should lead implementation and which should review). If they have no preference, route every kind to all installed engines. Then call `orch_routing_set` with the resulting ordered list per kind. This is saved for later sessions, so ask only once.
3. Before dispatching anything, ask the user to explicitly confirm all of the following:
   - **Run or Batch**: use a Run when tasks may be added later or when a later task depends on reviewing an earlier result. Use a Batch only when the complete set of independent tasks is known now.
   - **Routes**: the primary engine for every task kind that will be used. `agy` is accepted as an input alias for `antigravity`; outputs use the canonical name `antigravity`.
   - **Fallbacks**: omit a kind's fallback list to use the snapshotted automatic order, or provide a non-empty ordered list for a strict manual order. A manual list is exhaustive: never substitute an engine outside it.
4. After confirmation, create and dispatch exactly the approved workflow:
   - For a Run, call `orch_run_create`, add work with `orch_run_dispatch`, and wait for and review each result before deciding the next stage. Run membership and `parent_id` do not transfer uncommitted worktree changes. If a later task needs earlier code, pause until the user has approved, adopted, and committed it, then pass that commit as `base_ref`. Call `orch_run_close` when no more tasks will be added.
   - For a Batch, call `orch_batch_dispatch` once with the complete independent task set. Do not represent dependent stages as one Batch.
   - Use `orch_workflow_list` and `orch_workflow_show` to resume or inspect saved workflows.
5. Inspect every dispatch result for `spawn_error` before joining. A task with a spawn error is persisted but still queued for an external daemon; report it and do not include it in `orch_wait` until it has actually started. Join the other dispatched tasks with `orch_wait`; use `orch_status` only for a targeted progress check. For every finished task, read `orch_result` and `orch_diff`, and report failures, warnings, and conflicts before recommending adoption.
6. Call `orch_adopt` with its read-only patch strategy while reviewing. Apply changes to the real repository only after the user explicitly approves the reviewed patch. Never commit, push, or deploy on the user's behalf.

Finish with a concise account of the confirmed routes, task outcomes, reviewed changes, and anything that still needs a human decision.

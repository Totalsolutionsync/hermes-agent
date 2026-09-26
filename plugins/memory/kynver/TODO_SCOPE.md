# Hermes todos on Kynver AgentOS plans

With the Kynver substrate active, `KynverTodoStore` (`todo_store.py`) replaces the local
todo store: every `todo_list` write is projected to AgentOS plan progress rows and every
read reconciles from them. These are the rules it keeps.

## Whose rows

- Rows are keyed `hermes-todo:<scope>:<id>`. `<scope>` is the root of the session's
  compression lineage (`integration.session_todo_scope`), so compaction and a fresh
  gateway agent continue the same list; `/new`, branches and delegates start their own.
- A session only ever reads, supersedes or focuses its own rows. Other sessions' rows and
  legacy unscoped `hermes-todo:<id>` rows are never read back.
- Read-back asks Kynver for the session's rows only (`GET …/progress-rows?rowKeyPrefix=`).

## Which plan (automatic — there is no link command)

Kynver resolves the plan per session (`plan_binding.py` → `POST /todos/plan-binding`):
explicit → existing binding → task's plan → workspace **Inbox**.

| List | Plan |
| --- | --- |
| 1–2 items (a one-off) | the workspace Inbox |
| reaches 3 items while on the Inbox | the write first sends `promote`; Kynver creates the session's own draft plan, moves the rows already on the Inbox there and rebinds the session (`source: promoted`) |
| session opened on a task / bound to a plan | that plan, whatever the size |

A failed promotion leaves the list on the Inbox and the next write asks again; a Kynver
without promote support is asked once per resolver TTL. `KYNVER_PLAN_ID` is only a legacy
fallback for a Kynver without the binding endpoint.

## Done vs dropped

| Hermes status | Kynver row | Hermes output | Kynver plan UI |
| --- | --- | --- | --- |
| `completed` | `partial` | `✓ …` | ✓ Done |
| `cancelled` | `blocked` | `✗ … (dropped)` | ✗ Dropped (greyed, struck) |

A replace write that leaves an unfinished item off the new list cancels it — dropped work is
never reported as completed. Finished rows keep their outcome.

## Small reads

A read is the session's working set: every item on the current (local/hydrated) list plus
the session's unfinished rows in Kynver. Finished rows not on the current list — superseded
by a replace, or finished before this process/compaction — stay in AgentOS as history.
One `todo_list` result stays under `MAX_TODO_OUTPUT_CHARS`: the oldest finished items are
omitted first (still counted in `summary`, reported as `omitted_finished`), then item text
is shortened. Unfinished items are never omitted.

## Legacy rows

Before per-session keys every chat wrote `hermes-todo:<id>` rows into one shared plan.
Kynver's `POST /todos/legacy-archive { planId, dryRun?, restore? }` moves the finished ones
(with their events) to an `archived` archive plan; `restore: true` moves them back.

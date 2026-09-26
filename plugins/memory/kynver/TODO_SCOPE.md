# Hermes todos on Kynver AgentOS plans

With the Kynver substrate active, `KynverTodoStore` (`todo_store.py`) replaces the local
todo store. **Kynver plans are the single source of truth**: a chat's todo list is a live
view of shared plan progress rows, not a copy. These are the rules it keeps.

## One call per read or write

Every `todo_list` read or write is one `POST /api/agent-os/{slug}/todos/session`
(`todo_session.py`). Kynver resolves the session's plan, adopts a handed-over plan,
promotes an Inbox list to its own plan, applies the write and returns the list. It used to
take 6–8 calls (binding, row reads for guards, upserts, focus), which hit HTTP 429 on the
shared key. There is no `pre_tool_call` guard any more: Kynver enforces transitions inside
the call.

| | calls per write | calls per read |
| --- | --- | --- |
| before (legacy projection) | 6–8 (guard read, binding, promote, 2 row reads, upserts, focus, read-back ×2) | 2 (plan + rows) |
| now (`/todos/session`) | 1 | 1 |

## Anyone can tick it

- Whoever finishes an item marks it: this chat, another chat, a Hermes cron / worker-review
  session, a Kynver agent (chat tool or MCP), or Will in the Kynver UI. Kynver stamps every
  status change with who made it and from where (`hermes-chat`, `hermes-cron`,
  `kynver-agent`, `kynver-ui`, `mcp`, `api`).
- An item last changed by someone else comes back with `by`, and the checklist shows it:
  `✓ Worker x — by Hermes cron · Worker wake (Hermes cron)`.
- **Freshness:** reads always come from Kynver; newest change wins. The local list is only
  a cache for outages (see below).

## Picking up work (hand-off)

`todo_list(plan=…)` makes an existing plan this chat's list:

- a Kynver plan id, a task id (its plan), a Kynver link (`?plan=` / `?task=`), or a plan
  title (exact, else a unique partial match; ambiguous titles return the candidates);
- `session:<Hermes session id>` — another chat's list. Hermes turns the id into its todo
  scope (the id's compression-lineage root). A chat still on the Inbox is first promoted to
  its own plan so both sessions see the same rows.

Binding stays automatic for normal use; adoption is only for work handed over. Items owned
by someone else are addressed by the id the list shows (their row key). A replace
(`merge=false`) only drops **this chat's own** unfinished items — never someone else's.

## Parallel work

Any number of items may be `in_progress` at once — one per background worker. The plan's
focus pointer (`inProgressRowKey`) names the first; focusing a row never demotes the
others. `running` stays the harness executor lease (it already reads as in progress).

## Whose rows

- Rows are keyed `hermes-todo:<scope>:<id>`. `<scope>` is the root of the session's
  compression lineage (`integration.session_todo_scope`), so compaction and a fresh
  gateway agent continue the same list; `/new`, branches and delegates start their own.
- On the shared **Inbox** a chat sees only its own rows. On any other plan (its own, an
  adopted one, a task's plan) it sees every row of the plan, including items Will adds in
  Kynver.

## Which plan (automatic — there is no link command)

Kynver resolves the plan per session: explicit adopt → existing binding → task's plan →
workspace **Inbox**.

| List | Plan |
| --- | --- |
| 1–2 items (a one-off) | the workspace Inbox |
| reaches 3 items while on the Inbox | Kynver creates the session's own draft plan (named after its first item), moves the rows already written there and rebinds the session |
| session opened on a task / bound or adopted plan | that plan, whatever the size |

## Done vs dropped

| Hermes status | Kynver row | Hermes output | Kynver plan UI |
| --- | --- | --- | --- |
| `completed` | `partial` (plan rows: proposed `done`) | `✓ …` | ✓ Done |
| `cancelled` | `blocked` | `✗ … (dropped)` | ✗ Dropped (greyed, struck) |

A replace write that leaves one of this chat's unfinished items off the new list cancels
it — dropped work is never reported as completed. Plan rows that are not todo rows keep
their release rules (a ✓ that the linked task/PR does not back yet is refused per item and
reported in `sync.errors`).

## What a read contains

The session's current list: every unfinished row, plus finished rows that belong to the
current list — named in the last replace write, or finished (by anyone) after it. Items
finished before the list started (superseded lists, earlier sessions, an adopted plan's
old history) stay in Kynver as history. **Same-session ✓ items stay visible until the chat
replaces its list** (the 2026-09-26 bug where a merge made earlier ✓ items vanish is fixed
on both sides: Kynver keeps a list window per session, and the local cache no longer resets
when a hydrated session's scope resolves from a provisional id to its lineage root).

One `todo_list` result stays under `MAX_TODO_OUTPUT_CHARS`: the oldest finished items are
omitted first (still counted in `summary`, reported as `omitted_finished`), then item text
is shortened. Unfinished items are never omitted.

## Outages

Network errors, 5xx and HTTP 429 fall back to the local cache: the result carries
`sync: {"via": "local cache", "note": "Kynver unreachable (HTTP 429 …) …"}`, writes queue and
replay (oldest first) after `retry_interval_seconds`, and nothing retries Kynver inside
that window. A refusal on the merits (HTTP 4xx: unknown plan, ambiguous title) is a tool
error, not an outage. A Kynver without `/todos/session` (404/405) gets the legacy
multi-call projection (`plan_progress.py`, `plan_binding.py`), re-probed every 10 minutes.

## Nudges (cache-safe)

`agent/todo_nudge.py` adds one line to a **tool result** — never the system prompt or a
user message — when background work starts and the list has no active item for it:
`delegate_task(background=true)`, a `terminal` background job with `notify_on_complete`,
or any output containing `[background-work-started: <name>]` (printed by
`~/.hermes/scripts/launch-worker.sh`). The todo result itself carries a `hint` when an
item has been `in_progress` for 2 h with no update.

## Worker reviews (cron)

`launch-worker.sh` records the launching chat (`HERMES_SESSION_ID`) in
`~/artifacts/workers/meta/<name>.env`. The worker-review cron (job `12dd2c3aaf3c`) runs
as `hermes-cron`, picks up that chat's list with `todo_list(plan="session:<id>")`, and
marks the worker's item ✓ or ✗ once it has verified the outcome; the chat sees who did it.

## Legacy rows

Before per-session keys every chat wrote `hermes-todo:<id>` rows into one shared plan.
Kynver's `POST /todos/legacy-archive { planId, dryRun?, restore? }` moves the finished ones
(with their events) to an `archived` archive plan; `restore: true` moves them back.

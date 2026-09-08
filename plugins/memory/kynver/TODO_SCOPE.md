# Session todo scope and shared plan history

`KynverTodoStore` is a session working set, not a dump of a shared plan.

- Replace sets membership and order to the normalized submitted list. Merge updates
  existing IDs and appends new IDs. Read and write responses contain that full
  working set, including its completed items, rather than unrelated history.
- Remote titles, statuses, and plan focus still reconcile authoritatively for IDs
  in the working set. A remote focus outside the set demotes stale local focus but
  does not import the unrelated row. Read-back updates the local fallback cache.
- Partial merges project complete normalized values for touched IDs only, preserving
  omitted fields (including remote corrections). Rejected transitions restore the
  previous local membership. Existing remote projection/focus behavior is retained.
- Replace/clear never delete remote progress rows. The session linkage and existing
  `hermes-todo:<id>` keys are unchanged. This is not a remote ownership migration.
- A fresh store starts empty; callers must explicitly seed/restore its working set.
  The shared progress-row endpoint does not establish session ownership, so an
  empty session is **not** permission to import all historical todos.
- `reconcile_todos_from_kynver(..., local_scope_only=False)` retains the explicit
  plan-wide union behavior for cross-session consumers. The session store opts
  into `local_scope_only=True`; operating/context-envelope retrieval is unchanged.

## Bounds and limitations

Response membership is bounded by the current session working set, not by total
plan history. This is not an arbitrary character truncation: no selected session
items are silently omitted. Very large user-created working sets remain large.
The existing remote endpoint still transfers the global row list; this change
bounds model context, not network traffic. No undocumented session filter is sent.
Existing plan-wide row-key collisions and focus semantics are not redesigned.
The legacy `transform_todo_result` utility retains plan-wide behavior; it is not
used by the session store or the current no-op post-tool hook.

## Offline regression coverage

`tests/plugins/memory/test_kynver_todo_scope.py` uses a stateful fake containing
1,000 historical completed rows (serialized history >111 KB). It exercises actual
`todo_tool` replace/partial merge/read responses (<1 KB for three current items),
append, replacement, empty clear, compression injection, immutable remote history,
external authoritative status/title/focus changes, unchanged session identity,
blocked-transition rollback, and degraded fallback. Existing plan-wide tests stay
in place to protect cross-session reconciliation. No live AgentOS writes are used.

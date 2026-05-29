# Kynver AgentOS Hermes plugin

Standalone adapter for Hermes↔Kynver operating state. Bundled under
`plugins/memory/kynver/` for memory-provider discovery; also registers
general hooks via `plugins/kynver/`.

## Default-on behavior

When `KYNVER_API_URL`, `KYNVER_API_KEY`, and `KYNVER_AGENT_OS_SLUG` are set in the
active profile `.env` and `GET /api/agent-os/{slug}/stats` succeeds, Kynver becomes
the active external memory provider and owns todo/task projection — without
requiring `memory.provider: kynver` in config.

Explicit opt-out: `KYNVER_DISABLED=1`, `HERMES_KYNVER_DISABLED=1`, or
`kynver.disabled: true` in config.yaml.

## Operating anchors

Set via env or `kynver:` config keys:

- `KYNVER_PLAN_ID`, `KYNVER_PLAN_VERSION_ID`, `KYNVER_TASK_ID`, `KYNVER_PROGRESS_ROW_KEY`
- `KYNVER_GOAL_ID`, `KYNVER_PROJECT_ID`

Missing plan/task anchors trigger intake classification tasks instead of
unparented operational work.

## HTTP contract

Routes match `@kynver-app/mcp-agent-os` (not MCP operation pseudo-paths):

- `GET /memory?q=…&sourceId=hermes:forge`
- `POST /memory`
- `GET /context-envelope?anchorType=…&anchorId=…`
- `POST /sessions`, `POST /sessions/{id}/events`, `PATCH /sessions/{id}`
- `POST /tasks`, `PATCH /tasks/{id}`, `POST /tasks/{id}/close`
- `GET /skills?view=manifest`

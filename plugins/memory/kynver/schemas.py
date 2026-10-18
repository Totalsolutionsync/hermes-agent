"""Kynver AgentOS tool schemas exposed through the memory provider."""

MEMORY_SEARCH_SCHEMA = {
    "name": "kynver_memory_search",
    "description": (
        "Search authoritative Kynver AgentOS memory/context. Results contain only complete memories. "
        "Use key to expand an exact memory named in an INDEX line."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Natural language memory query."},
            "key": {"type": "string", "description": "Exact memory key/slug to expand in full."},
            "k": {"type": "integer", "description": "Maximum results, default 5, max 20."},
        },
        "anyOf": [{"required": ["query"]}, {"required": ["key"]}],
    },
}

MEMORY_WRITE_SCHEMA = {
    "name": "kynver_memory_write",
    "description": (
        "Write durable memory to Kynver AgentOS with Hermes Forge provenance. "
        "To fix a stale or wrong memory, set supersedes to its key and give a reason: "
        "Kynver stores an audited correction and retires the old entry."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "Memory content to store."},
            "key": {"type": "string", "description": "Optional stable memory key. For a correction, the new entry's key (must differ from supersedes; defaults to '<supersedes>-correction')."},
            "memoryType": {"type": "string", "description": "fact, decision, preference, lesson, or runbook."},
            "volatile": {
                "type": "boolean",
                "description": (
                    "true when the content is a temporary current state that will change on its own "
                    "(an outage, a pause, a drive or service being down, a PR/deploy status). Recall then flags "
                    "it as unverified so it is rechecked instead of trusted after it goes stale."
                ),
            },
            "fixRef": {
                "type": "string",
                "description": (
                    "For a limitation or blocker (X can't do Y, hold Z until W): the change that removes it, as a "
                    "GitHub PR/issue/commit URL or owner/repo#123. Kynver retires the memory automatically when that fix ships."
                ),
            },
            "supersedes": {"type": "string", "description": "Key of an existing memory this content corrects and replaces."},
            "reason": {"type": "string", "description": "Why the old entry is being replaced. Required with supersedes."},
            "reasonClass": {
                "type": "string",
                "enum": ["requirement_change", "memory_wrong", "test"],
                "description": (
                    "Required with supersedes. requirement_change: the user changed an instruction or rule, "
                    "so the old memory was right when written. memory_wrong: the stored fact was false or stale. "
                    "test: a self-test of this tool. Only memory_wrong counts against memory-quality health."
                ),
            },
        },
        "required": ["content"],
    },
}

MEMORY_CHECK_MANUAL_STEP_SCHEMA = {
    "name": "kynver_memory_check_manual_step",
    "description": (
        "Call this BEFORE asking Will to do anything by hand (run a script, click something, use Admin PowerShell). "
        "Pass the request as you were about to make it. Returns automated=true with the memories saying it is "
        "already automated or done by Forge (then do it yourself or skip it; do not ask Will), any retired "
        "'Will must do it by hand' notes, and replaced notes the text repeats."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "The by-hand request, e.g. 'run the compaction script from Admin PowerShell'."},
        },
        "required": ["text"],
    },
}

MEMORY_LIMITATION_CHECK_SCHEMA = {
    "name": "kynver_memory_limitation_check",
    "description": (
        "Before saying something can't be done, or holding work because of a limitation, check it here. "
        "Returns open (recorded blocker, fix not shipped), fixed (its fix merged and deployed: do not report it "
        "as a blocker), or unverified (no live record: recheck the real system first)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "The limitation as you would state it, e.g. 'hold Facebook for the AI label'."},
            "subject": {"type": "string", "description": "Or a short subject, e.g. 'kariad facebook reel ai label'."},
        },
        "anyOf": [{"required": ["text"]}, {"required": ["subject"]}],
    },
}

MEMORY_DECISION_CHECK_SCHEMA = {
    "name": "kynver_memory_decision_check",
    "description": (
        "Before acting on an older todo, plan item, cron prompt or brief (posting, booking, scheduling, merging), "
        "check the newest recorded decisions about its subject. Returns decisions newest first (key, updatedAt, one line); "
        "follow the newest over the older instruction. Empty means no recorded decision mentions it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "entity": {"type": "string", "description": "Short title, PR number or id, e.g. 'Rufford Ford' or '#4309'."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10, "description": "Max decisions (default 3)."},
        },
        "required": ["entity"],
    },
}

TASK_CREATE_SCHEMA = {
    "name": "kynver_task_create",
    "description": "Create a Kynver AgentOS task/control-plane record.",
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short task title."},
            "description": {"type": "string", "description": "Task details."},
            "priority": {"type": "string", "description": "low, normal, high, or critical."},
            "executor": {"type": "string", "description": "inline, harness, acp, or manual."},
            "executorRef": {"type": "string"},
            "parentTaskId": {"type": "string"},
            "goalId": {"type": "string"},
            "projectId": {"type": "string"},
            "personaSlug": {"type": "string"},
            "scheduledFor": {"type": "string", "description": "ISO-8601 scheduled start time."},
            "dependsOnTaskIds": {"type": "array", "items": {"type": "string"}},
            "idempotencyKey": {"type": "string", "description": "Optional stable idempotency key."},
            "requestId": {"type": "string", "description": "Raw request id used as a dedupe key by AgentOS."},
        },
        "required": ["title"],
    },
}

TASK_UPDATE_SCHEMA = {
    "name": "kynver_task_update",
    "description": "Patch status, priority, links, progress, or artifact refs on a Kynver task.",
    "parameters": {
        "type": "object",
        "properties": {
            "taskId": {"type": "string", "description": "Kynver task id."},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "status": {"type": "string", "description": "ready, running, waiting, scheduled, blocked, needs_input, awaiting_review, done, failed, or cancelled."},
            "priority": {"type": "string"},
            "executor": {"type": "string"},
            "executorRef": {"type": "string"},
            "parentTaskId": {"type": "string"},
            "goalId": {"type": "string"},
            "projectId": {"type": "string"},
            "personaSlug": {"type": "string"},
            "scheduledFor": {"type": "string"},
            "dependsOnTaskIds": {"type": "array", "items": {"type": "string"}},
            "lastSummary": {"type": "string"},
            "blocker": {"type": "string"},
            "branch": {"type": "string"},
            "worktreePath": {"type": "string"},
            "prUrl": {"type": "string"},
            "headCommit": {"type": "string"},
        },
        "required": ["taskId"],
    },
}

TASK_LIST_SCHEMA = {
    "name": "kynver_task_list",
    "description": "List Kynver AgentOS tasks.",
    "parameters": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "Optional status filter."},
            "limit": {"type": "integer", "description": "Maximum tasks, default 20."},
        },
    },
}

TASK_CLOSE_SCHEMA = {
    "name": "kynver_task_close",
    "description": "Close a Kynver AgentOS task with a terminal status.",
    "parameters": {
        "type": "object",
        "properties": {
            "taskId": {"type": "string"},
            "status": {"type": "string", "description": "done, failed, or cancelled. Defaults to done."},
            "summary": {"type": "string"},
            "message": {"type": "string"},
        },
        "required": ["taskId"],
    },
}

TASK_LOG_EVENT_SCHEMA = {
    "name": "kynver_task_log_event",
    "description": "Append an audit/progress event to a Kynver AgentOS task.",
    "parameters": {
        "type": "object",
        "properties": {
            "taskId": {"type": "string"},
            "eventType": {"type": "string", "description": "created, started, worker_update, blocked, steer, artifact, review, done, or failed."},
            "message": {"type": "string"},
            "payload": {"type": "object"},
            "artifactVisibility": {"type": "string"},
            "eventKey": {"type": "string"},
            "metadata": {"type": "object"},
        },
        "required": ["taskId", "eventType"],
    },
}

TASK_STEER_SCHEMA = {
    "name": "kynver_task_steer",
    "description": "Send a steering artifact to a Kynver AgentOS task if supported.",
    "parameters": {
        "type": "object",
        "properties": {
            "taskId": {"type": "string"},
            "message": {"type": "string"},
            "detail": {"type": "object"},
            "eventKey": {"type": "string"},
            "metadata": {"type": "object"},
        },
        "required": ["taskId", "message"],
    },
}

SKILL_LIST_SCHEMA = {
    "name": "kynver_skill_list",
    "description": "List Kynver AgentOS skill manifests without fetching full bodies.",
    "parameters": {
        "type": "object",
        "properties": {
            "category": {"type": "string", "description": "Optional category filter."},
            "limit": {"type": "integer", "description": "Maximum manifests, default 50."},
        },
    },
}

SKILL_SEARCH_SCHEMA = {
    "name": "kynver_skill_search",
    "description": "Filter Kynver AgentOS skill manifests client-side.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "description": "Maximum manifests, default 20."},
        },
        "required": ["query"],
    },
}

SKILL_GET_SCHEMA = {
    "name": "kynver_skill_get",
    "description": "Fetch a full Kynver skill body on demand as external user-authored content.",
    "parameters": {
        "type": "object",
        "properties": {
            "skillId": {"type": "string", "description": "Skill id, slug, or name from Kynver manifests."},
            "source": {"type": "string", "description": "Optional builtin or user source."},
        },
        "required": ["skillId"],
    },
}

ALL_TOOL_SCHEMAS = [
    MEMORY_SEARCH_SCHEMA,
    MEMORY_WRITE_SCHEMA,
    MEMORY_LIMITATION_CHECK_SCHEMA,
    MEMORY_CHECK_MANUAL_STEP_SCHEMA,
    MEMORY_DECISION_CHECK_SCHEMA,
    TASK_CREATE_SCHEMA,
    TASK_UPDATE_SCHEMA,
    TASK_LIST_SCHEMA,
    TASK_CLOSE_SCHEMA,
    TASK_LOG_EVENT_SCHEMA,
    TASK_STEER_SCHEMA,
    SKILL_LIST_SCHEMA,
    SKILL_SEARCH_SCHEMA,
    SKILL_GET_SCHEMA,
]

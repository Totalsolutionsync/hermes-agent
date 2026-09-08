# Context-efficient continuation

## Scope

The default compressor requests a current-state handoff instead of an
accumulating action diary. Its five sections cover the latest unfinished
objective/acceptance criteria, necessary constraints/decisions, exact active
state, verified results/evidence pointers, and pending actions/blockers.
Iterative updates replace superseded state rather than append numbered actions.
Tool-call IDs are serialized alongside calls so a result can be matched to its
source. Prior handoffs are redacted before being sent to the auxiliary model.

Delegated workers use the same result-first convention: exact handles, verified
outcomes and evidence locations, then unfinished work. Task-specific response
formats take precedence.

This is a bounded prompt/serialization improvement, not a new evidence database:
LLM compliance and retention of every evidence pointer are not guaranteed.
Existing transcript persistence, compaction boundaries, protected recent turns,
summary budgets, thresholds, and deterministic fallback are unchanged. Relevant
raw artifacts/logs should remain available; the handoff points to them rather
than substituting a summary for primary evidence. Protected newer user messages
remain authoritative over the earlier handoff.

## Narrow skill loading (existing API)

Use `skill_view(name="example", summary=True)` for metadata and a section index
without the full body or inline-command preprocessing. Then use
`skill_view(name="example", section="Procedure")` for a named section, or
`skill_view(name="example", max_bytes=4000)` for a budgeted view.
There is no new `sections` request parameter.

The existing selector matches case-insensitively with normalized whitespace.
It loads the text up to the next ATX heading, not the entire heading subtree;
load needed child sections separately. Missing titles return an error with the
index. Duplicate titles select the first match. The parser is a simple ATX
scanner, not a complete Markdown parser: fenced examples can appear as headings.
If selection is ambiguous, use the full view.

`max_bytes` is a soft UTF-8 content budget, not a hard response-size limit:
omission markers, separators, and mandatory sections can exceed it. Overview,
When to Use, Safety, Pitfalls, and Verification are protected from truncation;
`metadata.hermes.mandatory_sections` can add titles. This protection applies to
truncation, not selection: a named section does not automatically include other
safety or prerequisite sections. Read those explicitly before acting, or load
the full skill when unsure. Selection occurs before preprocessing, while byte
truncation occurs after it; a byte limit is not a sandbox for inline commands.

Full views remain the default. Linked-file access is unchanged and ignores
these selective options. No slash-command/preload behavior or global skill
binding is changed. This port leaves the existing skill implementation intact.

## Validation and non-goals

`tests/agent/test_context_efficient_handoffs.py` exercises auxiliary request
construction, repeated-compaction evidence identity, secret redaction, and the
worker handoff contract. These are offline request-contract tests, not proof of
LLM semantic compliance or measured production token savings.

`tests/agent/test_context_compressor_operational_template.py` checks operational
continuity requirements in the actual request rather than old section names.
Existing `tests/tools/test_skill_sections_loading.py` and
`tests/tools/test_skills_tool.py` cover selective loading and full-view behavior.

No todo synchronization behavior, live AgentOS state, gateway process, or profile
configuration is changed. Historical todo-output diagnosis and filtering are
separate from this scoped implementation.

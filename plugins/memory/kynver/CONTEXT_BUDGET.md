# Kynver memory context budget

Kynver recall requests the full server view. Model-facing context admits complete memories in retrieval rank order; an item that does not fit is skipped and listed in an INDEX with an exact `kynver_memory_search(key="...")` expansion call. Memory bodies are never clipped.

Behavioral settings belong in `config.yaml`:

```yaml
memory:
  provider: kynver
kynver:
  prefetch_token_budget: 2000
  search_token_budget: 4000
  relevance_score_floor: 0.01
  max_index_items: 8
```

Automatic prefetch keeps a per-session fingerprint set. An unchanged memory is injected once in that session; changed content is eligible again. The block remains per-turn user context. It does not modify the system prompt or its cached prefix.

## Automatic memory-quality feedback

The provider records Kynver memory-quality feedback (`POST /memory/quality-feedback`, the route behind `agent_os_record_memory_quality_feedback`) without the agent having to call anything:

- `retrieval_miss` (negative) when prefetch or `kynver_memory_search` fails (`note`: `rate_limited`, `unreachable`, `error`, including turns served the degraded notice) or returns nothing relevant (`note`: `empty`);
- `retrieval_used` (positive) with `memoryIds` when memories are injected, at most once per query per session;
- `human_correction` (negative) when the user's message reads like a correction ("I already told you", "don't go backwards", "you're Forge, you …"). `correctionReasonClass` is left unset for the daily scan to label.

Posts run on a background thread and never delay a turn. A post that fails because Kynver is down or rate-limited is kept in a bounded buffer (`<HERMES_HOME>/kynver/quality_feedback_queue.jsonl`) and sent after the next successful Kynver call. Observe-only mode and `KYNVER_MEMORY_DISABLED` record nothing. The route requires operator (owner/admin) access on the AgentOS.

```yaml
kynver:
  quality_feedback: true        # or a mapping:
  # quality_feedback:
  #   enabled: true
  #   retrieval_miss: true
  #   retrieval_used: true
  #   human_correction: true
```

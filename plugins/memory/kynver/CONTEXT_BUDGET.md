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

# Hermes Kynver plugin — M3.5 smoke evidence

**Milestone:** M3.5 — Multi-Repo Adapter Delivery Contract
**Date:** 2026-06-27

---

## Version pins (at smoke time)

| Artifact | Value |
|----------|-------|
| Kynver commit (M3.5 branch base) | `fc5a46f10321e98f158241a49a37ed0611e1647a` |
| Hermes commit | `f754cd517` (test(kynver): isolate blank-provider memory init) |
| Hermes plugin version (full operating) | `1.0.0` (`plugins/memory/kynver/plugin.yaml`) |
| Kynver MCP AgentOS package | `@kynver-app/mcp-agent-os@0.3.50` |
| Kynver Runtime package | `@kynver-app/runtime@0.1.158` |
| Hermes profile config mode | `enabled` (`KYNVER_AGENTOS_MODE=enabled`) |
| Kynver M3 plugin version | `0.3.0` (`Kynver: plugins/memory/kynver/plugin.yaml`) |

---

## Contract fixture

**Location (Kynver):** `tests/plugins/memory/fixtures/kynver-context-envelope-contract-v1.json`

Contract fixture defines:
- Required fields: `contractVersion`, `envelopeId`, `anchor`, `memories[*].content`, `memories[*].slug`
- Optional: `recentSessions`, `currentFocus`, `persona`, `followUps`
- Inline equivalent embedded in `test_kynver_provider.py` as `_M35_CONTRACT_FIXTURE`

---

## Test run evidence

```bash
# Full Hermes provider smoke (via venv)
/home/pizop/.hermes/hermes-agent/venv/bin/python \
  -m pytest tests/plugins/memory/test_kynver_provider.py -q -o 'addopts='
# Result: 18 passed, 1 warning in 2.53s (2026-06-27)
# M3.5 additions: 4 new tests all PASSED
```

```bash
# Kynver-side smoke (Python 3.x)
python3 -m pytest tests/plugins/memory/ -q -o 'addopts='
# Result: 18 passed in 0.06s (2026-06-27)
# M3.5 additions: 5 new tests all PASSED
```

---

## M3.5 smoke checklist

| # | Scenario | Status | Test |
|---|----------|--------|------|
| 1 | Contract fixture has required fields and version pins | **PASS** | `test_m35_contract_fixture_loads_and_has_required_fields` (Kynver) |
| 2 | Fixture → M3 formatter → safe `# Kynver AgentOS context` block | **PASS** | `test_m35_contract_fixture_flows_through_m3_formatter_to_safe_memory_context_block` (Kynver) |
| 3 | Fixture produces no credentials or injection patterns (M3) | **PASS** | `test_m35_contract_fixture_produces_no_unsafe_prompt_content` (Kynver) |
| 4 | Full pipeline: client → prefetch() → safe block (M3) | **PASS** | `test_m35_prefetch_with_contract_fixture_produces_safe_block_end_to_end` (Kynver) |
| 5 | Rollback: disabled M3 provider returns `""` → Hermes uses local fallback | **PASS** | `test_m35_rollback_path_disabled_provider_returns_empty_prefetch` (Kynver) |
| 6 | Fixture → _coerce_items() → _format_context() → safe block (full provider) | **PASS** | `test_m35_compat_fixture_flows_through_full_provider_format_context` (Hermes) |
| 7 | Full provider canonical header is `## Kynver AgentOS Context` | **PASS** | `test_m35_compat_fixture_format_produces_canonical_header` (Hermes) |
| 8 | Full pipeline produces no credentials in context block | **PASS** | `test_m35_compat_fixture_produces_no_credentials_in_context_block` (Hermes) |
| 9 | Rollback: observe_only → is_authoritative_context() False → local fallback active | **PASS** | `test_m35_rollback_observe_only_keeps_local_fallback` (Hermes) |

---

## Rollback path verified

Three rollback options available (all unit-tested or documented):

1. `KYNVER_DISABLED=true` or `HERMES_KYNVER_DISABLED=true` → `substrate.py` / `kynver_explicitly_disabled()` → no-op
2. Remove `KYNVER_API_KEY` → `is_available()` returns `False` → provider is no-op
3. Switch `memory.provider: local` in config.yaml → Hermes uses local MEMORY.md/USER.md

M3 provider is `kind: additive` — never suppresses local fallback.  Rollback is safe and lossless.

---

## Adapter config used for dogfood

```ini
# ~/.hermes/.env
KYNVER_API_KEY=[REDACTED]
KYNVER_API_URL=https://www.kynver.com
KYNVER_AGENT_OS_SLUG=ghost
KYNVER_FETCH_TIMEOUT_MS=6000
KYNVER_AGENTOS_MODE=enabled
```

```yaml
# config.yaml (full operating provider)
memory:
  provider: kynver
kynver:
  mode: enabled
```

---

## Handoff

Do not merge from this smoke evidence alone. Both PRs must have CI green + Lorentz review before enabling Kynver-first memory writes (M4) in production.

- Kynver PR: `feat/m3.5-multirepo-adapter-contract` (open against `Totalsolutionsync/Kynver:main`)
- Hermes PR: Against `Totalsolutionsync/hermes-agent` fork (or `fork/main`) with this smoke evidence

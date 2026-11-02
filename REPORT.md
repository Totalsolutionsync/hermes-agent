# Kynver plugin: source ids follow the running profile

Default profile (and no profile env): unchanged, `hermes:forge` and `hermes:forge:flush`.
Fortress profile (Shade): `hermes:fortress` and `hermes:fortress:flush`. Any other named profile: `hermes:<profile>`.

Kynver repo has no existing `hermes:<profile>` convention for tabs (grepped ~/repos/Kynver), so I used the profile name. Rename in `source_id.py` if the Shade tab expects `hermes:shade`.

## What changed (6 code paths, 1 new module)
- New `source_id.py`: resolves from `get_hermes_home()` via `profile_name_for_home`, evaluated per call (one process serves many profiles).
- `__init__.py`: module constant `SOURCE_ID` removed, all 14 uses call the resolver; the scope check now accepts any `hermes:` source (was `hermes:forge` only). The keyword list entry `hermes:forge` stays (it is a text keyword, not a write tag).
- `compaction_flush.py`: flush id from resolver; the security-lane regex now matches `hermes:<anything>:flush`, so fortress flush rows stay excluded.
- `skill_sync/service.py`: default source id from resolver.
- Tests: `tests/plugins/memory/test_kynver_source_id.py` (4); with compaction_flush, provider, skill_sync tests: 121 passed.

No existing memory rows were read, listed, moved or retagged. Live checkout and gateway untouched.
Type check + build: left for Forge.

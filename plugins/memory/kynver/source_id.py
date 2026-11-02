"""Source ids for Kynver writes, derived from the running Hermes profile.

The default profile keeps the historical ``hermes:forge`` id so existing Forge rows and recall are
unchanged; any named profile (e.g. ``fortress``) writes as ``hermes:<profile>`` so its facts never
surface as Forge's.
"""

from __future__ import annotations

from hermes_constants import get_hermes_home, profile_name_for_home

DEFAULT_SOURCE_ID = "hermes:forge"
FLUSH_SUFFIX = ":flush"


def resolve_source_id() -> str:
    profile = profile_name_for_home(get_hermes_home())
    if not profile or profile == "default":
        return DEFAULT_SOURCE_ID
    return f"hermes:{profile}"


def resolve_flush_source_id() -> str:
    return resolve_source_id() + FLUSH_SUFFIX

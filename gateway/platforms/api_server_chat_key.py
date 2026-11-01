"""``X-Kynver-Chat-Key``: the Kynver agent tab a /v1 turn belongs to.

The Kynver runtime sends ``kynver-tab:<tab session key>`` with every tab turn. Hermes remembers it
per session id so the Kynver todo store can send it as ``chatKey`` and the tab's To do panel shows
the session's list. Absent or malformed headers are ignored; other sessions are unaffected.
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict
from typing import Optional

HEADER = "X-Kynver-Chat-Key"
PREFIX = "kynver-tab:"
MAX_LEN = 200
_MAX_SESSIONS = 2048
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

_keys: OrderedDict[str, str] = OrderedDict()
_lock = threading.Lock()


def parse_chat_key(raw: Optional[str]) -> Optional[str]:
    value = (raw or "").strip()
    if not value.startswith(PREFIX) or len(value) == len(PREFIX) or len(value) > MAX_LEN:
        return None
    return None if _CONTROL.search(value) else value


def remember_chat_key(request, session_id: Optional[str]) -> None:
    """Remember the request's valid header for ``session_id``; no header leaves it unchanged."""
    key = parse_chat_key(request.headers.get(HEADER))
    if not key or not session_id:
        return
    with _lock:
        _keys[session_id] = key
        _keys.move_to_end(session_id)
        while len(_keys) > _MAX_SESSIONS:
            _keys.popitem(last=False)


def chat_key_for_session(session_id: Optional[str]) -> Optional[str]:
    with _lock:
        return _keys.get(session_id or "")

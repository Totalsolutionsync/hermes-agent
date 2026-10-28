"""Kynver todo list label: never a bare chat-platform label ("Telegram" for "Telegram · Will H")."""

from __future__ import annotations

import json
from types import SimpleNamespace

from plugins.memory.kynver.integration import todo_session_label


class _DB:
    def __init__(self, row=None, title=None):
        self.row, self.title, self.reads = row, title, 0

    def get_session(self, session_id):
        self.reads += 1
        return self.row

    def get_session_title(self, session_id):
        return self.title


def _agent(platform="telegram", chat_name=None, user_name=None, db=None, session_id="s1"):
    return SimpleNamespace(platform=platform, _chat_name=chat_name, _user_name=user_name,
                           _session_db=db, session_id=session_id)


def test_live_source_name_wins():
    assert todo_session_label(_agent(chat_name="Will H", db=_DB({"display_name": "Other"}))) == "Telegram · Will H"


def test_user_name_when_no_chat_name():
    assert todo_session_label(_agent(user_name="Will  H")) == "Telegram · Will H"


def test_name_resolved_from_stored_session_row():
    db = _DB({"display_name": "Will H"})
    assert todo_session_label(_agent(db=db)) == "Telegram · Will H"


def test_name_resolved_from_stored_origin():
    db = _DB({"display_name": None, "origin_json": json.dumps({"chat_name": None, "user_name": "Will H"})})
    assert todo_session_label(_agent(db=db)) == "Telegram · Will H"


def test_session_title_as_last_resort():
    db = _DB({"display_name": None, "origin_json": None}, title="Kynver todo work")
    assert todo_session_label(_agent(db=db)) == "Telegram · Kynver todo work"


def test_unknown_name_on_chat_platform_sends_no_label():
    """The bug: a fresh agent without the chat name sent "Telegram", renaming the list."""
    assert todo_session_label(_agent(db=_DB(None))) is None
    assert todo_session_label(_agent(db=None)) is None
    assert todo_session_label(_agent(platform="discord", db=_DB({"display_name": ""}))) is None


def test_session_lookup_failure_sends_no_label():
    class Broken(_DB):
        def get_session(self, session_id):
            raise RuntimeError("db locked")
    assert todo_session_label(_agent(db=Broken())) is None


def test_unnamed_surfaces_keep_bare_platform_label():
    assert todo_session_label(_agent(platform="cli", db=None)) == "CLI"
    assert todo_session_label(_agent(platform="cron", db=_DB(None))) == "Hermes cron"
    assert todo_session_label(_agent(platform="", db=None)) == "Hermes"
    assert todo_session_label(_agent(platform="cron", db=_DB(None, title="Worker wake"))) == "Hermes cron · Worker wake"


def test_resolved_name_is_cached_per_session():
    db = _DB({"display_name": "Will H"})
    agent = _agent(db=db)
    todo_session_label(agent)
    todo_session_label(agent)
    assert db.reads == 1
    agent.session_id = "s2"  # compression rotated the id: look again
    assert todo_session_label(agent) == "Telegram · Will H"
    assert db.reads == 2


def test_miss_is_not_cached():
    db = _DB(None)
    agent = _agent(db=db)
    assert todo_session_label(agent) is None
    db.row = {"display_name": "Will H"}
    assert todo_session_label(agent) == "Telegram · Will H"

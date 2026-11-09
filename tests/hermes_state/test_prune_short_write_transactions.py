"""prune_sessions must not hold the store's write lock for the whole sweep.

A single transaction over thousands of sessions (FTS + display triggers per message row) held
the write lock for ~17 minutes, so API run admission, cron admission and turn-lease refresh all
failed meanwhile. Deletes now commit in small batches, each its own write transaction.
"""

import time

import pytest

import hermes_state_maintenance as maint
from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    store = SessionDB(tmp_path / "state.db")
    yield store
    store.close()


def _old_ended_sessions(db, n):
    ids = []
    old = time.time() - 200 * 86400
    for i in range(n):
        sid = f"20260101_0000{i:02d}_old{i:03d}"
        db.create_session(sid, source="cli")
        with db._lock:
            db._conn.execute(
                "UPDATE sessions SET ended_at=?, started_at=?, message_count=1 WHERE id=?", (old, old, sid)
            )
            db._conn.commit()
        ids.append(sid)
    return ids


def _count_writes(db, monkeypatch):
    calls = []
    real = db._execute_write

    def spy(fn, *a, **kw):
        calls.append(fn)
        return real(fn, *a, **kw)

    monkeypatch.setattr(db, "_execute_write", spy)
    return calls


def test_prune_commits_deletes_in_batches(db, monkeypatch):
    ids = _old_ended_sessions(db, 7)
    monkeypatch.setattr(maint, "_PRUNE_DELETE_BATCH", 3)
    calls = _count_writes(db, monkeypatch)
    deleted = []
    assert db.prune_sessions(older_than_days=90, deleted_ids=deleted) == 7
    # select+fence, ceil(7/3)=3 delete batches, system-prompt cleanup
    assert len(calls) >= 5
    assert sorted(deleted) == sorted(ids)
    assert db._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_prune_failure_mid_sweep_reports_committed_batches(db, monkeypatch):
    _old_ended_sessions(db, 6)
    monkeypatch.setattr(maint, "_PRUNE_DELETE_BATCH", 2)
    real = db._execute_write
    seen = {"n": 0}

    def flaky(fn, *a, **kw):
        if getattr(fn, "__name__", "") == "_delete_chunk":
            seen["n"] += 1
            if seen["n"] == 2:
                raise RuntimeError("disk full")
        return real(fn, *a, **kw)

    monkeypatch.setattr(db, "_execute_write", flaky)
    deleted = []
    with pytest.raises(RuntimeError):
        db.prune_sessions(older_than_days=90, deleted_ids=deleted)
    assert len(deleted) == 2  # the first batch committed and is announced
    left = db._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    assert left == 4
    # the next pass picks the remainder up
    monkeypatch.setattr(db, "_execute_write", real)
    assert db.prune_sessions(older_than_days=90) == 4


def test_other_writer_gets_in_between_batches(db, monkeypatch):
    """Another write is admitted while a prune is mid-sweep (no whole-sweep lock)."""
    _old_ended_sessions(db, 4)
    monkeypatch.setattr(maint, "_PRUNE_DELETE_BATCH", 1)
    real = db._execute_write
    state = {"injected": False}

    def interleave(fn, *a, **kw):
        out = real(fn, *a, **kw)
        if getattr(fn, "__name__", "") == "_delete_chunk" and not state["injected"]:
            state["injected"] = True
            db.create_session("20261108_181400_freshx", source="api_server")  # a new chat admission
        return out

    monkeypatch.setattr(db, "_execute_write", interleave)
    assert db.prune_sessions(older_than_days=90) == 4
    assert state["injected"]
    assert db.get_session("20261108_181400_freshx") is not None

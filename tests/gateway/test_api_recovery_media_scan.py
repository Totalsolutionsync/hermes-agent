"""Startup API-turn recovery must not hold the event loop behind the media history scan."""
import asyncio
import time
from types import SimpleNamespace

from gateway import session_api_turn, session_ingress_media


def test_media_gc_runs_off_loop_while_liveness_ticks(monkeypatch):
    monkeypatch.setattr(session_ingress_media, 'collect_unheld_api_images',
                        lambda db: time.sleep(0.5) or 0)
    monkeypatch.setattr(session_api_turn, 'owner_scope', lambda a: __import__('contextlib').nullcontext(), raising=False)
    authority = SimpleNamespace(db=object())

    async def scenario():
        ticks = 0
        stop = asyncio.Event()

        async def liveness():
            nonlocal ticks
            while not stop.is_set():
                ticks += 1
                await asyncio.sleep(0.02)
        probe = asyncio.create_task(liveness())
        started = time.monotonic()
        session_api_turn._schedule_unheld_media_collection(authority)
        scheduling = time.monotonic() - started
        await asyncio.sleep(0.4)
        stop.set()
        await probe
        return scheduling, ticks

    scheduling, ticks = asyncio.run(scenario())
    assert scheduling < 0.2  # the scan is not awaited by recovery
    assert ticks >= 10       # loop kept turning during the 0.5s scan


def test_history_scan_over_budget_keeps_files(tmp_path, monkeypatch):
    import sqlite3
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE messages (id INTEGER PRIMARY KEY, content TEXT)')
    conn.executemany('INSERT INTO messages(content) VALUES (?)', [('x',)] * 50)
    monkeypatch.setattr(session_ingress_media, '_SCAN_SLICE_ROWS', 10)
    refs = [{'path': '/m/abc/api_abc.png', 'sha256': 'abc'}]
    try:
        session_ingress_media._history_mentions(conn, refs, deadline=time.monotonic() - 1)
    except session_ingress_media.HistoryScanBudgetExceeded:
        pass
    else:
        raise AssertionError('expected budget abort')
    conn.execute("UPDATE messages SET content='see abc/api_abc.png' WHERE id=47")
    assert session_ingress_media._history_mentions(conn, refs) == {'/m/abc/api_abc.png'}

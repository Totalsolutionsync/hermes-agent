import datetime as dt
from types import SimpleNamespace

import pytest

from agent import provider_spend_cap as cap


@pytest.fixture
def venice(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg = {"daily_spend_cap_usd": 3.0, "pricing": {"input_per_mtok": 1.0, "output_per_mtok": 4.0}}
    monkeypatch.setattr(cap, "_provider_cfg", lambda p: cfg if cap._provider_key(p) == "venice" else {})
    return cfg


def test_no_cap_means_no_tracking_and_no_block(venice):
    cap.record("openai", 99.0)
    assert cap.spent_today_usd("openai") == 0.0 and cap.cap_reached_message("openai") is None


def test_records_and_blocks_at_cap(venice):
    cap.record("venice", 1.25)
    assert cap.cap_reached_message("venice") is None
    cap.record("custom:venice", 1.75)
    msg = cap.cap_reached_message("venice")
    assert "$3.00 of $3.00" in msg and "No request was sent" in msg


def test_cap_resets_next_day(venice):
    yesterday = dt.date.today() - dt.timedelta(days=1)
    cap.record("venice", 5.0, today=yesterday)
    assert cap.spent_today_usd("venice") == 0.0 and cap.cap_reached_message("venice") is None


def test_price_from_config(venice):
    usage = SimpleNamespace(input_tokens=1_000_000, output_tokens=250_000, reasoning_tokens=0)
    assert cap.price_usd("venice", usage) == pytest.approx(2.0)
    assert cap.price_usd("openai", usage) is None


def test_preflight_gate_refuses_when_capped(venice, monkeypatch):
    from agent.turn_preflight_gate import run_preflight_gate
    cap.record("venice", 3.0)
    agent = SimpleNamespace(provider="venice", _api_call_count=3, iteration_budget=SimpleNamespace(refund=lambda: None),
                            _emit_diagnostic_status=lambda m: None)
    msgs = []
    v = run_preflight_gate(
        agent, request_pressure_tokens=10, _moa_prepared_request=None, pending_moa_prepared_request=None,
        messages=msgs, system_message="", user_message="hi", active_system_prompt="", conversation_history=[],
        api_call_count=3, compression_attempts=0, max_compression_attempts=3, effective_task_id=None,
        final_response=None, failed=False, _turn_exit_reason=None, _compression_timeout_exhausted=False,
        _preflight_compression_blocked=False, _provider_overflow_recovery_pending=False, _last_preflight_pressure=None)
    assert v.action == "break" and v.failed and v._turn_exit_reason == "provider_daily_spend_cap"
    assert "Daily spend cap reached" in msgs[-1]["content"]

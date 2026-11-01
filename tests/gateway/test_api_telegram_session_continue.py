"""An API turn continues a stored Telegram DM session only for its authorized owner, behind an explicit switch."""
import json

import pytest

from gateway.config import Platform
from gateway.session import SessionSource
from gateway.session_api import bind_api_session
from hermes_state_runtime import RuntimeStoreError

OWNER = '111'


def _telegram_row(owner, session_id, user_id, chat_type='dm'):
    source = SessionSource(platform=Platform.TELEGRAM, chat_id=user_id, user_id=user_id, chat_type=chat_type)
    owner.db.create_session(session_id, source='telegram', user_id=user_id)
    owner.db._execute_write(lambda conn: conn.execute(
        'UPDATE sessions SET origin_json=?, chat_id=?, session_key=? WHERE id=?',
        (json.dumps(source.to_dict()), user_id, f'agent:main:telegram:{chat_type}:{user_id}', session_id)))


@pytest.fixture
def telegram_owner(owner, monkeypatch):
    owner.runner._is_user_authorized = lambda source: source.user_id == OWNER
    return owner


def _switch(monkeypatch, value):
    config = {'gateway': {'api_server': {'allow_telegram_session_continue': value}}}
    monkeypatch.setattr('hermes_cli.config.load_config', lambda: config)


def test_owner_continues_their_telegram_session_with_the_switch_on(telegram_owner, monkeypatch):
    _switch(monkeypatch, True)
    _telegram_row(telegram_owner, 'tg', OWNER)

    ref = bind_api_session(telegram_owner, 'tg')

    assert ref.session_id == 'tg'
    row = telegram_owner.db.get_session('tg')
    # The Telegram row keeps its own identity and routing.
    assert (row['source'], row['user_id'], row['session_key']) == ('telegram', OWNER, f'agent:main:telegram:dm:{OWNER}')


@pytest.mark.parametrize('value', [False, 'true', None])
def test_switch_off_keeps_the_refusal(telegram_owner, monkeypatch, value):
    _switch(monkeypatch, value)
    _telegram_row(telegram_owner, 'tg', OWNER)

    with pytest.raises(RuntimeStoreError, match='permission_denied'):
        bind_api_session(telegram_owner, 'tg')


@pytest.mark.parametrize('user_id,chat_type', [('222', 'dm'), (OWNER, 'group')])
def test_other_users_and_groups_stay_refused(telegram_owner, monkeypatch, user_id, chat_type):
    _switch(monkeypatch, True)
    _telegram_row(telegram_owner, 'tg', user_id, chat_type)

    with pytest.raises(RuntimeStoreError, match='permission_denied'):
        bind_api_session(telegram_owner, 'tg')


def test_admission_refuses_a_foreign_session_before_binding_unless_allowed(telegram_owner, monkeypatch):
    from gateway.session_api_turn import admit_api_steps
    _telegram_row(telegram_owner, 'tg', '222')
    _switch(monkeypatch, True)
    adapter = type('A', (), {'gateway_runner': telegram_owner.runner, '_model_routes': {},
                             '_ensure_session_db': lambda self: telegram_owner.db})()

    steps = admit_api_steps(adapter, session_id='tg', user_message='hi', conversation_history=[])
    with pytest.raises(RuntimeStoreError, match='permission_denied'):
        next(steps)


def test_non_telegram_sessions_are_unchanged(telegram_owner, monkeypatch):
    _switch(monkeypatch, True)
    telegram_owner.db.create_session('cli-s', source='cli')
    with pytest.raises(RuntimeStoreError, match='permission_denied'):
        bind_api_session(telegram_owner, 'cli-s')
    ref = bind_api_session(telegram_owner, 'api-s')
    assert telegram_owner.db.get_session(ref.session_id)['source'] == 'api_server'

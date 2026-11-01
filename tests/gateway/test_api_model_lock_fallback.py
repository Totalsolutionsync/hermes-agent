"""A confirmed Browser model lock on a canonical API turn never falls back (main parity).

One ordinary daemon with the API server: the locked primary answers 429 ``insufficient_quota``
and ``fallback_providers`` names a healthy backup. Main builds the locked agent with
``fallback_model=None`` and fails a turn served by another runtime; the shared owner must too.
"""
import asyncio
import json
import os
from pathlib import Path
import socket

import aiohttp
import pytest

from tests.gateway.fixtures.local_recovery_probe import child_env, daemon
from tests.gateway.test_managed_worker_construct import Backup, Primary, _serve


@pytest.mark.platforms("linux")
def test_locked_session_chat_does_not_fall_back_to_the_profile_chain(tmp_path):
    root = Path(__file__).resolve().parents[2]
    home, user = tmp_path / 'state', tmp_path / 'user'
    home.mkdir(mode=0o700)
    user.mkdir()
    primary, backup = _serve(Primary), _serve(Backup)
    primary.refuse = True
    purl, burl = (f'http://127.0.0.1:{s.server_port}/v1' for s in (primary, backup))
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        api_port = sock.getsockname()[1]
    config = {'model': {'provider': 'custom', 'default': 'primary-model', 'base_url': purl},
              'gateway': {'multiplex_profiles': False,
                          'platforms': {'api_server': {'enabled': True, 'port': api_port, 'host': '127.0.0.1'}}},
              'auxiliary': {'title_generation': {'enabled': False}}, 'terminal': {'cwd': str(home)},
              'platform_toolsets': {'api_server': []},
              'fallback_providers': [{'provider': 'custom', 'model': 'backup-model', 'base_url': burl,
                                      'api_key': 'loopback-only'}]}
    (home / 'config.yaml').write_text(json.dumps(config))
    env = {**child_env(), **{k: os.environ[k] for k in ('TIRITH_ENABLED',) if k in os.environ}}
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home), PYTHONPATH=str(root),
               OPENAI_API_KEY='loopback-only', OPENAI_BASE_URL=purl, API_SERVER_KEY='lock-owned-secret',
               API_SERVER_ENABLED='true', API_SERVER_PORT=str(api_port), PYTHONUNBUFFERED='1')

    async def exercise():
        headers = {'Authorization': 'Bearer lock-owned-secret'}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as client:
            base = f'http://127.0.0.1:{api_port}/api/sessions'
            async with client.post(base, json={'id': 'locked', 'title': 'Locked'}, headers=headers) as response:
                assert response.status in (200, 201), await response.text()
            async with client.post(f'{base}/locked/chat', headers=headers, json={
                    'message': 'NEED_FALLBACK', 'provider': 'custom', 'model': 'primary-model',
                    'require_model_lock': True}) as response:
                return response.status, await response.text()
    try:
        with daemon(root, home, env, barrier=False):
            status, text = asyncio.run(exercise())
    finally:
        for server in (primary, backup):
            server.shutdown()
            server.server_close()
    assert primary.requests, (status, text)
    assert backup.requests == [], 'a confirmed model lock was served by the profile fallback chain'
    body = json.loads(text)
    assert body['status'] == 'failed' and 'BACKUP_OK' not in text, body
    assert body['usage']['runtime']['model_lock'] == 'confirmed', body


def test_locked_turn_served_by_another_runtime_settles_failed(api):
    """Main's settlement refusal: a locked turn whose agent ran on another provider/model is a
    failed turn carrying the mismatch, never a completed answer from the wrong runtime."""
    from types import SimpleNamespace
    from gateway.run_turn_runner import _settle_api_runtime_lock
    from gateway.session_api_turn import api_execution

    agent = SimpleNamespace(provider='custom', model='backup-model', _hermes_api_runtime={'provider': 'custom'})
    result, captured = {'final_response': 'BACKUP_OK', 'completed': True}, {'usage': {}}
    token = api_execution.set({'adapter': api, 'settings': {
        'confirmed_runtime_lock': True, 'route': {'provider': 'custom', 'model': 'primary-model'},
        'route_source': 'raw_request'}})
    try:
        _settle_api_runtime_lock(agent, result, captured)
    finally:
        api_execution.reset(token)
    assert result['failed'] is True and result['completed'] is False and result['final_response'] == ''
    assert 'confirmed model lock runtime mismatch' in result['error']


def _settle(api, agent, settings):
    from gateway.run_turn_runner import _settle_api_runtime_lock
    from gateway.session_api_turn import api_execution

    result, captured = {'final_response': 'OK', 'completed': True}, {'usage': {}}
    token = api_execution.set({'adapter': api, 'settings': settings})
    try:
        _settle_api_runtime_lock(agent, result, captured)
    finally:
        api_execution.reset(token)
    return result, captured


def test_unlocked_requested_turn_reports_the_served_runtime(api):
    """A per-request provider/model (no lock) still reports which runtime answered, so a
    ``/v1/runs`` caller can show the served model instead of an empty ``runtime``."""
    from types import SimpleNamespace

    agent = SimpleNamespace(provider='custom', model='qwen-3-6-plus', _hermes_api_runtime={'provider': 'custom'})
    result, captured = _settle(api, agent, {
        'requested_runtime': {'provider': 'venice', 'model': 'qwen-3-6-plus'}, 'route_source': 'raw_request'})
    runtime = captured['usage']['runtime']
    assert result['final_response'] == 'OK' and result['completed'] is True
    assert runtime['model'] == 'qwen-3-6-plus' and runtime['provider'] == 'custom'
    assert runtime['requested'] == {'provider': 'venice', 'model': 'qwen-3-6-plus'}
    assert runtime.get('model_lock', '') == ''


def test_unlocked_mismatch_never_fails_the_turn(api):
    """Only a confirmed lock refuses a turn served elsewhere; an unlocked request's fallback
    answer stands and simply reports the runtime that served it."""
    from types import SimpleNamespace

    agent = SimpleNamespace(provider='anthropic', model='claude-opus-5-5', _hermes_api_runtime={})
    result, captured = _settle(api, agent, {
        'requested_runtime': {'provider': 'venice', 'model': 'qwen-3-6-plus'}, 'route_source': 'raw_request'})
    assert result['completed'] is True and result['final_response'] == 'OK'
    assert captured['usage']['runtime']['model'] == 'claude-opus-5-5'


def test_global_turn_without_a_route_attaches_nothing(api):
    from types import SimpleNamespace

    agent = SimpleNamespace(provider='anthropic', model='claude-opus-5-5', _hermes_api_runtime={})
    _, captured = _settle(api, agent, {'route_source': 'global'})
    assert 'runtime' not in captured['usage']


def test_runs_runtime_contract_kynver_reads(api):
    """Cross-repo contract (Kynver agent tabs model pin, Kynver PR #4895): the box reads
    ``runtime.provider`` / ``runtime.model`` (top-level strings) off ``run.completed`` and
    ``GET /v1/runs/{id}`` to say which model answered a pinned turn, and treats an empty
    ``model`` as "Hermes did not say". Pin that shape through the same sanitizer the
    ``/v1/runs`` handler applies before ``_finish`` publishes it."""
    from types import SimpleNamespace

    requested = {'provider': 'venice', 'model': 'qwen-3-6-plus'}
    agent = SimpleNamespace(provider='custom', model='qwen-3-6-plus', _hermes_api_runtime={'provider': 'custom'})
    _, captured = _settle(api, agent, {'requested_runtime': requested, 'route_source': 'raw_request'})
    wire = api._sanitize_runtime_metadata(runtime=captured['usage']['runtime'], requested_runtime=requested)
    assert wire == {'provider': 'custom', 'model': 'qwen-3-6-plus', 'route_source': 'raw_request',
                    'requested': {'provider': 'venice', 'model': 'qwen-3-6-plus'}}

    # A global (unrouted) turn reports no served pair: the model id is empty, never invented.
    agent = SimpleNamespace(provider='anthropic', model='claude-opus-5-5', _hermes_api_runtime={})
    _, captured = _settle(api, agent, {'route_source': 'global'})
    assert api._sanitize_runtime_metadata(runtime=captured['usage'].get('runtime'))['model'] == ''

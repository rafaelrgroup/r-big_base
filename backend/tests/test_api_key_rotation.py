"""Rotation, retry and revocation tests with synthetic credentials only."""
from datetime import datetime, timedelta, timezone
import json
import time
from types import SimpleNamespace

import pyotp
import pytest
from fastapi.testclient import TestClient

from bigbase.api import create_app
from bigbase import security as security_module
from bigbase.security import hashed


PASSWORD = 'synthetic-rotation-password-123'


@pytest.fixture
def env(tmp_path, monkeypatch):
    clock = [datetime.now(timezone.utc)]
    monkeypatch.setattr(security_module, 'utcnow', lambda: clock[0])
    monkeypatch.setattr(security_module, 'time', SimpleNamespace(time=lambda: clock[0].timestamp(), monotonic=time.monotonic))
    app = create_app(tmp_path, testing=True)
    with app.state.store.transaction() as c:
        admin = app.state.security.create_user(c, 'synthetic-rotation-admin', PASSWORD, 'admin')
        owner = app.state.security.create_user(c, 'synthetic-rotation-owner', PASSWORD, permissions=['read', 'enrich'])
        secrets = {u['id']: app.state.security.secret(u) for u in (admin, owner)}
        token = 'bb_synthetic-parent-token'
        key = {'id': hashed(token), 'public_id': 'synthetic-parent-id', 'user_id': owner['id'], 'name': 'Synthetic integration',
               'scopes': ['read', 'enrich'], 'sources': ['manual'], 'active': True, 'created_at': clock[0].isoformat(),
               'expires_at': (clock[0]+timedelta(days=30)).isoformat()}
        app.state.store.put(c, 'api_key', key)
    with TestClient(app) as client:
        context = SimpleNamespace(client=client, app=app, admin=admin, owner=owner, secrets=secrets, clock=clock, key=key, token=token)
        login(context, client, owner)
        yield context


def login(env, client, user):
    response = client.post('/api/v1/auth/login', json={'username': user['username'], 'password': PASSWORD})
    assert response.status_code == 200
    response = client.post('/api/v1/auth/otp', json={'challenge': response.json()['challenge'],
                                                  'code': pyotp.TOTP(env.secrets[user['id']]).at(env.clock[0].timestamp())})
    assert response.status_code == 200, response.text
    client.headers['X-CSRF-Token'] = response.json()['csrf']
    return response


def code(env, user=None):
    env.clock[0] += timedelta(seconds=30)
    return pyotp.TOTP(env.secrets[(user or env.owner)['id']]).at(env.clock[0].timestamp())


def rotate(env, *, key_id=None, grace=900, idem='synthetic-operation', otp=True, client=None, user=None):
    body = {'grace_seconds': grace}
    if otp is True:
        body['otp'] = code(env, user)
    elif isinstance(otp, str):
        body['otp'] = otp
    return (client or env.client).post('/api/v1/admin/api-keys/'+(key_id or env.key['public_id'])+'/rotate',
                                       json=body, headers={'Idempotency-Key': idem})


def key_rows(env):
    with env.app.state.store.transaction() as c:
        return env.app.state.store.all(c, 'api_key')


def lookup(env, public_id):
    return next(key for key in key_rows(env) if key['public_id'] == public_id)


def consume(env, token):
    client = TestClient(env.app)
    try:
        return client.get('/api/v1/stats', headers={'X-API-Key': token})
    finally:
        client.close()


@pytest.mark.parametrize('grace', [0, 1, 900, 86400])
def test_old_and_new_transition_with_exact_expiry_boundary(env, grace):
    assert consume(env, env.token).status_code == 200
    response = rotate(env, grace=grace)
    assert response.status_code == 201, response.text
    result = response.json()
    parent, child = lookup(env, env.key['public_id']), lookup(env, result['id'])
    assert child['scopes'] == env.key['scopes']
    assert child['sources'] == env.key['sources']
    assert child['user_id'] == env.owner['id']
    assert child['expires_at'] == env.key['expires_at']
    assert child['predecessor_id'] == parent['public_id']
    assert parent['successor_id'] == child['public_id']
    assert child['rotation_root_id'] == parent['rotation_root_id'] == parent['public_id']
    assert parent['original_expires_at'] == env.key['expires_at']
    assert result['key'] != env.token
    assert consume(env, result['key']).status_code == 200
    assert consume(env, env.token).status_code == (200 if grace else 401)
    env.clock[0] = datetime.fromisoformat(result['predecessor_valid_until'])
    assert consume(env, env.token).status_code == 401
    assert consume(env, result['key']).status_code == 200


def test_grace_never_extends_parent_or_child_expiry(env):
    expiry = env.clock[0] + timedelta(seconds=60)
    with env.app.state.store.transaction() as c:
        parent = env.app.state.store.get(c, 'api_key', env.key['id'])
        parent['expires_at'] = expiry.isoformat()
        env.app.state.store.put(c, 'api_key', parent)
    result = rotate(env, grace=86400).json()
    assert result['expires_at'] == result['predecessor_valid_until'] == expiry.isoformat()
    env.clock[0] = expiry
    assert consume(env, env.token).status_code == 401
    assert consume(env, result['key']).status_code == 401


def test_network_retry_recovers_same_secret_without_otp_once_committed(env):
    result = rotate(env, grace=0).json()
    assert consume(env, env.token).status_code == 401
    retried = rotate(env, grace=0, otp=False)
    assert retried.status_code == 201
    assert retried.json() == result
    assert len(key_rows(env)) == 2
    with env.app.state.store.transaction() as c:
        events = [json.loads(row[0]) for row in c.execute('SELECT body FROM events')]
        assert len([event for event in events if event['action'] == 'rotate_key']) == 1
        receipt = env.app.state.store.all(c, 'key_rotation_receipt')[0]
        serialized = json.dumps(env.app.state.store.all(c, 'api_key') + [receipt] + events)
        assert result['key'] not in serialized and env.token not in serialized
        assert receipt['response_ciphertext']
        assert not any(name in receipt for name in ('otp', 'key', 'response', 'token'))


def test_retry_parameters_cannot_change_or_create_another_child(env):
    assert rotate(env).status_code == 201
    changed = rotate(env, grace=0, otp=False)
    assert changed.status_code == 409 and changed.json()['detail']['code'] == 'IDEMPOTENCY_CONFLICT'
    other = rotate(env, idem='different')
    assert other.status_code == 409 and other.json()['detail']['code'] == 'KEY_ALREADY_ROTATED'
    assert len(key_rows(env)) == 2


def test_receipt_is_bound_to_original_session(env):
    result = rotate(env).json()
    other = TestClient(env.app)
    try:
        env.clock[0] += timedelta(seconds=30)
        login(env, other, env.owner)
        response = rotate(env, client=other, otp=False)
        assert response.status_code == 409
        assert result['key'] not in response.text
    finally:
        other.close()


def test_receipt_expiry_erases_ciphertext_and_preserves_chain_metadata(env):
    result = rotate(env).json()
    env.clock[0] += timedelta(seconds=300)
    response = rotate(env, otp=False)
    assert response.status_code == 409 and response.json()['detail']['code'] == 'ROTATION_RESPONSE_EXPIRED'
    assert response.json()['detail']['successor_id'] == result['id']
    env.app.state.security.cleanup_rotation_receipts()
    with env.app.state.store.transaction() as c:
        receipt = env.app.state.store.all(c, 'key_rotation_receipt')[0]
        assert receipt['response_ciphertext'] is None
        assert receipt['successor_id'] == result['id'] and receipt['secret_erased_at']
    assert consume(env, result['key']).status_code == 200


def test_login_or_rotation_totp_cannot_be_replayed_for_new_child(env):
    reused = pyotp.TOTP(env.secrets[env.owner['id']]).at(env.clock[0].timestamp())
    assert rotate(env, otp=reused).status_code == 401
    result = rotate(env).json()
    reused = pyotp.TOTP(env.secrets[env.owner['id']]).at(env.clock[0].timestamp())
    assert rotate(env, key_id=result['id'], idem='next', otp=reused).status_code == 401


@pytest.mark.parametrize('member', ['parent', 'child', 'grandchild'])
def test_revoking_any_member_revokes_whole_chain_and_erases_receipts(env, member):
    child = rotate(env).json()
    grandchild = rotate(env, key_id=child['id'], idem='grandchild').json()
    selected = {'parent': env.key['public_id'], 'child': child['id'], 'grandchild': grandchild['id']}[member]
    response = env.client.patch('/api/v1/admin/api-keys/'+selected)
    assert response.status_code == 200
    assert len(response.json()['revoked_ids']) == 3
    for token in (env.token, child['key'], grandchild['key']):
        assert consume(env, token).status_code == 401
    assert all(not key['active'] and key['revoked_at'] for key in key_rows(env))
    assert rotate(env, otp=False).status_code == 409
    with env.app.state.store.transaction() as c:
        assert all(receipt['response_ciphertext'] is None for receipt in env.app.state.store.all(c, 'key_rotation_receipt'))


def test_disable_and_reenable_user_does_not_reactivate_rotated_keys(env):
    result = rotate(env).json()
    with env.app.state.store.transaction() as c:
        env.app.state.security.revoke_access(c, env.owner['id'])
        user = env.app.state.store.get(c, 'user', env.owner['id'])
        user['active'] = True
        env.app.state.store.put(c, 'user', user)
    assert consume(env, env.token).status_code == 401
    assert consume(env, result['key']).status_code == 401
    assert rotate(env, otp=False).status_code == 401


def test_admin_can_rotate_for_owner_using_admin_totp_without_changing_owner(env):
    admin_client = TestClient(env.app)
    try:
        login(env, admin_client, env.admin)
        response = rotate(env, client=admin_client, user=env.admin)
        assert response.status_code == 201, response.text
        assert lookup(env, response.json()['id'])['user_id'] == env.owner['id']
    finally:
        admin_client.close()


def test_other_normal_user_cannot_list_rotate_or_revoke_key(env):
    with env.app.state.store.transaction() as c:
        other = env.app.state.security.create_user(c, 'synthetic-other-owner', PASSWORD)
        env.secrets[other['id']] = env.app.state.security.secret(other)
    client = TestClient(env.app)
    try:
        login(env, client, other)
        assert client.get('/api/v1/admin/api-keys').json()['items'] == []
        assert rotate(env, client=client, user=other).status_code == 404
        assert client.patch('/api/v1/admin/api-keys/'+env.key['public_id']).status_code == 404
    finally:
        client.close()


def test_api_key_even_with_human_cookie_cannot_manage_rotation(env):
    env.client.headers['X-API-Key'] = env.token
    for response in (rotate(env), env.client.patch('/api/v1/admin/api-keys/'+env.key['public_id']), env.client.get('/api/v1/admin/api-keys')):
        assert response.status_code == 403
        assert response.json()['detail']['code'] == 'HUMAN_SESSION_REQUIRED'


def test_csrf_required_for_rotation_retry_and_revoke(env):
    assert rotate(env).status_code == 201
    env.client.headers.pop('X-CSRF-Token')
    assert rotate(env, otp=False).status_code == 403
    assert env.client.patch('/api/v1/admin/api-keys/'+env.key['public_id']).status_code == 403


@pytest.mark.parametrize('grace', [-1, 86401, 0.0, True, '900', None, [], {}])
def test_invalid_grace_is_rejected_without_consuming_otp_or_creating_child(env, grace):
    before = lookup(env, env.key['public_id'])
    response = rotate(env, grace=grace)
    assert response.status_code == 422
    assert key_rows(env) == [before]


@pytest.mark.parametrize('extra', [{'scopes': ['admin']}, {'sources': ['other']}, {'expires_at': '2099-01-01T00:00:00Z'}, {'user_id': 'other'}])
def test_rotation_cannot_expand_source_scope_expiry_or_owner(env, extra):
    body = {'otp': code(env), **extra}
    response = env.client.post('/api/v1/admin/api-keys/'+env.key['public_id']+'/rotate', json=body, headers={'Idempotency-Key': 'test'})
    assert response.status_code == 422
    assert len(key_rows(env)) == 1


def test_missing_idempotency_key_or_new_otp_cannot_create_rotation(env):
    response = env.client.post('/api/v1/admin/api-keys/'+env.key['public_id']+'/rotate', json={'otp': code(env)})
    assert response.status_code == 422 and response.json()['detail']['code'] == 'IDEMPOTENCY_KEY_REQUIRED'
    response = rotate(env, otp=False)
    assert response.status_code == 422 and response.json()['detail']['code'] == 'NEW_TOTP_REQUIRED'
    assert len(key_rows(env)) == 1


def test_list_never_returns_old_new_hash_cipher_or_secret(env):
    result = rotate(env).json()
    rows = env.client.get('/api/v1/admin/api-keys').json()['items']
    assert len(rows) == 2
    encoded = json.dumps(rows)
    for value in (env.token, result['key'], env.key['id'], hashed(result['key'])):
        assert value not in encoded
    assert not any('response_ciphertext' in row or 'key' in row for row in rows)
    assert next(row for row in rows if row['id'] == env.key['public_id'])['status'] == 'transition'


def test_original_worker_uses_shortened_parent_expiry(env, monkeypatch):
    from bigbase import imports as imports_module
    result = rotate(env, grace=900).json()
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return env.clock[0] if tz else env.clock[0].replace(tzinfo=None)
    monkeypatch.setattr(imports_module, 'datetime', Clock)
    job = {'owner_id': env.owner['id'], 'api_key_id': env.key['id']}
    with env.app.state.store.transaction() as c:
        assert env.app.state.imports._principal(c, job)[1]['id'] == env.key['id']
    env.clock[0] = datetime.fromisoformat(result['predecessor_valid_until'])
    with env.app.state.store.transaction() as c:
        with pytest.raises(imports_module._PrincipalUnavailable) as failure:
            env.app.state.imports._principal(c, job)
        assert failure.value.error['code'] == 'api_key_expired'


def test_concurrent_retries_commit_one_child_and_one_audit_event(env):
    from concurrent.futures import ThreadPoolExecutor
    body = {'otp': code(env), 'grace_seconds': 900}
    cookies = dict(env.client.cookies)
    csrf = env.client.headers['X-CSRF-Token']
    def send():
        client = TestClient(env.app)
        try:
            client.cookies.update(cookies)
            return client.post('/api/v1/admin/api-keys/'+env.key['public_id']+'/rotate', json=body,
                               headers={'X-CSRF-Token': csrf, 'Idempotency-Key': 'concurrent'} )
        finally:
            client.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: send(), range(2)))
    assert all(response.status_code == 201 for response in responses)
    assert responses[0].json() == responses[1].json()
    assert len(key_rows(env)) == 2
    with env.app.state.store.transaction() as c:
        events = [json.loads(row[0]) for row in c.execute('SELECT body FROM events')]
        assert len([event for event in events if event['action'] == 'rotate_key']) == 1

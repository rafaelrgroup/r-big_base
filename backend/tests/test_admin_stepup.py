"""Synthetic accounts and sessions only; no principal deployment is opened."""
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


PASSWORD = "synthetic-stepup-password-123"


@pytest.fixture
def env(tmp_path, monkeypatch):
    # Exercise real TOTP codes and replay counters without a blocking wall wait.
    clock = [int(time.time()) // 30 * 30]
    monkeypatch.setattr(security_module, "time", SimpleNamespace(time=lambda: clock[0], monotonic=time.monotonic))
    app = create_app(tmp_path, testing=True)
    with app.state.store.transaction() as tx:
        admin = app.state.security.create_user(tx, "synthetic-stepup-admin", PASSWORD, "admin")
        target = app.state.security.create_user(tx, "synthetic-stepup-target", PASSWORD)
        secret = app.state.security.secret(admin)
    with TestClient(app) as client:
        result = login(client, admin["username"], secret, clock[0])
        yield SimpleNamespace(client=client, app=app, admin=admin, target=target, secret=secret, clock=clock, login=result)


def login(client, username, secret, at):
    challenge = client.post("/api/v1/auth/login", json={"username": username, "password": PASSWORD})
    assert challenge.status_code == 200
    result = client.post("/api/v1/auth/otp", json={"challenge": challenge.json()["challenge"], "code": pyotp.TOTP(secret).at(at)})
    assert result.status_code == 200, result.text
    client.headers["X-CSRF-Token"] = result.json()["csrf"]
    return result.json()


def session_edit(env, client=None, **changes):
    client = client or env.client
    with env.app.state.store.transaction() as tx:
        session = env.app.state.store.get(tx, "session", hashed(client.cookies["bigbase_session"]))
        session.update(changes)
        env.app.state.store.put(tx, "session", session)


def expire_grant(env, client=None):
    session_edit(env, client, totp_verified_at=(datetime.now(timezone.utc)-timedelta(minutes=6)).isoformat())


def confirm(env, client=None, code=None):
    client = client or env.client
    if code is None:
        env.clock[0] += 30
        code = pyotp.TOTP(env.secret).at(env.clock[0])
    return client.post("/api/v1/auth/step-up", json={"code": code})


def action(env, kind, client=None):
    client = client or env.client
    if kind == "invite":
        return client.post("/api/v1/admin/invitations", json={"username": "synthetic-new-invite"})
    if kind == "create":
        return client.post("/api/v1/admin/users", json={"username": "synthetic-direct-user", "password": PASSWORD})
    return client.patch("/api/v1/admin/users/" + env.target["id"], json={"active": kind == "enable"})


@pytest.mark.parametrize("kind", ["invite", "create", "disable", "enable"])
def test_recent_login_totp_satisfies_grant_in_its_session(env, kind):
    response = action(env, kind)
    assert response.status_code in {200, 201}, response.text


@pytest.mark.parametrize("kind", ["invite", "create", "disable", "enable"])
def test_expired_grant_blocks_before_user_or_audit_mutation(env, kind):
    expire_grant(env)
    with env.app.state.store.transaction() as tx:
        users = env.app.state.store.all(tx, "user")
        events = list(tx.execute("SELECT body FROM events"))
    response = action(env, kind)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "RECENT_TOTP_REQUIRED"
    with env.app.state.store.transaction() as tx:
        assert env.app.state.store.all(tx, "user") == users
        assert list(tx.execute("SELECT body FROM events")) == events
    assert confirm(env).status_code == 200
    assert action(env, kind).status_code in {200, 201}


@pytest.mark.parametrize("age,allowed", [(299, True), (300, True), (300.001, False), (-0.001, False)])
def test_five_minute_boundary_and_future_dates(env, monkeypatch, age, allowed):
    fixed = datetime.now(timezone.utc)
    monkeypatch.setattr(security_module, "utcnow", lambda: fixed)
    session_edit(env, totp_verified_at=(fixed-timedelta(seconds=age)).isoformat())
    response = action(env, "invite")
    assert (response.status_code == 201) is allowed


@pytest.mark.parametrize("bad", [None, True, 1, {}, [], "not-a-date", "2026-09-08T13:00:00"])
def test_legacy_or_invalid_session_grant_requires_confirmation(env, bad):
    session_edit(env, totp_verified_at=bad)
    assert action(env, "invite").json()["detail"]["code"] == "RECENT_TOTP_REQUIRED"


def test_recent_grant_does_not_slide_with_use(env):
    with env.app.state.store.transaction() as tx:
        original = env.app.state.store.get(tx, "session", hashed(env.client.cookies["bigbase_session"]))["totp_verified_at"]
    assert action(env, "invite").status_code == 201
    assert env.client.get("/api/v1/admin/users").status_code == 200
    with env.app.state.store.transaction() as tx:
        assert env.app.state.store.get(tx, "session", hashed(env.client.cookies["bigbase_session"]))["totp_verified_at"] == original


def test_login_code_and_replayed_stepup_cannot_refresh_grant(env):
    expire_grant(env)
    code = pyotp.TOTP(env.secret).at(env.clock[0])
    response = confirm(env, code=code)
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "TOTP_INVALID_OR_REPLAYED"
    assert action(env, "invite").status_code == 403
    response = confirm(env)
    assert response.status_code == 200
    assert response.json()["max_age_seconds"] == 300
    repeated = confirm(env, code=pyotp.TOTP(env.secret).at(env.clock[0]))
    assert repeated.status_code == 401


def test_stepup_is_bound_to_session_and_replay_counter_spans_sessions(env):
    other = TestClient(env.app)
    try:
        env.clock[0] += 30
        login(other, env.admin["username"], env.secret, env.clock[0])
        expire_grant(env)
        expire_grant(env, other)
        assert confirm(env).status_code == 200
        assert action(env, "create").status_code == 200
        assert action(env, "invite", other).status_code == 403
        replay = pyotp.TOTP(env.secret).at(env.clock[0])
        assert confirm(env, other, replay).status_code == 401
        assert confirm(env, other).status_code == 200
        assert action(env, "invite", other).status_code == 201
    finally:
        other.close()


def test_api_key_cannot_substitute_human_totp_even_with_cookie_and_code(env):
    token = "synthetic-admin-key-never-production"
    with env.app.state.store.transaction() as tx:
        env.app.state.store.put(tx, "api_key", {"id": hashed(token), "public_id": "synthetic-key", "user_id": env.admin["id"],
            "active": True, "expires_at": (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(), "scopes": ["admin"], "sources": ["manual"]})
    env.client.headers["X-API-Key"] = token
    assert action(env, "invite").json()["detail"]["code"] == "HUMAN_SESSION_REQUIRED"
    assert confirm(env).json()["detail"]["code"] == "HUMAN_SESSION_REQUIRED"
    env.client.cookies.clear()
    assert action(env, "create").json()["detail"]["code"] == "HUMAN_SESSION_REQUIRED"


def test_csrf_required_for_stepup_and_every_protected_write(env):
    env.client.headers.pop("X-CSRF-Token")
    assert confirm(env).status_code == 403
    for kind in ("invite", "create", "enable", "disable"):
        assert action(env, kind).status_code == 403


def test_normal_user_cannot_confirm_or_administer(env):
    with env.app.state.store.transaction() as tx:
        secret = env.app.state.security.secret(env.target)
    other = TestClient(env.app)
    try:
        login(other, env.target["username"], secret, env.clock[0])
        assert other.post("/api/v1/auth/step-up", json={"code": pyotp.TOTP(secret).at(env.clock[0])}).status_code == 403
        for kind in ("invite", "create", "enable", "disable"):
            assert action(env, kind, other).status_code == 403
    finally:
        other.close()


def test_revoked_session_cannot_be_restored_by_totp(env):
    with env.app.state.store.transaction() as tx:
        env.app.state.security.revoke_access(tx, env.admin["id"])
    assert confirm(env).status_code == 401
    assert action(env, "invite").status_code == 401


def test_disabled_user_cannot_use_recent_totp(env):
    with env.app.state.store.transaction() as tx:
        user = env.app.state.store.get(tx, "user", env.admin["id"])
        user["active"] = False
        env.app.state.store.put(tx, "user", user)
    assert confirm(env).status_code == 401
    assert action(env, "invite").status_code == 401


def test_recent_totp_does_not_extend_session_idle_or_absolute_lifetime(env):
    session_edit(env, last_seen=(datetime.now(timezone.utc)-timedelta(minutes=31)).isoformat())
    assert action(env, "invite").status_code == 401
    assert confirm(env).status_code == 401
    session_edit(env, last_seen=datetime.now(timezone.utc).isoformat(), created_at=(datetime.now(timezone.utc)-timedelta(hours=13)).isoformat())
    assert action(env, "invite").status_code == 401


def test_audit_and_stored_grant_contain_no_code_or_secret(env):
    assert confirm(env).status_code == 200
    with env.app.state.store.transaction() as tx:
        events = [json.loads(row[0]) for row in tx.execute("SELECT body FROM events")]
        assert [row["action"] for row in events] == ["login", "admin_step_up"]
        session = env.app.state.store.get(tx, "session", hashed(env.client.cookies["bigbase_session"]))
        assert set(session) == {"id", "user_id", "csrf", "created_at", "last_seen", "totp_verified_at"}
        assert env.secret not in json.dumps(events + [session])
        assert all("code" not in row and "otp" not in row and "secret" not in row for row in events)


@pytest.mark.parametrize("body", [{"code": 123456}, {"code": "12345"}, {"code": "１２３４５６"}, {"code": "123456", "user_id": "other"}])
def test_stepup_body_is_strict_and_does_not_accept_other_user(env, body):
    assert env.client.post("/api/v1/auth/step-up", json=body).status_code == 422


def test_disable_reenable_revokes_target_sessions_and_keys(env):
    with env.app.state.store.transaction() as tx:
        secret = env.app.state.security.secret(env.target)
    other = TestClient(env.app)
    try:
        login(other, env.target["username"], secret, env.clock[0])
        token = "synthetic-target-key"
        with env.app.state.store.transaction() as tx:
            env.app.state.store.put(tx, "api_key", {"id": hashed(token), "public_id": "target-key", "user_id": env.target["id"],
                "active": True, "expires_at": (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(), "scopes": ["read"], "sources": ["manual"]})
        assert action(env, "disable").status_code == 200
        assert action(env, "enable").status_code == 200
        assert other.get("/api/v1/auth/me").status_code == 401
        with env.app.state.store.transaction() as tx:
            assert env.app.state.store.get(tx, "api_key", hashed(token))["active"] is False
    finally:
        other.close()

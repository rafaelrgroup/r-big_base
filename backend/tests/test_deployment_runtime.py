"""Deployment guardrails, using only synthetic credentials and isolated files."""
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import json
import os
import sqlite3

import psycopg
import pyotp
import pytest
from fastapi.testclient import TestClient

from bigbase.api import create_app
from bigbase.control_store import ControlStore
from bigbase.deployment import (DeploymentIdentity, DeploymentStore, DeploymentReads,
                                DeploymentRuntime, private_text)
from bigbase.security import Security, Limiter
from bigbase.store import Store


def identity(**changes):
    result=dict(deployment_id=str(uuid4()),environment='staging',database='bigbase_release_validation',
                schema='bigbase_canonical',port=15432)
    result.update(changes)
    return DeploymentIdentity(**result)


def setup_control(root):
    root.mkdir(mode=0o700)
    store=Store(root/'control.sqlite3')
    security=Security(store,root)
    with store.transaction() as c:
        user=security.create_user(c,'release-synthetic','synthetic-password-123','admin')
        secret=security.secret(user)
    (root/'control.sqlite3').chmod(0o600)
    return store,user,secret


class FakeRedis(Limiter):
    def __init__(self,*args,**kwargs):super().__init__()
    def close(self):pass


class FakeRepository(DeploymentStore):
    def __init__(self,wanted):
        self.identity=wanted
        self.dsn=f'host=127.0.0.1 port=15432 dbname={wanted.database} user=validation'
        self.schema=wanted.schema
    def deployment_info(self):
        return {**self.identity.__dict__,'server_address':'127.0.0.1','server_port':15432,'server_version_num':180006}
    def cleanup_read_cursors(self,**kwargs):return 0


@pytest.fixture
def deployed(tmp_path,monkeypatch):
    root=tmp_path/'control';old,user,secret=setup_control(root)
    wanted=identity();reads=DeploymentReads(FakeRepository(wanted),writes_enabled=True)
    config=DeploymentRuntime(wanted,root,'redis://127.0.0.1:16379/0',('panel.example','api.example'))
    monkeypatch.setenv('BIGBASE_ENV','staging');monkeypatch.delenv('BIGBASE_LOCAL_HTTP',raising=False)
    monkeypatch.setattr('bigbase.api.RedisLimiter',FakeRedis)
    app=create_app(canonical_reads=reads,deployment=config)
    with TestClient(app,base_url='https://panel.example') as client:
        yield client,app,secret,reads,config


def login(client,secret):
    first=client.post('/api/v1/auth/login',json={'username':'release-synthetic','password':'synthetic-password-123'})
    assert first.status_code==200,first.text
    assert first.json()['enrollment_required']
    assert client.get('/api/v1/auth/me').status_code==401
    result=client.post('/api/v1/auth/otp',json={'challenge':first.json()['challenge'],'code':pyotp.TOTP(secret).now()})
    assert result.status_code==200,result.text
    client.headers['X-CSRF-Token']=result.json()['csrf']
    return result


def test_health_honest_no_unproven_migration_percentage(deployed):
    c,app,secret,reads,config=deployed
    result=c.get('/api/v1/health').json()
    assert result['runtime']=='deployed' and result['environment']=='staging'
    assert result['canonical_connected'] and result['synthetic'] is False
    assert result['production_connected'] is False and result['local_person_data_enabled'] is False
    assert result['migration_complete'] is None and result['migration']['processed'] is None
    reads.repository.deployment_info=lambda:{'environment':'synthetic'}
    assert c.get('/api/v1/health').status_code==503


def test_secure_cookie_otp_csrf_and_allowed_host(deployed):
    c,app,secret,*_=deployed
    response=login(c,secret)
    cookie=response.headers['set-cookie'].lower()
    assert 'secure' in cookie and 'httponly' in cookie and 'samesite=strict' in cookie and 'domain=' not in cookie
    assert c.get('/api/v1/auth/me').status_code==200
    token=c.headers.pop('X-CSRF-Token')
    assert c.post('/api/v1/admin/sources',json={'id':'test','name':'Test'}).status_code==403
    c.headers['X-CSRF-Token']=token
    assert c.post('/api/v1/admin/sources',json={'id':'test','name':'Test'}).status_code==200
    assert c.get('/api/v1/health',headers={'Host':'attacker.example'}).status_code==400


@pytest.mark.parametrize('method,path',[
 ('post','/api/v1/people/enrich'),('post','/api/v1/companies/search'),
 ('get','/api/v1/people/arbitrary'),('post','/api/v1/imports'),
 ('post','/api/v1/bulk-queries'),('get','/api/v1/saved-searches'),
 ('post','/api/v1/admin/fields')])
def test_local_person_paths_never_write_or_fallback(deployed,method,path):
    c,app,secret,*_=deployed
    assert getattr(c,method)(path).status_code==401
    login(c,secret)
    result=getattr(c,method)(path)
    assert result.status_code==503,result.text
    assert result.json()['detail']['code']=='CANONICAL_OPERATION_REQUIRED'
    with app.state.store.transaction() as conn:
        assert not app.state.store.all(conn,'entity')
        with pytest.raises((sqlite3.DatabaseError,ValueError)):
            app.state.store.put(conn,'entity',{'id':'forbidden','name':'synthetic'})
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("INSERT INTO objects VALUES('entity','forbidden','{}')")


def test_canonical_status_real_environment_and_catalog_control(deployed):
    c,app,secret,*_=deployed;login(c,secret)
    value=c.get('/api/v1/canonical/status').json()
    assert value['runtime']=='deployed' and value['environment']=='staging'
    assert value['writes_enabled'] and value['can_administer_catalog']
    assert value['auth_storage']=='sqlite-control-only-single-process'
    assert c.get('/api/v1/admin/fields').json()['items']==[]
    assert c.get('/api/v1/stats').json()['people'] is None


def test_deployed_api_rate_limits_still_enforced(deployed):
    c,*_=deployed
    responses=[c.post('/api/v1/auth/login',json={'username':'absent','password':'not-secret'}) for _ in range(7)]
    assert any(r.status_code==429 for r in responses)


@pytest.mark.parametrize('change',[{'environment':'synthetic'},{'database':'bigbase_test'},{'port':18769},
                                  {'schema':'unsafe;drop'},{'port':True}])
def test_identity_rejects_fixture_or_unsafe_configuration(change):
    with pytest.raises(ValueError):identity(**change)


@pytest.mark.parametrize('dsn',[
 'host=/tmp port=18769 dbname=bigbase_test user=test',
 'host=127.0.0.1 port=18769 dbname=bigbase_release_validation user=test',
 'host=remote.example port=15432 dbname=bigbase_release_validation user=test',
 'host=127.0.0.1 port=15432 dbname=other user=test',
 'host=127.0.0.1 port=15432 dbname=bigbase_release_validation user=test hostaddr=127.0.0.2',
 'host=127.0.0.1 port=15432 dbname=bigbase_release_validation user=test options=-csearch_path=public'])
def test_dsn_rejected_before_connection(dsn,monkeypatch):
    monkeypatch.setattr(psycopg,'connect',lambda *a,**k:pytest.fail('Unexpected connection'))
    with pytest.raises(ValueError):DeploymentStore(dsn,identity())


def test_guarded_mode_rejects_testing_and_insecure_cookie(deployed,monkeypatch):
    c,app,secret,reads,config=deployed
    with pytest.raises(RuntimeError):create_app(testing=True,canonical_reads=reads,deployment=config)
    monkeypatch.setenv('BIGBASE_LOCAL_HTTP','1')
    with pytest.raises(RuntimeError):create_app(canonical_reads=reads,deployment=config)


def test_legacy_factory_production_guard_remains(tmp_path,monkeypatch):
    monkeypatch.setenv('BIGBASE_ENV','production')
    with pytest.raises(RuntimeError):create_app(tmp_path)
    assert not tmp_path.joinpath('development.sqlite3').exists()


def test_control_store_rejects_data_and_preserves_original_bytes(tmp_path):
    root=tmp_path/'control';store,user,secret=setup_control(root)
    with store.transaction() as c:store.put(c,'entity',{'id':'sample','name':'synthetic'})
    before=(root/'control.sqlite3').read_bytes();key=(root/'encryption.key').read_bytes()
    with pytest.raises(ValueError,match='PERSON_DATA'):ControlStore(root)
    assert (root/'control.sqlite3').read_bytes()==before
    assert (root/'encryption.key').read_bytes()==key


def test_single_runtime_lock_and_original_account_preserved(tmp_path):
    root=tmp_path/'control';store,user,secret=setup_control(root)
    key=(root/'encryption.key').read_bytes()
    first=ControlStore(root)
    with pytest.raises(ValueError,match='ALREADY_RUNNING'):ControlStore(root)
    with first.transaction() as c:assert first.get(c,'user',user['id'])==user
    first.close();second=ControlStore(root);second.close()
    assert (root/'encryption.key').read_bytes()==key


def test_private_configuration_rejects_symlink_and_world_readable(tmp_path):
    p=tmp_path/'private';p.write_text('synthetic');p.chmod(0o600)
    assert private_text(p)=='synthetic'
    q=tmp_path/'link';q.symlink_to(p)
    with pytest.raises(ValueError):private_text(q)
    p.chmod(0o644)
    with pytest.raises(ValueError):private_text(p)


def test_runtime_status_never_exposes_private_metadata_or_infers_completion(tmp_path):
    from bigbase.deployment import load_runtime_status
    p=tmp_path/'status.json'
    body={'version':1,'updated_at':datetime.now(timezone.utc).isoformat(),'path':'private-do-not-expose',
          'migration':{'status':'restoring','phase':'restoring_backup','processed':None,'total':None,
                       'progress_percent':100,'completion_verified':False,'deployment_id':'never-expose'}}
    p.write_text(json.dumps(body));p.chmod(0o600)
    public,complete=load_runtime_status(p)
    assert public['status']=='restoring' and complete is False
    assert 'private' not in json.dumps(public) and 'deployment_id' not in public
    body['migration']['status']='completed';p.write_text(json.dumps(body))
    assert load_runtime_status(p)[1] is None
    body['migration'].update(processed=10,total=10,completion_verified=True,verification_sha256='a'*64)
    p.write_text(json.dumps(body));assert load_runtime_status(p)[1] is True
    body['migration']['processed']=11;p.write_text(json.dumps(body))
    assert load_runtime_status(p)[1] is None


def test_postgres_inet_text_host_prefix_is_checked_exactly():
    expected=identity();info=FakeRepository(expected).deployment_info()
    info['server_address']='127.0.0.1/32';expected.verify(info)
    for value in ['127.0.0.1/24','127.0.0.2/32',None]:
        info['server_address']=value
        with pytest.raises(ValueError):expected.verify(info)


def test_deployed_api_documentation_omits_disabled_local_person_routes(deployed):
    c,*_=deployed
    paths=c.get('/api/openapi.json').json()['paths']
    assert '/api/v1/canonical/{collection}/enrich' in paths
    assert '/api/v1/{collection}/enrich' not in paths
    assert '/api/v1/imports' not in paths and '/api/v1/bulk-queries' not in paths


def test_runtime_health_expires_report_without_changing_database_health(deployed, tmp_path):
    from datetime import timedelta
    _, _, _, reads, config = deployed
    p = tmp_path / 'status.json'
    p.write_text(json.dumps({'version': 1,
        'updated_at': (datetime.now(timezone.utc) - timedelta(seconds=121)).isoformat(),
        'migration': {'status': 'running', 'phase': 'pilot', 'processed': 10,
                      'total': 100, 'progress_percent': 10}}))
    p.chmod(0o600)
    result = replace(config, status_file=p).health(reads)
    assert result['status'] == 'ok' and result['canonical_connected'] is True
    assert result['migration']['status'] == 'needs_attention'
    assert result['migration']['freshness'] == 'stale'
    assert result['migration_complete'] is None
    assert result['search_enabled'] is False and result['bulk_enabled'] is False

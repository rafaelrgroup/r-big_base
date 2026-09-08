import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from bigbase.api import create_app
from bigbase.rate_limit import RedisLimiter, RateLimitUnavailable

@pytest.fixture
def redis_process(tmp_path):
    runtime=Path(__file__).resolve().parents[2]/'var'/'redis-runtime'/'root'
    binary=runtime/'usr/bin/redis-server'
    if not binary.exists():
        found=shutil.which('redis-server')
        if not found:pytest.skip('Redis real necessário: configure runtime isolado ou instale no ambiente de testes')
        binary=Path(found)
    env={**os.environ,'LD_LIBRARY_PATH':str(runtime/'usr/lib/x86_64-linux-gnu')}
    socket=tmp_path/'redis.sock'
    process=subprocess.Popen([str(binary),'--port','0','--unixsocket',str(socket),'--unixsocketperm','700','--save','','--appendonly','no','--maxmemory','32mb','--maxmemory-policy','noeviction'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    url='unix://'+str(socket)
    try:
        for _ in range(100):
            if socket.exists():break
            if process.poll() is not None:pytest.fail('Redis de teste não iniciou')
            time.sleep(.01)
        yield url,process
    finally:
        if process.poll() is None:process.terminate();process.wait(timeout=5)


def test_atomic_bucket_shared_by_independent_clients(redis_process):
    url,_=redis_process
    clients=[RedisLimiter(url) for _ in range(2)]
    try:
        with ThreadPoolExecutor(max_workers=16) as executor:
            allowed=list(executor.map(lambda n:clients[n%2].consume('same-user',rate=.001,burst=7),range(100)))
        assert sum(allowed)==7
        assert clients[0].consume('another-user',rate=.001,burst=7)
        assert clients[0].client.config_get('maxmemory-policy')['maxmemory-policy']=='noeviction'
    finally:
        for c in clients:c.close()


def test_redis_failure_returns_503_without_local_fallback(redis_process,tmp_path,monkeypatch):
    url,process=redis_process
    limiter=RedisLimiter(url)
    assert limiter.consume('test')
    process.terminate();process.wait(timeout=5)
    with pytest.raises(RateLimitUnavailable):limiter.consume('test')
    limiter.close()
    monkeypatch.setenv('BIGBASE_REDIS_URL',url)
    app=create_app(tmp_path/'app')
    with TestClient(app) as client:
        response=client.get('/api/v1/health')
        assert response.status_code==503
        assert response.headers['Retry-After']=='5'


def test_login_account_limit_survives_ip_rotation(redis_process,tmp_path,monkeypatch):
    url,_=redis_process
    monkeypatch.setenv('BIGBASE_REDIS_URL',url)
    app=create_app(tmp_path/'account-test')
    with TestClient(app,client=('192.0.2.1',1234)) as client:
        statuses=[]
        for number in range(6):
            client._transport.client=('192.0.2.'+str(number+1),1234)
            response=client.post('/api/v1/auth/login',json={'username':'synthetic-target','password':'wrong'})
            statuses.append(response.status_code)
        assert statuses==[401]*5+[429]
        assert 'conta' in response.json()['detail']

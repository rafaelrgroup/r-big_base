#!/usr/bin/env python3
"""Run fresh sequential checks and bind evidence to unchanged source hashes."""
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from uuid import uuid4
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


def now():
    return datetime.now(timezone.utc).isoformat()


def hash_files():
    paths = []
    for folder in ['backend/bigbase', 'backend/tests', 'frontend/src', 'frontend/public', 'scripts', 'infra']:
        paths.extend(path for path in (ROOT/folder).rglob('*') if path.is_file() and '__pycache__' not in path.parts)
    paths.extend(ROOT/path for path in ['frontend/browser-test.mjs', 'frontend/precision-test.mjs',
        'frontend/imports-input-test.mjs', 'frontend/sorting-compat-test.mjs', 'pyproject.toml',
        'requirements.lock', 'frontend/package.json', 'frontend/package-lock.json',
        'frontend/tsconfig.json', 'frontend/vite.config.ts'])
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(paths)}


def save(path, report):
    temp = path.with_name(path.name + '.tmp')
    with temp.open('x') as stream:
        json.dump(report, stream, ensure_ascii=True, indent=2)
        stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
    temp.chmod(0o600); temp.replace(path)


def main():
    os.umask(0o077)
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:8]
    folder = ROOT/'var/validation'/('integration-'+run_id)
    folder.mkdir(parents=True, mode=0o700)
    before = hash_files()
    report = {'run_id': run_id, 'started_at': now(), 'status': 'running', 'project_complete': False,
              'production_migration_executed': False, 'production_load_validated': False,
              'environment': 'isolated-synthetic-development', 'source_files_before': before,
              'checks': [], 'artifacts': {}}
    env = os.environ.copy()
    env.pop('BIGBASE_REDIS_URL', None)
    try:
        # Explicit existing fixture; never boot or initialize production here.
        dsn = (ROOT/'var/postgres-test/dsn.txt').read_text().strip()
        import psycopg
        with psycopg.connect(dsn, connect_timeout=5) as connection:
            row = connection.execute("SELECT current_database(),current_setting('port'),inet_server_addr(),current_setting('server_version_num')::integer").fetchone()
            if row[:3] != ('bigbase_test', '18769', None) or not 180000 <= row[3] < 190000:
                raise RuntimeError('Synthetic PostgreSQL identity mismatch')
            report['postgres_fixture'] = {'database': row[0], 'port': 18769, 'network': 'private_unix_socket', 'version_num': row[3]}
        env['BIGBASE_TEST_PG_DSN'] = dsn
        save(folder/'report.json', report)

        def run(name, arguments, *, cwd=ROOT, timeout=900):
            log = folder/(name+'.log')
            started = time.monotonic()
            entry = {'name': name, 'started_at': now(), 'status': 'running'}
            report['checks'].append(entry)
            save(folder/'report.json', report)
            print(json.dumps({'check': name, 'state': 'running', 'run_id': run_id}), flush=True)
            with log.open('x') as output:
                process = subprocess.Popen(arguments, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    code = process.wait(timeout=timeout)
                except BaseException:
                    # Only the test process group created by this check.
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=5)
                    raise
            entry.update(exit_code=code, duration_seconds=round(time.monotonic()-started,3),
                         finished_at=now(), status='passed' if code==0 else 'failed', log=str(log.relative_to(ROOT)))
            save(folder/'report.json', report)
            if code:
                raise RuntimeError('Check failed: '+name)
            return log

        run('backend', [str(ROOT/'.venv/bin/pytest'), '-q', '--junitxml='+str(folder/'backend.xml')])
        suites = list(ET.parse(folder/'backend.xml').getroot().iter('testsuite'))
        backend = {key:sum(int(suite.get(key,'0')) for suite in suites) for key in ['tests','failures','errors','skipped']}
        if not backend['tests'] or any(backend[key] for key in ['failures','errors','skipped']):
            raise RuntimeError('Backend did not pass all collected tests without skips')
        report['backend'] = backend
        run('build', ['npm','run','build'], cwd=ROOT/'frontend')
        for name, script in [('precision','precision-test.mjs'), ('imports-input','imports-input-test.mjs')]:
            log=run(name,['node',str(ROOT/'frontend'/script)])
            output=log.read_text()
            count=re.search(r'^# tests (\d+)$',output,re.M)
            failures=re.search(r'^# fail (\d+)$',output,re.M)
            skipped=re.search(r'^# skipped (\d+)$',output,re.M)
            if not count or int(count[1])<1 or not failures or int(failures[1]) or not skipped or int(skipped[1]):
                raise RuntimeError('Incomplete TAP evidence: '+name)
            report[name]={'tests':int(count[1]),'failures':0,'skipped':0}
        log=run('sorting-compat',['node',str(ROOT/'frontend/sorting-compat-test.mjs')])
        compatibility=json.loads(log.read_text())
        if compatibility.get('status')!='passed' or not compatibility.get('checks'):
            raise RuntimeError('Invalid sorting compatibility evidence')
        report['sorting_compatibility']={'tests':len(compatibility['checks']),'status':'passed'}
        run('browser',[str(ROOT/'.venv/bin/python'),str(ROOT/'scripts/run-browser-tests.py')])
        browser=json.loads((ROOT/'var/browser-test/browser-report.json').read_text())
        if browser.get('status')!='passed' or not browser.get('checks'):
            raise RuntimeError('Invalid current browser evidence')
        report['browser']={'status':'passed','checks':browser['checks']}
        browser_folder=folder/'browser';browser_folder.mkdir()
        # Evidence whitelist only; never copy fixture credentials/authentication.
        for path in (ROOT/'var/browser-test').iterdir():
            if path.is_file() and (path.name=='browser-report.json' or path.suffix in {'.png','.xlsx','.zip'}):
                shutil.copyfile(path,browser_folder/path.name)
        after=hash_files()
        if after != before:
            report['source_files_after']=after
            report['changed_source_files']=sorted(name for name in set(before)|set(after) if before.get(name)!=after.get(name))
            raise RuntimeError('Sources changed during verification; no combined success claim')
        report['source_files']=after
        report['frontend_assets']={str(path.relative_to(ROOT/'frontend/dist')):hashlib.sha256(path.read_bytes()).hexdigest()
                                   for path in sorted((ROOT/'frontend/dist').rglob('*')) if path.is_file()}
        report['status']='passed'
    except Exception as exc:
        report['status']='failed'
        # Operational errors only; driver/process exceptions can contain secrets.
        report['failure_reason']=str(exc) if type(exc) is RuntimeError else type(exc).__name__
    finally:
        report['finished_at']=now()
        report['artifacts']={str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest()
                             for path in sorted(folder.rglob('*')) if path.is_file() and path.name not in {'report.json','report.json.tmp'}}
        save(folder/'report.json',report)
        if report['status']=='passed':
            output=ROOT/'docs'/('EVIDENCIAS-INTEGRACAO-'+run_id+'.json')
            save(output,report)
        print(json.dumps({'run_id':run_id,'status':report['status'],'report':str(folder/'report.json'),
                          'backend':report.get('backend'),'failure_reason':report.get('failure_reason')}),flush=True)
    return 0 if report['status']=='passed' else 1


if __name__=='__main__':
    raise SystemExit(main())

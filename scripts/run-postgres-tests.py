#!/usr/bin/env python3
"""Private PostgreSQL 18 fixture; never starts production or uses port 5432."""
import argparse
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / 'var/postgres-runtime'
WORK = ROOT / 'var/postgres-test'
DATA = WORK / 'data'
SOCKET = WORK / 'socket'
BIN = RUNTIME / 'root/usr/lib/postgresql/18/bin'
UNIT = 'bigbase-postgres-test.service'
PORT = 18769
DB = 'bigbase_test'
USER = 'bigbase_test_admin'
PURPOSE = 'isolated-synthetic-postgresql-tests'


def run(args, *, capture=True, check=True, **kwargs):
    return subprocess.run(args, check=check, text=True,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None, **kwargs)


def environment():
    env = {key: value for key, value in os.environ.items() if not key.startswith('PG')}
    env['LD_LIBRARY_PATH'] = ':'.join(str(RUNTIME / part) for part in
        ['root/usr/lib/x86_64-linux-gnu', 'root/lib/x86_64-linux-gnu'])
    env['PATH'] = str(BIN) + ':' + env.get('PATH', '')
    env['PGPASSFILE'] = '/dev/null'
    return env


def private_json(path, value):
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temp.chmod(0o600)
    temp.replace(path)


def marker():
    path = WORK / 'purpose.json'
    if not path.exists():
        if WORK.exists() and any(WORK.iterdir()):
            raise RuntimeError('Pasta de teste existente sem identificação; inspecione antes de continuar.')
        WORK.mkdir(mode=0o700, parents=True, exist_ok=True)
        private_json(path, {'purpose': PURPOSE, 'root': str(ROOT), 'port': PORT})
    value = json.loads(path.read_text())
    if value != {'purpose': PURPOSE, 'root': str(ROOT), 'port': PORT}:
        raise RuntimeError('Identificação da pasta de teste divergente.')
    WORK.chmod(0o700)
    SOCKET.mkdir(mode=0o700, exist_ok=True)
    SOCKET.chmod(0o700)


def unit_state():
    completed = run(['systemctl', '--user', 'show', UNIT, '--property=ActiveState,MainPID,ExecStart,WorkingDirectory,MemoryCurrent,MemoryMax,MemoryPeak,Result'], check=False)
    return dict(line.split('=', 1) for line in completed.stdout.splitlines() if '=' in line)


def guard_unit(state):
    if state.get('ActiveState') not in {'active', 'activating', 'deactivating'}:
        return False
    if state.get('WorkingDirectory') != str(WORK) or str(BIN / 'postgres') not in state.get('ExecStart', '') or str(DATA) not in state.get('ExecStart', ''):
        raise RuntimeError('Unidade com mesmo nome não pertence a este fixture.')
    return True


def query(sql, database='postgres'):
    result = run([str(BIN / 'psql'), '-X', '--no-password', '-A', '-t', '-v', 'ON_ERROR_STOP=1',
                  '-h', str(SOCKET), '-p', str(PORT), '-U', USER, '-d', database, '-c', sql],
                 env=environment(), timeout=20)
    return result.stdout.strip()


def identity():
    data = json.loads(query("SELECT json_build_object('version', current_setting('server_version'),"
        "'version_num', current_setting('server_version_num'), 'data_directory', current_setting('data_directory'),"
        "'listen_addresses', current_setting('listen_addresses'), 'port', current_setting('port'),"
        "'shared_buffers', current_setting('shared_buffers'), 'max_connections', current_setting('max_connections'),"
        "'max_locks_per_transaction', current_setting('max_locks_per_transaction'),"
        "'fsync', current_setting('fsync'), 'synchronous_commit', current_setting('synchronous_commit'),"
        "'full_page_writes', current_setting('full_page_writes'), 'jit', current_setting('jit'),"
        "'max_wal_size', current_setting('max_wal_size'), 'io_method', current_setting('io_method'))"))
    if not 180000 <= int(data['version_num']) < 190000 or data['data_directory'] != str(DATA) or data['listen_addresses'] or int(data['port']) != PORT:
        raise RuntimeError('Identidade do PostgreSQL de teste divergente; nenhum teste será executado.')
    if data['fsync'] != 'on' or data['synchronous_commit'] != 'on' or data['full_page_writes'] != 'on':
        raise RuntimeError('Garantias de escrita durável estão desativadas.')
    return data


def start():
    if os.getuid() == 0:
        raise RuntimeError('Execute como usuário normal, sem sudo.')
    run(['bash', str(ROOT / 'scripts/prepare-postgres-test.sh')], capture=False)
    marker()
    if not (DATA / 'PG_VERSION').exists():
        if DATA.exists() and any(DATA.iterdir()):
            raise RuntimeError('Diretório de dados parcial existente; nada será apagado automaticamente.')
        run([str(BIN / 'initdb'), '-D', str(DATA), '-L', str(RUNTIME / 'root/usr/share/postgresql/18'),
             '--username=' + USER, '--auth-local=trust', '--auth-host=reject', '--encoding=UTF8',
             '--locale=C', '--data-checksums', '--wal-segsize=1',
             '--set=shared_buffers=16MB', '--set=max_connections=10', '--set=maintenance_work_mem=4MB',
             '--set=work_mem=1MB', '--set=io_method=sync', '--set=jit=off'],
            env=environment(), capture=False, timeout=120)
    if (DATA / 'PG_VERSION').read_text().strip() != '18':
        raise RuntimeError('Diretório existente não é PostgreSQL 18.')
    state = unit_state()
    if not guard_unit(state):
        configuration = WORK / 'fixture.conf'
        # Fixed paths/configuration; no supplied DSN, remote host or production path.
        settings = {
            'listen_addresses': "''", 'port': str(PORT), 'unix_socket_directories': "'" + str(SOCKET) + "'",
            'unix_socket_permissions': '0700', 'shared_buffers': "'16MB'", 'max_connections': '10',
            'superuser_reserved_connections': '1', 'reserved_connections': '0',
            'max_locks_per_transaction': '512',
            'work_mem': "'1MB'", 'maintenance_work_mem': "'4MB'", 'effective_cache_size': "'32MB'",
            'wal_buffers': "'512kB'", 'min_wal_size': "'4MB'", 'max_wal_size': "'16MB'",
            'max_worker_processes': '0', 'max_parallel_workers': '0', 'max_parallel_maintenance_workers': '0',
            'io_method': "'sync'", 'autovacuum': 'off', 'jit': 'off', 'fsync': 'on',
            'synchronous_commit': 'on', 'full_page_writes': 'on', 'logging_collector': 'off',
            'log_statement': "'none'", 'log_min_error_statement': "'panic'", 'log_connections': "''",
            'log_disconnections': 'off', 'log_lock_waits': 'off', 'log_temp_files': '-1',
        }
        configuration.write_text('\n'.join(key + ' = ' + value for key, value in settings.items()) + '\n')
        configuration.chmod(0o600)
        run(['systemctl', '--user', 'reset-failed', UNIT], check=False)
        run(['systemd-run', '--user', '--collect', '--unit=' + UNIT, '--service-type=exec',
             '--working-directory=' + str(WORK), '--property=MemoryMax=96M', '--property=MemorySwapMax=0',
             '--property=CPUQuota=50%', '--property=TasksMax=48', '--property=TimeoutStopSec=30s',
             '--property=KillSignal=SIGINT', '--property=Restart=no', '--property=NoNewPrivileges=yes',
             '--setenv=LD_LIBRARY_PATH=' + environment()['LD_LIBRARY_PATH'],
             str(BIN / 'postgres'), '-D', str(DATA), '-c', 'config_file=' + str(configuration)],
            capture=False, timeout=30)
    deadline = time.monotonic() + 45
    while True:
        try:
            server = identity()
            break
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            if time.monotonic() >= deadline:
                raise RuntimeError('O PostgreSQL isolado não iniciou; inspecione journalctl --user -u ' + UNIT)
            time.sleep(1)
    if query("SELECT 1 FROM pg_database WHERE datname='bigbase_test'") != '1':
        query('CREATE DATABASE bigbase_test')
    dsn = f"host={SOCKET} port={PORT} dbname={DB} user={USER} application_name=bigbase_synthetic_test connect_timeout=5"
    (WORK / 'dsn.txt').write_text(dsn + '\n')
    (WORK / 'dsn.txt').chmod(0o600)
    result = {'status': 'ready', 'purpose': PURPOSE, 'checked_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'server': server, 'unit': unit_state(), 'dsn_file': str(WORK / 'dsn.txt'),
              'network': 'private_unix_socket_only', 'production_connected': False,
              'note': 'Small fixture for sequential correctness tests; not a production sizing or load benchmark.'}
    private_json(WORK / 'status.json', result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return dsn


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--start-only', action='store_true')
    group.add_argument('--stop', action='store_true')
    group.add_argument('--status', action='store_true')
    group.add_argument('--pytest', action='store_true')
    parser.add_argument('pytest_args', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.stop:
        if guard_unit(unit_state()):
            run(['systemctl', '--user', 'stop', UNIT], capture=False, timeout=45)
        print('Fixture parado; arquivos sintéticos preservados.')
        return
    if args.status:
        state = unit_state()
        if not guard_unit(state):
            raise SystemExit('Fixture não está ativo.')
        print(json.dumps({'server': identity(), 'unit': state}, ensure_ascii=False, indent=2))
        return
    dsn = start()
    if args.pytest:
        env = environment()
        env['BIGBASE_TEST_PG_DSN'] = dsn
        arguments = args.pytest_args[1:] if args.pytest_args[:1] == ['--'] else args.pytest_args
        result = run([str(ROOT / '.venv/bin/pytest'), *(arguments or ['-q', 'backend/tests/test_postgres.py'])],
                     env=env, cwd=ROOT, capture=False, check=False)
        raise SystemExit(result.returncode)


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Private PostgreSQL18 fixture; never connects to a system/global cluster.

First run on a host with PostgreSQL18 already installed:
  python3 scripts/run-postgres-tests.py --runtime system --start-only
Later --pytest/--status/--stop reuse the saved runtime binding automatically.
Fresh fixtures otherwise retain the signed Debian12 download mode. Switching
an existing fixture's runtime is refused; use a new isolated checkout/fixture.
System binaries remain managed by the operator; this script never installs them.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / 'var/postgres-runtime'
WORK = ROOT / 'var/postgres-test'
DATA = WORK / 'data'
SOCKET = WORK / 'socket'
BIN = RUNTIME / 'root/usr/lib/postgresql/18/bin'
SHARE = RUNTIME / 'root/usr/share/postgresql/18'
SYSTEM_BIN = Path('/usr/lib/postgresql/18/bin')
SYSTEM_SHARE = Path('/usr/share/postgresql/18')
RUNTIME_MODE = 'download'
BINARY_NAMES = ('postgres', 'initdb', 'pg_ctl', 'psql')
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
    env = {key: value for key, value in os.environ.items() if not key.startswith(('PG', 'LD_'))}
    if RUNTIME_MODE == 'download':
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


def nonroot():
    if os.getuid() == 0 or os.geteuid() == 0:
        raise RuntimeError('Execute como usuário normal, sem sudo.')


def binary_hash(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def trusted_system_path(path, *, directory=False, executable=True):
    """System runtime is fixed, root-owned and never writable by the test user."""
    value = path.lstat()
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(value.st_mode) or value.st_uid != 0 or value.st_mode & 0o022:
        raise RuntimeError('Runtime de sistema deve ser regular, pertencer a root e não permitir escrita por grupo/outros.')
    if not directory and executable and not value.st_mode & 0o111:
        raise RuntimeError('Executável do runtime de sistema não possui permissão de execução.')


def inspect_system_runtime():
    trusted_system_path(SYSTEM_BIN, directory=True)
    trusted_system_path(SYSTEM_SHARE, directory=True)
    trusted_system_path(SYSTEM_SHARE / 'postgres.bki', executable=False)
    env = {key: value for key, value in os.environ.items() if not key.startswith(('PG', 'LD_'))}
    env['PGPASSFILE'] = '/dev/null'
    versions = {}
    minor_versions = set()
    hashes = {}
    for name in BINARY_NAMES:
        path = SYSTEM_BIN / name
        trusted_system_path(path)
        version = run([str(path), '--version'], env=env, timeout=10).stdout.strip()
        match = re.fullmatch(re.escape(name) + r' \(PostgreSQL\) (18\.\d+)(?:\s.*)?', version)
        if not match:
            raise RuntimeError('Runtime de sistema deve conter somente executáveis PostgreSQL18.')
        minor_versions.add(match[1])
        versions[name] = version
        hashes[name] = binary_hash(path)
    if len(minor_versions) != 1:
        raise RuntimeError('Executáveis PostgreSQL18 de sistema possuem versões menores divergentes.')
    return {'purpose': PURPOSE, 'mode': 'system', 'major': 18, 'binary_root': str(SYSTEM_BIN),
            'share_root': str(SYSTEM_SHARE), 'postgres_version': versions['postgres'],
            'versions': versions, 'binary_sha256': hashes}


def prepare_system_runtime():
    nonroot()
    if RUNTIME.resolve() != RUNTIME or RUNTIME.is_symlink():
        raise RuntimeError('Diretório privado do runtime não pode ser um link.')
    manifest = inspect_system_runtime()
    path = RUNTIME / 'runtime-system.json'
    if path.is_symlink():
        raise RuntimeError('Manifesto privado do runtime não pode ser um link.')
    if path.exists():
        previous = json.loads(path.read_text())
        if {key: previous.get(key) for key in manifest} != manifest:
            raise RuntimeError('Runtime de sistema mudou desde a preparação; inspecione e revalide explicitamente antes de continuar.')
    else:
        RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
        RUNTIME.chmod(0o700)
        private_json(path, {**manifest, 'prepared_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                           'verification': 'Existing root-owned PostgreSQL18 executables and SHA256; no global package or cluster modified'})
    print('Runtime PostgreSQL18 de sistema conferido; nenhum pacote, serviço ou cluster global foi alterado.')
    return manifest


def binding_value(mode):
    binary_root = SYSTEM_BIN if mode == 'system' else RUNTIME / 'root/usr/lib/postgresql/18/bin'
    return {'purpose': PURPOSE, 'mode': mode, 'binary_root': str(binary_root),
            'data_directory': str(DATA), 'port': PORT}


def configure_runtime(requested=None):
    global RUNTIME_MODE, BIN, SHARE
    nonroot()
    if any(path.resolve() != path or path.is_symlink() for path in (WORK, DATA, SOCKET)):
        raise RuntimeError('Diretórios do fixture não podem apontar para outro local por links.')
    binding_path = WORK / 'runtime-binding.json'
    if binding_path.is_symlink() or (WORK / 'purpose.json').is_symlink():
        raise RuntimeError('Identificação do fixture não pode ser um link.')
    binding = json.loads(binding_path.read_text()) if binding_path.exists() else None
    chosen = requested or (binding.get('mode') if isinstance(binding, dict) else 'download')
    if chosen not in {'download', 'system'}:
        raise RuntimeError('Modo de runtime desconhecido.')
    if binding is not None and binding != binding_value(chosen):
        raise RuntimeError('Runtime divergente do fixture existente; não troque binários/cluster implicitamente.')
    if binding is None and (WORK / 'purpose.json').exists() and chosen != 'download':
        raise RuntimeError('Fixture legado sem binding pertence ao runtime baixado; use um fixture novo para o runtime de sistema.')
    RUNTIME_MODE = chosen
    BIN = SYSTEM_BIN if chosen == 'system' else RUNTIME / 'root/usr/lib/postgresql/18/bin'
    SHARE = SYSTEM_SHARE if chosen == 'system' else RUNTIME / 'root/usr/share/postgresql/18'
    return chosen


def bind_runtime():
    path = WORK / 'runtime-binding.json'
    expected = binding_value(RUNTIME_MODE)
    if path.exists() and json.loads(path.read_text()) != expected:
        raise RuntimeError('Binding do runtime mudou durante a preparação.')
    if not path.exists():
        private_json(path, expected)


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
    raw = state.get('ExecStart', '')
    paths = re.findall(r'(?:^|[\s{;])path=([^;]+);', raw)
    commands = re.findall(r'argv\[\]=([^;]*);', raw)
    expected = [str(BIN / 'postgres'), '-D', str(DATA), '-c', 'config_file=' + str(WORK / 'fixture.conf')]
    try:
        arguments = shlex.split(commands[0]) if len(commands) == 1 else []
    except ValueError:
        arguments = []
    if state.get('WorkingDirectory') != str(WORK) or len(paths) != 1 or paths[0].strip() != str(BIN / 'postgres') or arguments != expected:
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


def start(runtime_mode=None):
    configure_runtime(runtime_mode)
    run(['bash', str(ROOT / 'scripts/prepare-postgres-test.sh'), '--runtime', RUNTIME_MODE], capture=False)
    marker()
    bind_runtime()
    if not (DATA / 'PG_VERSION').exists():
        if DATA.exists() and any(DATA.iterdir()):
            raise RuntimeError('Diretório de dados parcial existente; nada será apagado automaticamente.')
        run([str(BIN / 'initdb'), '-D', str(DATA), '-L', str(SHARE),
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
             '--setenv=LD_LIBRARY_PATH=' + environment().get('LD_LIBRARY_PATH', ''),
             '--setenv=LD_PRELOAD=', '--setenv=LD_AUDIT=',
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
              'network': 'private_unix_socket_only', 'production_connected': False, 'runtime_mode': RUNTIME_MODE,
              'note': 'Small fixture for sequential correctness tests; not a production sizing or load benchmark.'}
    private_json(WORK / 'status.json', result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return dsn


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--runtime', choices=('download', 'system'), help='Primeira preparação: system usa PostgreSQL18 instalado; depois reutiliza o binding privado salvo.')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--start-only', action='store_true')
    group.add_argument('--stop', action='store_true')
    group.add_argument('--status', action='store_true')
    group.add_argument('--pytest', action='store_true')
    group.add_argument('--prepare-system-runtime', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('pytest_args', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    nonroot()
    if args.prepare_system_runtime:
        if args.runtime not in {None, 'system'}:
            raise RuntimeError('Preparação de sistema incompatível com o runtime solicitado.')
        prepare_system_runtime()
        return
    configure_runtime(args.runtime)
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
    dsn = start(args.runtime)
    if args.pytest:
        env = environment()
        env['BIGBASE_TEST_PG_DSN'] = dsn
        arguments = args.pytest_args[1:] if args.pytest_args[:1] == ['--'] else args.pytest_args
        result = run([str(ROOT / '.venv/bin/pytest'), *(arguments or ['-q', 'backend/tests/test_postgres.py'])],
                     env=env, cwd=ROOT, capture=False, check=False)
        raise SystemExit(result.returncode)


if __name__ == '__main__':
    main()

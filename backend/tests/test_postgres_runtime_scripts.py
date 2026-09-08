"""Guardrails for fixture runtime selection; no PostgreSQL or systemd is started."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest


REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def runner(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('postgres_fixture_runner', REPO / 'scripts/run-postgres-tests.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ROOT = tmp_path / 'project'
    module.ROOT.mkdir()
    module.RUNTIME = module.ROOT / 'var/postgres-runtime'
    module.WORK = module.ROOT / 'var/postgres-test'
    module.DATA = module.WORK / 'data'
    module.SOCKET = module.WORK / 'socket'
    module.SYSTEM_BIN = tmp_path / 'system/usr/lib/postgresql/18/bin'
    module.SYSTEM_SHARE = tmp_path / 'system/usr/share/postgresql/18'
    monkeypatch.setattr(module.os, 'getuid', lambda: 1000)
    monkeypatch.setattr(module.os, 'geteuid', lambda: 1000)
    return module


def synthetic_system(runner, monkeypatch):
    runner.SYSTEM_BIN.mkdir(parents=True)
    runner.SYSTEM_SHARE.mkdir(parents=True)
    (runner.SYSTEM_SHARE / 'postgres.bki').write_text('synthetic initdb metadata')
    for name in runner.BINARY_NAMES:
        (runner.SYSTEM_BIN / name).write_text('synthetic binary ' + name)
    # Ownership checks are tested separately with controlled stat metadata; these
    # synthetic executables are never run and deliberately belong to pytest.
    monkeypatch.setattr(runner, 'trusted_system_path', lambda *args, **kwargs: None)
    def version(args, **kwargs):
        assert args[1:] == ['--version']
        assert not any(key.startswith('LD_') for key in kwargs['env'])
        return SimpleNamespace(stdout=Path(args[0]).name + ' (PostgreSQL) 18.6 (synthetic)\n')
    monkeypatch.setattr(runner, 'run', version)


def test_system_runtime_manifest_is_private_and_does_not_create_cluster(runner, monkeypatch):
    synthetic_system(runner, monkeypatch)
    manifest = runner.prepare_system_runtime()
    path = runner.RUNTIME / 'runtime-system.json'
    assert manifest['major'] == 18 and manifest['mode'] == 'system'
    assert set(manifest['binary_sha256']) == set(runner.BINARY_NAMES)
    assert path.stat().st_mode & 0o777 == 0o600
    assert runner.RUNTIME.stat().st_mode & 0o777 == 0o700
    assert not runner.WORK.exists()
    initial = path.read_bytes()
    runner.prepare_system_runtime()
    assert path.read_bytes() == initial


def test_system_runtime_binary_changes_are_not_silently_reapproved(runner, monkeypatch):
    synthetic_system(runner, monkeypatch)
    runner.prepare_system_runtime()
    before = (runner.RUNTIME / 'runtime-system.json').read_bytes()
    (runner.SYSTEM_BIN / 'postgres').write_text('synthetic altered executable')
    with pytest.raises(RuntimeError, match='mudou'):
        runner.prepare_system_runtime()
    assert (runner.RUNTIME / 'runtime-system.json').read_bytes() == before


@pytest.mark.parametrize('version', ['17.9', '19.0', '18.devel'])
def test_system_runtime_rejects_other_or_unreleased_major(runner, monkeypatch, version):
    synthetic_system(runner, monkeypatch)
    monkeypatch.setattr(runner, 'run', lambda args, **kwargs: SimpleNamespace(stdout=Path(args[0]).name + ' (PostgreSQL) ' + version))
    with pytest.raises(RuntimeError, match='PostgreSQL18'):
        runner.prepare_system_runtime()
    assert not runner.RUNTIME.exists()


def test_mixed_minor_versions_are_rejected(runner, monkeypatch):
    synthetic_system(runner, monkeypatch)
    monkeypatch.setattr(runner, 'run', lambda args, **kwargs: SimpleNamespace(stdout=Path(args[0]).name + ' (PostgreSQL) ' + ('18.5' if Path(args[0]).name == 'psql' else '18.6')))
    with pytest.raises(RuntimeError, match='menores divergentes'):
        runner.prepare_system_runtime()


@pytest.mark.parametrize('uid,mode', [(1000, stat.S_IFREG | 0o755), (0, stat.S_IFREG | 0o775), (0, stat.S_IFREG | 0o777), (0, stat.S_IFLNK | 0o777), (0, stat.S_IFREG | 0o644)])
def test_system_binary_requires_root_owned_regular_nonwritable_executable(runner, uid, mode):
    path = SimpleNamespace(lstat=lambda: SimpleNamespace(st_uid=uid, st_mode=mode))
    with pytest.raises(RuntimeError):
        runner.trusted_system_path(path)


def test_system_binding_is_explicit_initially_then_reused_without_flag(runner):
    assert runner.configure_runtime() == 'download'
    assert runner.configure_runtime('system') == 'system'
    runner.marker()
    runner.bind_runtime()
    assert runner.configure_runtime() == 'system'
    assert runner.BIN == runner.SYSTEM_BIN and runner.SHARE == runner.SYSTEM_SHARE
    assert runner.PORT == 18769
    with pytest.raises(RuntimeError, match='divergente'):
        runner.configure_runtime('download')


def test_legacy_fixture_can_keep_download_but_cannot_silently_switch(runner):
    runner.marker()
    assert runner.configure_runtime() == 'download'
    with pytest.raises(RuntimeError, match='legado'):
        runner.configure_runtime('system')


@pytest.mark.parametrize('changed', ['mode', 'port', 'data_directory', 'binary_root'])
def test_altered_runtime_binding_cannot_select_other_cluster(runner, changed):
    runner.configure_runtime('system')
    runner.marker()
    binding = runner.binding_value('system')
    binding[changed] = 5432 if changed == 'port' else 'synthetic-unexpected-value'
    runner.private_json(runner.WORK / 'runtime-binding.json', binding)
    with pytest.raises(RuntimeError):
        runner.configure_runtime()


def test_fixture_cannot_point_data_to_external_directory(runner, tmp_path):
    runner.WORK.mkdir(parents=True)
    outside = tmp_path / 'external-existing-database'
    outside.mkdir()
    runner.DATA.symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match='links'):
        runner.configure_runtime('system')
    assert list(outside.iterdir()) == []


def test_unit_guard_requires_exact_binary_data_and_configuration(runner):
    runner.configure_runtime('system')
    binary = str(runner.SYSTEM_BIN / 'postgres')
    argv = f'{binary} -D {runner.DATA} -c config_file={runner.WORK}/fixture.conf'
    expected = {'ActiveState': 'active', 'WorkingDirectory': str(runner.WORK), 'ExecStart': f'{{ path={binary} ; argv[]={argv} ; ignore_errors=no ; }}'}
    assert runner.guard_unit(expected) is True
    for changed in [
        expected['ExecStart'].replace(binary, binary + '-different'),
        expected['ExecStart'].replace(str(runner.DATA), str(runner.DATA) + '-different'),
        expected['ExecStart'].replace('fixture.conf', 'different.conf'),
        expected['ExecStart'].replace(' ; ignore_errors=', ' -c data_directory=/synthetic/other ; ignore_errors='),
    ]:
        with pytest.raises(RuntimeError, match='não pertence'):
            runner.guard_unit({**expected, 'ExecStart': changed})


def test_runtime_binding_symlink_cannot_escape_private_directory(runner, tmp_path):
    runner.WORK.mkdir(parents=True)
    outside = tmp_path / 'external-binding.json'
    outside.write_text(json.dumps(runner.binding_value('system')))
    (runner.WORK / 'runtime-binding.json').symlink_to(outside)
    with pytest.raises(RuntimeError, match='link'):
        runner.configure_runtime('system')


def test_system_environment_ignores_pg_and_debian_library_injection(runner, monkeypatch):
    monkeypatch.setenv('PGHOST', 'synthetic-external-host')
    monkeypatch.setenv('PGPORT', '5432')
    monkeypatch.setenv('PGSERVICE', 'synthetic-production')
    monkeypatch.setenv('LD_LIBRARY_PATH', '/synthetic/debian-runtime')
    monkeypatch.setenv('LD_PRELOAD', '/synthetic/injection.so')
    runner.configure_runtime('system')
    env = runner.environment()
    assert env['PGPASSFILE'] == '/dev/null'
    assert set(k for k in env if k.startswith('PG')) == {'PGPASSFILE'}
    assert not any(k.startswith('LD_') for k in env)
    assert env['PATH'].startswith(str(runner.SYSTEM_BIN) + ':')


@pytest.mark.parametrize('action', ['--start-only', '--stop', '--status', '--prepare-system-runtime'])
def test_root_is_rejected_before_any_service_or_database_action(runner, monkeypatch, action):
    monkeypatch.setattr(runner.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(sys, 'argv', ['runner', action])
    monkeypatch.setattr(runner, 'run', lambda *args, **kwargs: pytest.fail('No command permitted as root'))
    with pytest.raises(RuntimeError, match='usuário normal'):
        runner.main()
    assert not runner.WORK.exists()


def test_system_start_keeps_private_paths_and_never_uses_default_cluster(runner, monkeypatch):
    calls = []
    def capture(args, **kwargs):
        calls.append(args)
        if args[0] == str(runner.SYSTEM_BIN / 'initdb'):
            runner.DATA.mkdir()
            (runner.DATA / 'PG_VERSION').write_text('18\n')
        return SimpleNamespace(stdout='', returncode=0)
    monkeypatch.setattr(runner, 'run', capture)
    monkeypatch.setattr(runner, 'unit_state', lambda: {'ActiveState': 'inactive'})
    monkeypatch.setattr(runner, 'identity', lambda: {'fixture': True})
    monkeypatch.setattr(runner, 'query', lambda *args, **kwargs: '1')
    dsn = runner.start('system')
    assert 'port=18769' in dsn and 'host=' + str(runner.SOCKET) in dsn
    initdb = next(args for args in calls if args[0] == str(runner.SYSTEM_BIN / 'initdb'))
    assert initdb[initdb.index('-D') + 1] == str(runner.DATA)
    assert initdb[initdb.index('-L') + 1] == str(runner.SYSTEM_SHARE)
    assert '--auth-host=reject' in initdb
    launch = next(args for args in calls if args[0] == 'systemd-run')
    assert '--user' in launch and '--setenv=LD_LIBRARY_PATH=' in launch
    assert str(runner.SYSTEM_BIN / 'postgres') in launch
    configuration = (runner.WORK / 'fixture.conf').read_text()
    assert "listen_addresses = ''" in configuration and 'port = 18769' in configuration
    assert all('5432' not in arg for args in calls for arg in args)


def test_shell_system_mode_delegates_without_debian_or_download_commands(tmp_path):
    root = tmp_path / 'project'
    scripts = root / 'scripts'
    scripts.mkdir(parents=True)
    shutil.copy2(REPO / 'scripts/prepare-postgres-test.sh', scripts / 'prepare-postgres-test.sh')
    shims = tmp_path / 'shims'
    shims.mkdir()
    for name, body in {
        'id': '#!/bin/sh\nprintf "1000\\n"\n',
        'python3': '#!/bin/sh\nprintf "%s\\n" "$@"\n',
        'dpkg': '#!/bin/sh\nexit 91\n',
        'curl': '#!/bin/sh\nexit 92\n',
        'apt-get': '#!/bin/sh\nexit 93\n',
    }.items():
        path = shims / name
        path.write_text(body)
        path.chmod(0o700)
    env = {**os.environ, 'PATH': str(shims) + ':/usr/bin:/bin'}
    result = subprocess.run(['bash', str(scripts / 'prepare-postgres-test.sh'), '--runtime', 'system'], env=env, text=True, capture_output=True, timeout=5)
    assert result.returncode == 0
    assert result.stdout.splitlines() == [str(scripts / 'run-postgres-tests.py'), '--prepare-system-runtime']
    assert not (root / 'var').exists()

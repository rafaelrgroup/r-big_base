"""Run the browser suite with its own synthetic server and bounded cleanup."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
import json

ROOT = Path(__file__).resolve().parents[1]
PORT = 18767

def main():
    # Never stop a process that was not created by this run.
    with socket.socket() as probe:
        try:
            probe.bind(('127.0.0.1', PORT))
        except OSError:
            raise SystemExit('A porta de teste 18767 está ocupada; encerre apenas seu servidor de teste antes de repetir.')
    env = {**os.environ, 'PYTHONPATH': str(ROOT / 'backend'),
           'BIGBASE_DATA': str(ROOT / 'var' / 'browser-test'),
           'BIGBASE_LOCAL_HTTP': '1', 'BIGBASE_ENV': 'development'}
    # Tests use their own process-local limits, never a caller's Redis instance.
    env.pop('BIGBASE_REDIS_URL', None)
    subprocess.run([sys.executable, str(ROOT / 'scripts' / 'browser-fixture.py')],
                   cwd=ROOT, env=env, check=True)
    logs = ROOT / 'var' / 'browser-test' / 'server.log'
    with logs.open('w') as output:
        server = subprocess.Popen(
            [sys.executable, '-m', 'uvicorn', 'bigbase.api:create_app', '--factory',
             '--host', '127.0.0.1', '--port', str(PORT), '--no-access-log'],
            cwd=ROOT, env=env, stdout=output, stderr=subprocess.STDOUT)
        try:
            for _ in range(100):
                if server.poll() is not None:
                    raise RuntimeError('Servidor sintético não iniciou; confira var/browser-test/server.log.')
                try:
                    with urllib.request.urlopen(f'http://127.0.0.1:{PORT}/api/v1/health', timeout=1) as response:
                        health = json.load(response)
                    if health.get('environment') != 'isolated-development' or health.get('production_connected') is not False:
                        raise RuntimeError('Identidade do ambiente de teste divergente.')
                    break
                except (OSError, TimeoutError):
                    time.sleep(0.2)
            else:
                raise RuntimeError('Servidor sintético não ficou disponível.')
            libraries = ROOT / 'var' / 'browser-libs' / 'root' / 'usr' / 'lib' / 'x86_64-linux-gnu'
            if libraries.is_dir():
                env['LD_LIBRARY_PATH'] = str(libraries) + (':' + env['LD_LIBRARY_PATH'] if env.get('LD_LIBRARY_PATH') else '')
            completed = subprocess.run(['node', str(ROOT / 'frontend' / 'browser-test.mjs')],
                                       cwd=ROOT / 'frontend', env=env)
            return completed.returncode
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)

if __name__ == '__main__':
    raise SystemExit(main())

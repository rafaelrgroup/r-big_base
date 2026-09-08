"""Application maintenance lifecycle with a fake synthetic repository; no live database."""
from copy import deepcopy
import logging
import threading
import time
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from bigbase.api import create_app
from bigbase.canonical_http import CanonicalReads
import bigbase.canonical_http as canonical_http


class SyntheticRepository:
    dsn = 'host=/tmp/synthetic-cursor-maintenance port=18769 dbname=bigbase_test'

    def __init__(self, results):
        self.info = {'deployment_id': str(uuid4()), 'environment': 'synthetic',
                     'database': 'bigbase_test', 'server_address': None,
                     'server_port': 18769, 'server_version_num': 180006}
        self.results = list(results)
        self.calls = []
        self.verifications = 0
        self.verified = threading.Event()
        self.completed = threading.Event()

    def deployment_info(self):
        self.verifications += 1
        self.verified.set()
        return deepcopy(self.info)

    def cleanup_read_cursors(self, *, limit):
        self.calls.append(limit)
        result = self.results.pop(0) if self.results else 0
        if isinstance(result, Exception):
            raise result
        if not self.results:
            self.completed.set()
        return result


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    for name in ('PGHOSTADDR', 'PGSERVICE', 'BIGBASE_REDIS_URL'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(canonical_http, 'CURSOR_CLEANUP_BACKLOG_SECONDS', 0.01)
    monkeypatch.setattr(canonical_http, 'CURSOR_CLEANUP_RETRY_SECONDS', 0.01)


def app_with_repository(tmp_path, results):
    repository = SyntheticRepository(results)
    reads = CanonicalReads(repository, expected_deployment_id=repository.info['deployment_id'])
    return create_app(tmp_path, testing=True, canonical_reads=reads), repository


def test_lifespan_drains_full_batches_then_waits_and_stops_on_shutdown(tmp_path, monkeypatch):
    monkeypatch.setattr(canonical_http, 'CURSOR_CLEANUP_IDLE_SECONDS', 0.1)
    app, repository = app_with_repository(tmp_path, [1000, 1000, 7])
    with TestClient(app) as client:
        assert repository.completed.wait(3), 'Application did not drain the pending cursor batches'
        assert repository.calls == [1000, 1000, 1000]
        assert repository.verifications == 4  # Construction plus every cleanup batch.
        assert client.get('/api/v1/health').status_code == 200
    finished_calls = len(repository.calls)
    time.sleep(0.15)
    assert len(repository.calls) == finished_calls, 'Cleanup continued after application shutdown'


def test_transient_cleanup_failure_is_sanitized_and_retried(tmp_path, caplog):
    private_marker = 'SYNTHETIC-EXCEPTION-VALUE-MUST-NOT-ENTER-LOGS'
    app, repository = app_with_repository(tmp_path, [RuntimeError(private_marker), 0])
    with caplog.at_level(logging.WARNING, logger='bigbase.canonical_http'):
        with TestClient(app):
            assert repository.completed.wait(3), 'Cleanup did not resume after a transient error'
    assert repository.calls == [1000, 1000]
    assert repository.verifications == 3
    warnings = [row for row in caplog.records if row.name == 'bigbase.canonical_http']
    assert len(warnings) == 1 and warnings[0].exc_info is None
    assert private_marker not in caplog.text


def test_destination_is_rechecked_before_each_batch_and_recovers(tmp_path):
    app, repository = app_with_repository(tmp_path, [0])
    repository.info['environment'] = 'production'
    repository.verified.clear()
    with TestClient(app):
        assert repository.verified.wait(3)
        assert repository.calls == [], 'Cleanup reached a deployment that failed verification'
        repository.info['environment'] = 'synthetic'
        assert repository.completed.wait(3), 'Cleanup did not retry after the synthetic destination returned'
    assert repository.calls == [1000]


def test_disabled_canonical_reads_do_not_start_maintenance(tmp_path, monkeypatch):
    calls = []
    async def forbidden(reads):
        calls.append(reads)
    monkeypatch.setattr(canonical_http, 'maintain_canonical_cursors', forbidden)
    with TestClient(create_app(tmp_path, testing=True)) as client:
        assert client.get('/api/v1/health').status_code == 200
    assert calls == []


@pytest.mark.parametrize('invalid_count', [True, -1, 1001, '1'])
def test_invalid_repository_count_fails_closed(invalid_count):
    repository = SyntheticRepository([invalid_count])
    reads = CanonicalReads(repository, expected_deployment_id=repository.info['deployment_id'])
    with pytest.raises(ValueError, match='CANONICAL_CURSOR_CLEANUP_INVALID_COUNT'):
        reads.cleanup_read_cursors()
    assert repository.calls == [1000]

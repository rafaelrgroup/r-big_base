"""Synthetic status publications: no real services or migration data."""
from datetime import datetime, timedelta, timezone
import json

import pytest

from bigbase.deployment import load_runtime_status

NOW = datetime(2026, 9, 9, 6, tzinfo=timezone.utc)


def publication(tmp_path, *, age=0, status='running', phase='pilot'):
    body = {'version': 1, 'updated_at': (NOW - timedelta(seconds=age)).isoformat(),
            'migration': {'status': status, 'phase': phase, 'processed': 10,
                          'total': 10, 'progress_percent': 100,
                          'completion_verified': status == 'completed'}}
    if status == 'completed':
        body['migration']['verification_sha256'] = 'a' * 64
    path = tmp_path / 'status.json'
    path.write_text(json.dumps(body))
    path.chmod(0o600)
    return path, body


@pytest.mark.parametrize('age,freshness', [(0, 'fresh'), (120, 'fresh'), (120.001, 'stale'),
                                         (-30, 'fresh'), (-30.001, 'clock_skew')])
def test_clock_boundaries(tmp_path, age, freshness):
    path, _ = publication(tmp_path, age=age)
    result, complete = load_runtime_status(path, now=NOW)
    assert result['freshness'] == freshness
    assert result['processed'] == 10 and result['progress_percent'] == 100
    assert complete is (False if freshness == 'fresh' else None)
    assert result['status'] == ('running' if freshness == 'fresh' else 'needs_attention')
    if freshness != 'fresh':
        assert result['phase'] == 'needs_attention'
        assert result['reported_phase'] == 'pilot' and result['reported_status'] == 'running'


@pytest.mark.parametrize('status', ['restoring', 'preparing', 'running', 'verifying',
                                   'paused', 'needs_attention', 'completed', 'not_reported'])
def test_every_status_expires_without_publisher_or_file_change(tmp_path, status):
    path, _ = publication(tmp_path, status=status)
    original = path.read_bytes()
    assert load_runtime_status(path, now=NOW)[1] is (status == 'completed')
    result, complete = load_runtime_status(path, now=NOW + timedelta(seconds=121))
    assert result['freshness'] == 'stale' and complete is None
    assert path.read_bytes() == original
    # Touching the file does not refresh its publication time.
    path.touch()
    assert load_runtime_status(path, now=NOW + timedelta(seconds=122))[0]['freshness'] == 'stale'
    publication(tmp_path, age=-122, status=status)
    assert load_runtime_status(path, now=NOW + timedelta(seconds=122))[0]['freshness'] == 'fresh'


@pytest.mark.parametrize('body', [None, [], 'text', 1, {'version': True},
    {'version': 1, 'updated_at': 123}, {'version': 1, 'updated_at': '2026-09-09'},
    {'version': 1, 'updated_at': NOW.isoformat(), 'migration': []},
    {'version': 1, 'updated_at': NOW.isoformat(), 'migration': {'status': []}}])
def test_malformed_publications_fail_closed_without_health_exception(tmp_path, body):
    path, _ = publication(tmp_path)
    path.write_text(json.dumps(body))
    result, complete = load_runtime_status(path, now=NOW)
    assert result['status'] == 'not_reported' and complete is None
    assert result['processed'] is None


def test_offset_and_no_private_metadata(tmp_path):
    path, body = publication(tmp_path)
    body['updated_at'] = '2026-09-09T03:00:00-03:00'
    body['private'] = {'error': 'SECRET-SYNTHETIC'}
    path.write_text(json.dumps(body))
    result, _ = load_runtime_status(path, now=NOW)
    assert result['freshness'] == 'fresh' and result['report_age_seconds'] == 0
    assert 'SECRET' not in json.dumps(result)


def test_missing_deleted_and_invalid_json(tmp_path):
    path, _ = publication(tmp_path)
    path.write_text('{')
    assert load_runtime_status(path, now=NOW)[1] is None
    path.unlink()
    assert load_runtime_status(path, now=NOW)[1] is None
    assert load_runtime_status(None, now=NOW)[1] is None

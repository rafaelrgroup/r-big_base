"""End-to-end synthetic input -> adapters -> actual PostgreSQL transactions."""
from pathlib import Path
import json
import os
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest

from bigbase.canonical_store import CanonicalStore, decode
from bigbase.migration_cli import execute_jsonl, main
from bigbase.migration_transport import JsonlSource, MigrationReadError


@pytest.fixture
def canonical():
    dsn = os.environ.get('BIGBASE_TEST_PG_DSN')
    if not dsn:
        pytest.skip('Explicit synthetic PostgreSQL fixture required')
    with psycopg.connect(dsn) as connection:
        identity = connection.execute("SELECT current_database(),current_setting('port'),inet_server_addr(),current_setting('server_version_num')::integer").fetchone()
        assert identity[:3] == ('bigbase_test', '18769', None)
        assert 180000 <= identity[3] < 190000
    store = CanonicalStore(dsn, schema='cbtest_' + uuid4().hex)
    store.initialize(environment='synthetic')
    try:
        yield store
    finally:
        with store.connection() as connection:
            connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(store.schema)))


def make_input(tmp_path, count=3, source='pessoas', *, document=None):
    path = tmp_path / (source + '.jsonl')
    records = [{'source_id': source, 'external_id': 'synthetic-' + str(i), 'source_version': 1,
                'record': {'NOME': 'Pessoa Sintética ' + str(i), 'unknown': [False, 0, None, {}],
                           **({'CPF': document} if document else {})}} for i in range(count)]
    path.write_text(''.join(json.dumps(row) + '\n' for row in records))
    return path


def load(path, store, *, expected=3, key='synthetic-attempt', source='pessoas', **kwargs):
    with JsonlSource(path, page_size=2) as reader:
        return execute_jsonl(reader, store, job_key=key, source_id=source, actor_id='synthetic-test',
                             expected_records=expected, synthetic=True, **kwargs)


def counts(store):
    with store.connection() as connection:
        return tuple(connection.execute('SELECT count(*) AS n FROM ' + name).fetchone()['n']
                     for name in ['entities', 'observations', 'operations', 'outbox'])


def test_complete_pipeline_and_completed_replay_are_lossless_and_private(canonical, tmp_path):
    path = make_input(tmp_path)
    report = tmp_path / 'result.json'
    result = load(path, canonical, report_path=report)
    assert result['state'] == 'completed' and result['records_processed'] == 3
    assert result['real_migration'] is False and result['observations_created'] == 15
    assert result['completion_verified'] is True and result['progress_percent'] == 100
    assert result['verification']['complete'] is True and result['verification']['records_checked']==3
    assert counts(canonical) == (3, 15, 3, 3)
    assert report.stat().st_mode & 0o777 == 0o600
    assert 'Pessoa Sintética' not in report.read_text() and 'source_record_id' not in report.read_text()
    replay = load(path, canonical)
    assert replay['replayed_completed_job'] and counts(canonical) == (3, 15, 3, 3)
    with canonical.connection() as connection:
        rows = connection.execute('SELECT input_json,input_type FROM observations').fetchall()
    assert {row['input_type'] for row in rows} == {'text', 'boolean', 'integer', 'null', 'empty_object'}
    assert any(row['input_json'] == 'false' for row in rows)
    assert any(row['input_json'] == '0' for row in rows)


def test_interrupted_commit_resumes_from_durable_cursor_without_duplication(canonical, tmp_path):
    path = make_input(tmp_path, count=5)

    class Interrupted:
        def __getattr__(self, name):
            return getattr(canonical, name)

        def apply_batch(self, records, **kwargs):
            if kwargs['expected_checkpoint'] == 2:
                raise RuntimeError('synthetic-interruption')
            return canonical.apply_batch(records, **kwargs)

    with pytest.raises(RuntimeError, match='synthetic-interruption'):
        load(path, Interrupted(), expected=5)
    assert counts(canonical) == (2, 10, 2, 2)
    with canonical.connection() as connection:
        checkpoint = connection.execute('SELECT checkpoint,cursor_json FROM migration_jobs').fetchone()
    assert checkpoint['checkpoint'] == 2 and decode(checkpoint['cursor_json'])['seen'] == 2
    fresh = CanonicalStore(canonical.dsn, schema=canonical.schema)
    result = load(path, fresh, expected=5)
    assert result['state'] == 'completed' and result['records_processed'] == 5
    assert counts(canonical) == (5, 25, 5, 5)


def test_cross_source_document_dedup_preserves_both_sources_and_reprocessing(canonical, tmp_path):
    first = make_input(tmp_path, count=1, document='529.982.247-25')
    second = make_input(tmp_path, count=1, source='pessoas_serasa', document='52998224725')
    load(first, canonical, expected=1, key='first')
    load(second, canonical, expected=1, key='second', source='pessoas_serasa')
    assert counts(canonical) == (1, 12, 2, 2)
    a = canonical.lookup_identity(source_id='pessoas', source_record_id='synthetic-0')
    b = canonical.lookup_identity(source_id='pessoas_serasa', source_record_id='synthetic-0')
    assert a == b and a is not None
    load(first, canonical, expected=1, key='explicit-new-attempt')
    assert counts(canonical) == (1, 12, 2, 2)


def test_count_failure_does_not_falsely_complete_job(canonical, tmp_path):
    with pytest.raises(MigrationReadError, match='COUNT_RECONCILIATION_FAILED'):
        load(make_input(tmp_path), canonical, expected=4)
    with canonical.connection() as connection:
        job = connection.execute('SELECT status,checkpoint,records_processed FROM migration_jobs').fetchone()
    assert job['status'] != 'completed' and job['checkpoint'] == job['records_processed'] == 3


def test_changed_input_job_contract_and_cancellation(canonical, tmp_path):
    path = make_input(tmp_path)
    result = load(path, canonical, cancelled=lambda: True)
    assert result['state'] == 'cancelled' and counts(canonical) == (0, 0, 0, 0)
    with pytest.raises(MigrationReadError, match='TERMINAL_JOB'):
        load(path, canonical)
    path.write_text(path.read_text().replace('Pessoa Sintética', 'Outra Sintética'))
    with pytest.raises(Exception):
        # Existing job key is immutable even if its old run wrote zero rows.
        load(path, canonical)
    assert counts(canonical) == (0, 0, 0, 0)


def test_source_mismatch_never_commits_mixed_page(canonical, tmp_path):
    path = make_input(tmp_path)
    lines = path.read_text().splitlines()
    second = json.loads(lines[1]); second['source_id'] = 'pessoas_serasa'
    path.write_text(lines[0] + '\n' + json.dumps(second) + '\n')
    with pytest.raises(MigrationReadError, match='SOURCE_INDEX_CHANGED_WITHIN_JOB'):
        load(path, canonical, expected=2)
    assert counts(canonical) == (0, 0, 0, 0)


def test_decimal_token_survives_transport_adapter_and_storage(canonical, tmp_path):
    path = tmp_path / 'decimal.jsonl'
    path.write_text('{"source_id":"pessoas_serasa","external_id":"synthetic-exact","record":{"RENDA":1.2300000000000000000001e+30,"odd":"a\\u0000b","large":90071992547409931234567890}}\n')
    load(path, canonical, expected=1, source='pessoas_serasa')
    with canonical.connection() as connection:
        rows = connection.execute('SELECT input_json FROM observations').fetchall()
    assert {row['input_json'] for row in rows} == {'1.2300000000000000000001e+30', '"a\\u0000b"', '90071992547409931234567890'}


def test_synthetic_entrypoint_never_accepts_a_real_destination(tmp_path):
    class WrongDestination:
        def deployment_info(self):
            return {'environment': 'production'}
        def create_job(self, *args, **kwargs):
            pytest.fail('Must refuse before writes')
    with pytest.raises(MigrationReadError, match='SYNTHETIC_DESTINATION_REQUIRED'):
        load(make_input(tmp_path), WrongDestination())


def test_cli_missing_credentials_sanitized_and_real_attempt_not_reported_synthetic(tmp_path, monkeypatch, capsys):
    inputs = tmp_path / 'inputs.json'; inputs.write_text('{}')
    report = tmp_path / 'report.json'
    monkeypatch.delenv('BIGBASE_MIGRATION_DSN', raising=False)
    result = main(['full-elasticsearch', '--inputs', str(inputs), '--source-url', 'https://synthetic.invalid',
                  '--source-id', 'pessoas', '--schema', 'fixture', '--job-key', 'test', '--report', str(report)])
    assert result == 1
    output = json.loads(report.read_text())
    assert output['real_migration'] is True and output['completion_verified'] is False
    assert output['error_code'] == 'BIGBASE_MIGRATION_DSN_REQUIRED'
    assert 'Traceback' not in capsys.readouterr().err


def test_progress_file_never_claims_completion_before_final_reconciliation(canonical, tmp_path, monkeypatch):
    from bigbase import migration_cli
    outputs=[]
    original=migration_cli._write_report
    def observe(path, result):
        outputs.append(dict(result))
        original(path,result)
    monkeypatch.setattr(migration_cli,'_write_report',observe)
    load(make_input(tmp_path),canonical,report_path=tmp_path/'progress.json')
    assert len(outputs)>=2 and outputs[0]['state']=='processing'
    assert outputs[0]['records_processed']==2 and outputs[0]['progress_percent']<100
    assert outputs[0]['completion_verified'] is False
    assert outputs[-1]['completion_verified'] is True and outputs[-1]['progress_percent']==100
    assert any(row['state']=='verifying' and row['records_verified']==0 for row in outputs)


def test_destination_reconciliation_failure_never_completes_job(canonical,tmp_path,monkeypatch):
    from bigbase import canonical_reconciliation
    monkeypatch.setattr(canonical_reconciliation,'reconcile_reader',lambda *args,**kwargs:
        {'state':'failed','complete':False,'passed':False,'records_checked':3,'divergences':1})
    with pytest.raises(MigrationReadError,match='DESTINATION_RECONCILIATION_FAILED'):
        load(make_input(tmp_path),canonical)
    with canonical.connection() as connection:
        job=connection.execute('SELECT status,checkpoint FROM migration_jobs').fetchone()
    assert job['status']=='processing' and job['checkpoint']==3
    assert counts(canonical)==(3,15,3,3)


def test_verification_cancellation_is_explicit_without_erasing_commits(canonical,tmp_path,monkeypatch):
    from bigbase import canonical_reconciliation
    monkeypatch.setattr(canonical_reconciliation,'reconcile_reader',lambda *args,**kwargs:
        {'state':'partial','reason':'cancelled','complete':False,'passed':False,'records_checked':1,'divergences':0})
    result=load(make_input(tmp_path),canonical)
    assert result['state']=='cancelled' and result['completion_verified'] is False
    assert counts(canonical)==(3,15,3,3)
    with canonical.connection() as connection:
        job=connection.execute('SELECT status,checkpoint FROM migration_jobs').fetchone()
    assert job['status']=='cancelled' and job['checkpoint']==3

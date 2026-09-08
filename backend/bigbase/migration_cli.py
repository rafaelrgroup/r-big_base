"""Explicit migration entry points; imports are inert and secrets use env/files."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time

from .migration_transport import ElasticsearchPitSource, JsonlSource, MigrationReadError, transfer_pages
from .source_adapters import ADAPTER_VERSION, map_record
from .domain import NORMALIZER_VERSION


def _write_report(path, result):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp.' + str(os.getpid()))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump(result, output, ensure_ascii=True, indent=2)
        output.write('\n')
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def execute_source(reader, store, *, job_key, source_id, actor_id, expected_records,
                   synthetic, report_path=None, cancelled=lambda: False, approval=None,
                   snapshot_uuid=None):
    """One canonical path; full real import requires the evaluated preflight."""
    info = store.deployment_info()
    if synthetic:
        if info.get('environment') != 'synthetic' or reader.identity.get('kind') != 'jsonl':
            raise MigrationReadError('SYNTHETIC_DESTINATION_REQUIRED')
    elif (info.get('environment') not in {'staging', 'production'}
          or not approval or approval.get('ready') is not True
          or approval.get('phase') != 'full' or approval.get('ready_for_full_import') is not True
          or not snapshot_uuid or reader.identity.get('kind') != 'elasticsearch_pit'
          or reader.require_immutable is not True):
        raise MigrationReadError('FULL_MIGRATION_PREFLIGHT_REQUIRED')
    if type(expected_records) is not int or expected_records < 0:
        raise ValueError('expected_records must be a nonnegative integer')
    if source_id not in {'pessoas', 'pessoas_serasa'}:
        raise ValueError('Unknown source index')
    metadata = {'source': reader.identity, 'expected_records': expected_records,
                'dataset_kind': 'synthetic' if synthetic else 'real', 'snapshot_uuid': snapshot_uuid,
                'adapter_version': ADAPTER_VERSION, 'normalizer_version': NORMALIZER_VERSION,
                'completion_contract': 'source_destination_reconciliation_v1',
                'destination_deployment_id': str(info.get('deployment_id'))}
    job = store.create_job(job_key, source_id, metadata=metadata)
    if job.get('metadata') != metadata:
        raise MigrationReadError('JOB_SOURCE_CONTRACT_CHANGED')
    if job['status'] == 'completed':
        if job['checkpoint'] != expected_records or job['records_processed'] != expected_records:
            raise MigrationReadError('COMPLETED_JOB_COUNT_MISMATCH')
        verified = job.get('verification') or {}
        if (verified.get('state')!='verified' or verified.get('passed') is not True
                or verified.get('complete') is not True or verified.get('records_checked')!=expected_records
                or verified.get('divergences')!=0):
            raise MigrationReadError('COMPLETED_JOB_VERIFICATION_MISSING')
        result = {'state': 'completed', 'job_id': str(job['id']),
                  'records_processed': job['records_processed'], 'replayed_completed_job': True,
                  'expected_records': expected_records, 'completion_verified': True, 'progress_percent': 100,
                  'count_unit': 'source_records_not_unique_people',
                  'verification': verified,
                  'real_migration': not synthetic}
        if report_path:
            _write_report(report_path, result)
        return result
    if job['status'] in {'failed', 'cancelled'}:
        raise MigrationReadError('TERMINAL_JOB_REQUIRES_EXPLICIT_NEW_ATTEMPT')
    cursor = job.get('cursor')
    if (cursor or {}).get('seen', 0) != job['checkpoint']:
        raise MigrationReadError('CHECKPOINT_CURSOR_MISMATCH')

    def prepare(source, external_id, record, source_version=None):
        if source != source_id:
            raise MigrationReadError('SOURCE_INDEX_CHANGED_WITHIN_JOB')
        mapped = map_record(source, external_id, record, source_version=source_version)
        if mapped.get('coverage', {}).get('passed') is not True:
            raise MigrationReadError('SOURCE_FIELD_RECONCILIATION_FAILED')
        return mapped

    started, last_report = time.monotonic(), [0.0]
    initial_count = job['records_processed']
    def progress(count, receipt):
        if not report_path:
            return
        at = time.monotonic()
        if last_report[0] and at - last_report[0] < 10:
            return
        elapsed = max(at - started, 0.001)
        _write_report(report_path, {'state': 'processing', 'job_id': str(job['id']),
            'records_processed': count, 'expected_records': expected_records,
            'progress_percent': min(99.9, round(100 * count / expected_records, 3)) if expected_records else 0,
            'records_per_second_this_attempt': round((count-initial_count)/elapsed, 2),
            'count_unit': 'source_records_not_unique_people', 'completion_verified': False,
            'real_migration': not synthetic, 'checked_at': datetime.now(timezone.utc).isoformat()})
        last_report[0] = at

    result = transfer_pages(reader, store, job['id'], checkpoint=cursor, mapper=prepare,
                            actor_id=actor_id, cancelled=cancelled, progress=progress)
    after = store.get_job(job['id'])
    if result['state'] == 'cancelled':
        store.finish_job(job['id'], expected_checkpoint=after['checkpoint'], status='cancelled')
    elif (result['state'] == 'source_exhausted' and result.get('records_processed') == expected_records
          and after['checkpoint'] == expected_records and after['records_processed'] == expected_records):
        from .canonical_reconciliation import reconcile_reader
        last_verified_report = [0.0]
        def verification_progress(checked, divergences):
            if not report_path:
                return
            at=time.monotonic()
            if last_verified_report[0] and at-last_verified_report[0]<10:
                return
            _write_report(report_path, {'state':'verifying','job_id':str(job['id']),
                'records_processed':after['records_processed'],'expected_records':expected_records,
                'records_verified':checked,'divergences':divergences,
                'verification_percent':round(100*checked/expected_records,3) if expected_records else 0,
                'progress_percent':99.9,'completion_verified':False,'real_migration':not synthetic,
                'count_unit':'source_records_not_unique_people','checked_at':datetime.now(timezone.utc).isoformat()})
            last_verified_report[0]=at
        verification_progress(0,0)
        reader.rewind()
        verification=reconcile_reader(reader,store,expected_records=expected_records,
                                      expected_actor_id=None,cancelled=cancelled,progress=verification_progress)
        if verification.get('reason')=='cancelled':
            store.finish_job(job['id'],expected_checkpoint=expected_records,status='cancelled')
            result['state']='cancelled'
        elif (verification.get('state')!='verified' or verification.get('complete') is not True
                or verification.get('passed') is not True or verification.get('records_checked')!=expected_records
                or verification.get('divergences')!=0):
            raise MigrationReadError('DESTINATION_RECONCILIATION_FAILED')
        else:
            store.finish_job(job['id'], expected_checkpoint=expected_records, status='completed', verification=verification)
            result['state'] = 'completed'
        result['verification'] = verification
    else:
        raise MigrationReadError('SOURCE_RECORD_COUNT_RECONCILIATION_FAILED')
    result = {'state': result['state'], 'job_id': str(job['id']),
              'records_processed': after['records_processed'], 'expected_records': expected_records,
              'observations_created': after.get('observations_created'),
              'source_sha256': reader.identity.get('sha256'), 'real_migration': not synthetic,
              'completion_verified': result['state'] == 'completed',
              'progress_percent': 100 if result['state'] == 'completed' else min(99.9, round(100*after['records_processed']/expected_records,3)) if expected_records else 0,
              'count_unit': 'source_records_not_unique_people',
              'verification': result.get('verification'),
              'checked_at': datetime.now(timezone.utc).isoformat()}
    if report_path:
        _write_report(report_path, result)
    return result


def execute_jsonl(reader, store, **kwargs):
    if kwargs.get('synthetic') is not True:
        raise MigrationReadError('JSONL_ENTRYPOINT_REQUIRES_SYNTHETIC_DESTINATION')
    return execute_source(reader, store, **kwargs)


def main(argv=None):
    parser = argparse.ArgumentParser(description='BIG BASE — migração verificável, sem carga implícita')
    sub = parser.add_subparsers(dest='action', required=True)
    load = sub.add_parser('synthetic-jsonl', help='Validar o pipeline apenas no PostgreSQL sintético')
    load.add_argument('--input', required=True)
    load.add_argument('--schema', required=True)
    load.add_argument('--job-key', required=True)
    load.add_argument('--source-id', choices=['pessoas', 'pessoas_serasa'], required=True)
    load.add_argument('--expected-records', type=int, required=True)
    load.add_argument('--page-size', type=int, default=100)
    load.add_argument('--report', required=True)
    load.add_argument('--initialize', action='store_true', help='Criar explicitamente schema de ensaio ausente')
    check = sub.add_parser('preflight', help='Avaliar pré-condições de carga real sem ler registros ou escrever no banco')
    check.add_argument('--inputs', required=True, help='JSON com manifest, restore, destination, pilot e source')
    check.add_argument('--report', required=True)
    real = sub.add_parser('full-elasticsearch', help='Carga completa somente com preflight full aprovado e destino existente')
    real.add_argument('--inputs', required=True)
    real.add_argument('--source-url', required=True)
    real.add_argument('--source-id', choices=['pessoas', 'pessoas_serasa'], required=True)
    real.add_argument('--schema', required=True)
    real.add_argument('--job-key', required=True)
    real.add_argument('--page-size', type=int, default=100)
    real.add_argument('--report', required=True)
    args = parser.parse_args(argv)
    try:
        if args.action in {'preflight', 'full-elasticsearch'}:
            from .migration_preflight import assess_migration
            inputs = json.loads(Path(args.inputs).read_text())
            if args.action == 'full-elasticsearch':
                from .canonical_store import CanonicalStore
                dsn = os.environ.get('BIGBASE_MIGRATION_DSN')
                if not dsn:
                    raise MigrationReadError('BIGBASE_MIGRATION_DSN_REQUIRED')
                store = CanonicalStore(dsn, schema=args.schema)
                actual = store.deployment_info()
                declared = inputs.get('destination') or {}
                if (str(actual.get('deployment_id')) != declared.get('expected_deployment_id')
                        or actual.get('environment') != declared.get('environment')
                        or actual.get('schema') != args.schema):
                    raise MigrationReadError('DESTINATION_IDENTITY_CHANGED')
                inputs['destination'] = {**declared, **actual}
            result = assess_migration(inputs.get('manifest'), inputs.get('restore'),
                                      inputs.get('destination'), pilot=inputs.get('pilot'), source_info=inputs.get('source'))
            if args.action == 'preflight' or result.get('ready_for_full_import') is not True:
                _write_report(args.report, result)
                print(json.dumps(result, ensure_ascii=True))
                return 0 if result['ready'] and args.action == 'preflight' else 2
            source_info = inputs['source']
            chosen = [row for row in source_info['indices'] if row['source_id'] == args.source_id]
            if len(chosen) != 1:
                raise MigrationReadError('SOURCE_INDEX_NOT_APPROVED')
            chosen = chosen[0]
            headers = {}
            if os.environ.get('BIGBASE_SOURCE_AUTHORIZATION'):
                headers['Authorization'] = os.environ['BIGBASE_SOURCE_AUTHORIZATION']
            with ElasticsearchPitSource(args.source_url, chosen['index'], args.source_id,
                    expected_cluster_uuid=source_info['expected_cluster_uuid'],
                    expected_index_uuid=chosen['expected_index_uuid'], page_size=args.page_size,
                    headers=headers, require_immutable=True, expected_count=chosen['count'],
                    expected_mapping_sha256=chosen['mapping_sha256']) as reader:
                loaded = execute_source(reader, store, job_key=args.job_key, source_id=args.source_id,
                    actor_id='approved-migration-cli', expected_records=chosen['count'], synthetic=False,
                    approval=result, snapshot_uuid=inputs['restore']['snapshot_uuid'], report_path=args.report)
            print(json.dumps(loaded, ensure_ascii=True))
            return 0
        from .canonical_store import CanonicalStore
        dsn = os.environ.get('BIGBASE_TEST_PG_DSN')
        if not dsn:
            raise MigrationReadError('BIGBASE_TEST_PG_DSN_REQUIRED')
        store = CanonicalStore(dsn, schema=args.schema)
        if args.initialize:
            store.initialize(environment='synthetic')
        with JsonlSource(args.input, page_size=args.page_size) as reader:
            result = execute_jsonl(reader, store, job_key=args.job_key, source_id=args.source_id,
                                   actor_id='synthetic-migration-cli', expected_records=args.expected_records,
                                   synthetic=True, report_path=args.report)
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except Exception as exc:
        # Database/HTTP exceptions can carry SQL, DSNs or source values. Keep
        # console/report output operational; detailed records stay in storage.
        error = str(exc) if isinstance(exc, MigrationReadError) else type(exc).__name__
        result = {'state': 'interrupted', 'error_code': error,
                  'real_migration': args.action == 'full-elasticsearch',
                  'completion_verified': False, 'checkpoint_preserved': True,
                  'checked_at': datetime.now(timezone.utc).isoformat()}
        _write_report(args.report, result)
        print(json.dumps(result, ensure_ascii=True), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

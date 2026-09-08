"""Private, run-owned PostgreSQL schema for the canonical browser flow only."""
import json
import os
import re
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from bigbase.api import create_app as application
from bigbase.canonical_http import CanonicalReads, validate_synthetic_dsn
from bigbase.canonical_store import CanonicalStore, digest

ROOT = Path(__file__).resolve().parents[1]


def repository():
    schema = os.environ.get('BIGBASE_CANONICAL_BROWSER_SCHEMA', '')
    if not re.fullmatch(r'cbbrowser_[a-f0-9]{32}', schema):
        raise RuntimeError('Run-owned canonical browser schema required')
    dsn = os.environ.get('BIGBASE_TEST_PG_DSN', '')
    validate_synthetic_dsn(dsn)
    with psycopg.connect(dsn) as connection:
        row = connection.execute("SELECT current_database(),current_setting('port'),inet_server_addr(),current_setting('server_version_num')::integer").fetchone()
        if row[:3] != ('bigbase_test', '18769', None) or not 180000 <= row[3] < 190000:
            raise RuntimeError('Private synthetic PostgreSQL identity mismatch')
    return CanonicalStore(dsn, schema)


def prepare():
    store = repository()
    store.initialize(environment='synthetic')
    source = 'browser-canonical-synthetic'
    work = store.create_job(uuid4().hex, source)
    values = [None, False, 0, Decimal('12345678901234567890.12345678901234567890'), 'NUL\x00preservado']
    values.extend('Valor sintético '+str(i) for i in range(20))
    facts = []
    for i, value in enumerate(values):
        input_type = 'null' if value is None else 'boolean' if type(value) is bool else 'integer' if type(value) is int else 'decimal' if isinstance(value, Decimal) else 'text'
        facts.append({'id':str(i),'source_path':'/synthetic/'+str(i),'target_path':'campo_'+str(i),
                      'target_kind':'custom','item_key':'item_'+str(i),'input_value':value,'input_type':input_type,
                      'normalized_value':value,'status':'unknown','observed_at':'2026-01-01T00:00:00Z'})
    facts[1]['flags'] = {'valid': {'value':False}, 'is_whatsapp': {'value':None}}
    record = {'source_id':source,'source_record_id':'person-1','entity_type':'person','adapter_version':'browser-synthetic-v1',
              'facts':facts,'containers':[{'source_path':'','type':'object','length':len(facts)}]}
    record['record_hash'] = digest(record)
    first = store.apply_batch([record],job_id=work['id'],expected_checkpoint=0,next_checkpoint=1,actor_id='browser-synthetic')
    company = {**record,'source_record_id':'company-1','entity_type':'company','facts':facts[:1]}
    company['record_hash'] = digest(company)
    second = store.apply_batch([company],job_id=work['id'],expected_checkpoint=1,next_checkpoint=2,actor_id='browser-synthetic')
    path = ROOT/'var/browser-test/canonical-fixture.json'
    path.write_text(json.dumps({'source_id':source,'person_id':first['entity_ids'][0], 'company_id':second['entity_ids'][0]}))
    path.chmod(0o600)


def cleanup():
    store = repository()
    with store.connection() as connection:
        connection.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(store.schema)))


def create_app():
    store = repository()
    reads = CanonicalReads(store,expected_deployment_id=store.deployment_info()['deployment_id'])
    return application(ROOT/'var/browser-test', canonical_reads=reads)


if __name__ == '__main__':
    import sys
    if sys.argv[1:] == ['prepare']: prepare()
    elif sys.argv[1:] == ['cleanup']: cleanup()
    else: raise SystemExit('Use prepare or cleanup')

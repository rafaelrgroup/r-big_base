import os
from uuid import uuid4
import pytest
import psycopg
from psycopg import sql
from bigbase.source_adapters import map_record
from postgres_block_experiment import BlockExperiment,CodecError
from wire_codec import dumps
from synthetic_samples import record


@pytest.fixture
def store():
    dsn=os.environ.get('BLOCK_EXPERIMENT_DSN')
    if not dsn:pytest.skip('Explicit fictitious PostgreSQL fixture required')
    repo=BlockExperiment(dsn,'blockexp_'+uuid4().hex);repo.initialize()
    try:yield repo
    finally:
        with repo.connection() as c:c.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(repo.schema)))


def records():return [map_record('pessoas',str(n),record('nested',n),source_version='1:'+str(n)) for n in range(5)]


def test_restart_and_lost_response_replay_preserve_all_atoms(store):
    job,request=uuid4(),uuid4();mapped=records()
    first=store.append(job,request,mapped,expected_cursor=None,next_cursor={'sequence':5},actor='synthetic-worker')
    restarted=BlockExperiment(store.dsn,store.schema)
    again=restarted.append(job,request,mapped,expected_cursor=None,next_cursor={'sequence':5},actor='synthetic-worker')
    assert first=={'processed':5,'replayed':False} and again=={'processed':5,'replayed':True}
    for n,wanted in enumerate(mapped):
        row=restarted.get(job,n);assert dumps(row['record'])==dumps(wanted) and row['actor']=='synthetic-worker' and row['received_at'].tzinfo
    with store.connection() as c:assert c.execute('SELECT count(*) AS n FROM positions').fetchone()['n']==5


def test_changed_replay_fails_and_does_not_advance(store):
    job,request=uuid4(),uuid4();mapped=records()
    store.append(job,request,mapped,expected_cursor=None,next_cursor=5,actor='synthetic-worker')
    with pytest.raises(CodecError,match='IDEMPOTENCY'):
        store.append(job,request,mapped,expected_cursor=None,next_cursor=6,actor='synthetic-worker')
    with pytest.raises(CodecError,match='CHECKPOINT'):
        store.append(job,uuid4(),mapped,expected_cursor=None,next_cursor=10,actor='synthetic-worker')
    with store.connection() as c:assert c.execute('SELECT processed FROM checkpoints').fetchone()['processed']==5


def test_failure_mid_copy_rolls_back_blocks_positions_and_checkpoint(store):
    job=uuid4();mapped=records()
    with store.connection() as c:
        c.execute("CREATE FUNCTION reject_third() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.slot=2 THEN RAISE EXCEPTION 'synthetic failure'; END IF; RETURN NEW; END $$")
        c.execute('CREATE TRIGGER fail_copy BEFORE INSERT ON positions FOR EACH ROW EXECUTE FUNCTION reject_third()')
    with pytest.raises(psycopg.errors.RaiseException):
        store.append(job,uuid4(),mapped,expected_cursor=None,next_cursor=5,actor='synthetic-worker')
    with store.connection() as c:
        for table in ['blocks','positions','checkpoints','receipts']:
            assert c.execute(sql.SQL('SELECT count(*) AS n FROM {}').format(sql.Identifier(table))).fetchone()['n']==0


def test_old_block_and_observation_receipts_are_immutable(store):
    store.append(uuid4(),uuid4(),records(),expected_cursor=None,next_cursor=5,actor='synthetic-worker')
    for table in ['blocks','positions','receipts']:
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            with store.connection() as c:c.execute(sql.SQL('DELETE FROM {}').format(sql.Identifier(table)))


def test_two_batches_keep_old_records_and_allocate_distinct_positions(store):
    job=uuid4();mapped=records()
    store.append(job,uuid4(),mapped,expected_cursor=None,next_cursor=5,actor='first')
    store.append(job,uuid4(),mapped,expected_cursor=5,next_cursor=10,actor='second')
    assert store.get(job,0)['actor']=='first' and store.get(job,5)['actor']=='second'
    assert store.size()['bytes']>=store.size()['index_bytes']>0

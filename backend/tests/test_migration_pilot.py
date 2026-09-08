import copy
import json

import pytest

from bigbase.migration_pilot import PilotBudget, PilotLimitReached, PilotLimits, execute_pilot_source
from bigbase.migration_transport import JsonlSource, MigrationReadError


def plan(**changes):
    return {'status': 'PLANNED', 'record_limit': 4, 'max_batch_records': 2,
            'max_duration_seconds': 60, 'volume_budgets': [{'volume_id': 'synthetic-disk', 'max_bytes': 1000}], **changes}


def probe(used=0, free=5000, reservation=100):
    return {'synthetic-disk': {'allocated_bytes': used, 'free_bytes': free, 'next_batch_reservation_bytes': reservation}}


class Store:
    def __init__(self):
        self.batches = []; self.completed = False
    def deployment_info(self):
        return {'environment': 'synthetic', 'deployment_id': 'synthetic-id'}
    def create_job(self, key, source, metadata):
        self.job = {'id': 'synthetic-job', 'status': 'pending', 'checkpoint': 0,
                    'records_processed': 0, 'cursor': None, 'metadata': copy.deepcopy(metadata)}
        return self.job
    def apply_batch(self, records, **kwargs):
        assert kwargs['expected_checkpoint'] == self.job['checkpoint']
        self.batches.append((records, kwargs))
        self.job.update(checkpoint=kwargs['next_checkpoint'], records_processed=kwargs['next_checkpoint'],
                        cursor=kwargs['next_cursor'], status='processing')
        return {}
    def get_job(self, key):
        return self.job
    def finish_job(self, *args, **kwargs):
        pytest.fail('Pilot ingestion must not mark full migration complete')


def input_file(tmp_path, count=6):
    path = tmp_path / 'synthetic.jsonl'
    path.write_text(''.join(json.dumps({'source_id':'pessoas','external_id':'test-'+str(i),
                                     'record':{'NOME':'Pessoa Sintética','false':False}})+'\n' for i in range(count)))
    return path


def run(tmp_path, *, store=None, measurement=lambda: probe(), count=6, page_size=2, **kwargs):
    store = store or Store()
    with JsonlSource(input_file(tmp_path,count),page_size=page_size) as reader:
        return execute_pilot_source(reader,store,plan=plan(),probe=measurement,job_key='test',
            source_id='pessoas',actor_id='synthetic-pilot-test',synthetic=True,**kwargs), store


@pytest.mark.parametrize('change',[{'record_limit':True},{'record_limit':1000001},{'max_batch_records':0},
                                 {'max_duration_seconds':0},{'volume_budgets':[]},
                                 {'volume_budgets':[{'volume_id':'d','max_bytes':True}]}])
def test_pilot_limits_are_explicit_and_strict(change):
    with pytest.raises(ValueError):
        PilotLimits.from_plan(plan(**change))


def test_pilot_caps_records_without_skipping_page_cursor_or_claiming_full_completion(tmp_path):
    result, store = run(tmp_path)
    assert result['state'] == 'pilot_ingested' and result['reason'] == 'record_limit'
    assert result['records_processed'] == 4 and result['input_leaves_verified'] == 8
    assert len(store.batches) == 2 and store.job['checkpoint'] == store.job['cursor']['seen'] == 4
    assert store.job['cursor']['complete'] is False
    assert result['full_migration_complete'] is result['representative_sample_verified'] is False
    assert result['destination_reconciliation_complete'] is False


def test_partial_page_is_never_trimmed_with_the_wrong_resume_cursor(tmp_path):
    with pytest.raises(ValueError,match='divide'):
        run(tmp_path,page_size=3)


def test_small_source_eof_is_a_pilot_receipt_only(tmp_path):
    result, store = run(tmp_path,count=3)
    assert result['reason'] == 'source_exhausted' and result['records_processed'] == 3
    assert store.job['status'] == 'processing'


def test_missing_volume_or_invalid_measurement_fails_closed_before_writes(tmp_path):
    for sample in ({},probe(used=False),probe(reservation=0)):
        store = Store()
        if sample == probe(reservation=0):
            result, _ = run(tmp_path,store=store,measurement=lambda: sample)
            assert result['reason'] == 'PILOT_NEXT_BATCH_NOT_RESERVED'
        else:
            with pytest.raises(PilotLimitReached):
                run(tmp_path,store=store,measurement=lambda: sample)
        assert store.batches == []


def test_capacity_drop_after_first_commit_preserves_checkpoint_and_stops(tmp_path):
    store = Store()
    result, _ = run(tmp_path,store=store,measurement=lambda: probe(used=1100 if store.batches else 0))
    assert result['state'] == 'pilot_stopped' and result['reason'] == 'PILOT_VOLUME_BUDGET_LIMIT'
    assert result['records_processed'] == 2 and len(store.batches) == 1
    assert store.job['cursor']['seen'] == 2


def test_time_and_cancel_stop_before_any_new_commit(tmp_path):
    ticks = iter([0,0,61,61])
    result, store = run(tmp_path,clock=lambda: next(ticks))
    assert result['reason'] == 'PILOT_DURATION_LIMIT' and store.batches == []
    result, store = run(tmp_path,cancelled=lambda:True)
    assert result['reason'] == 'PILOT_CANCELLED' and store.batches == []


def test_budget_retains_half_budget_free_and_requires_reserved_next_batch():
    for sample in [probe(free=499),probe(used=999,reservation=2),probe(reservation=0),probe(free=550,reservation=100)]:
        guard = PilotBudget(PilotLimits.from_plan(plan()),lambda:sample)
        with pytest.raises(PilotLimitReached):
            guard.check(before_commit=True)


def test_real_pilot_rejects_synthetic_database_and_full_approval(tmp_path):
    with JsonlSource(input_file(tmp_path)) as reader:
        with pytest.raises(MigrationReadError,match='PILOT_PREFLIGHT_REQUIRED'):
            execute_pilot_source(reader,Store(),plan=plan(),probe=lambda:probe(),job_key='test',
                source_id='pessoas',actor_id='synthetic-test',synthetic=False,
                approval={'ready':True,'phase':'full','ready_for_full_import':True},snapshot_uuid='test')

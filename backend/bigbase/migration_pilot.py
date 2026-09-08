"""Bounded pilot orchestration; no default connections or production approval.

This module deliberately does not manufacture the representative sample or disk
measurements required to approve a full migration. Its receipt proves only the
bounded ingestion and source-leaf reconciliation that it actually performs.
"""
from dataclasses import dataclass
import hashlib
import json
import time

from .domain import NORMALIZER_VERSION
from .migration_transport import MigrationReadError, prepare_page
from .source_adapters import ADAPTER_VERSION, map_record


class PilotLimitReached(MigrationReadError):
    pass


@dataclass(frozen=True)
class PilotLimits:
    records: int
    batch_records: int
    duration_seconds: int
    volume_budgets: tuple

    @classmethod
    def from_plan(cls, plan):
        if not isinstance(plan, dict) or plan.get('status') != 'PLANNED':
            raise ValueError('A bounded pilot plan is required')
        keys = [('record_limit', 1, 1000000), ('max_batch_records', 1, 1000), ('max_duration_seconds', 1, 86400)]
        for key, low, high in keys:
            if type(plan.get(key)) is not int or not low <= plan[key] <= high:
                raise ValueError('Invalid pilot limit')
        budgets = plan.get('volume_budgets')
        if not isinstance(budgets, list) or not budgets:
            raise ValueError('Destination volume measurements are required')
        seen, values = set(), []
        for row in budgets:
            if (not isinstance(row, dict) or not isinstance(row.get('volume_id'), str)
                    or not row['volume_id'] or row['volume_id'] in seen
                    or type(row.get('max_bytes')) is not int or row['max_bytes'] < 1):
                raise ValueError('Invalid volume budget')
            seen.add(row['volume_id']); values.append((row['volume_id'], row['max_bytes']))
        return cls(plan['record_limit'], plan['max_batch_records'], plan['max_duration_seconds'], tuple(sorted(values)))


class PilotBudget:
    """A destination-owned probe is mandatory, even for synthetic rehearsals.

The probe returns cumulative allocated bytes since job creation, available bytes
and a bound/reservation for the next batch on *every* budgeted volume. A probe
that cannot establish a reservation must fail; arbitrary estimates are not a
storage guarantee. The executor does not reserve remote disks by itself.
"""
    def __init__(self, limits, probe, *, clock=time.monotonic):
        self.limits, self.probe, self.clock = limits, probe, clock
        self.started = clock()
        self.peak_bytes = dict.fromkeys(dict(limits.volume_budgets), 0)

    def check(self, *, before_commit=False):
        if self.clock() - self.started >= self.limits.duration_seconds:
            raise PilotLimitReached('PILOT_DURATION_LIMIT')
        sample = self.probe()
        if not isinstance(sample, dict) or set(sample) != set(self.peak_bytes):
            raise PilotLimitReached('PILOT_VOLUME_MEASUREMENT_UNAVAILABLE')
        for volume, budget in self.limits.volume_budgets:
            row = sample[volume]
            if not isinstance(row, dict) or any(type(row.get(key)) is not int or row[key] < 0
                                               for key in ['allocated_bytes', 'free_bytes', 'next_batch_reservation_bytes']):
                raise PilotLimitReached('PILOT_VOLUME_MEASUREMENT_INVALID')
            used, free, reservation = (row[key] for key in ['allocated_bytes', 'free_bytes', 'next_batch_reservation_bytes'])
            self.peak_bytes[volume] = max(self.peak_bytes[volume], used)
            if used > budget or free < (budget + 1) // 2:
                raise PilotLimitReached('PILOT_VOLUME_BUDGET_LIMIT')
            if before_commit and (reservation <= 0 or used + reservation > budget or free < reservation + (budget + 1) // 2):
                raise PilotLimitReached('PILOT_NEXT_BATCH_NOT_RESERVED')
        return sample


def execute_pilot_source(reader, store, *, plan, probe, job_key, source_id, actor_id,
                         synthetic=False, approval=None, snapshot_uuid=None,
                         cancelled=lambda: False, clock=time.monotonic):
    limits = PilotLimits.from_plan(plan)
    info = store.deployment_info()
    if synthetic:
        if info.get('environment') != 'synthetic' or reader.identity.get('kind') != 'jsonl':
            raise MigrationReadError('SYNTHETIC_DESTINATION_REQUIRED')
    elif (info.get('environment') not in {'staging', 'production'} or not approval
          or approval.get('ready') is not True or approval.get('phase') != 'pilot'
          or approval.get('ready_for_full_import') is not False or not snapshot_uuid
          or reader.identity.get('kind') != 'elasticsearch_pit' or reader.require_immutable is not True):
        raise MigrationReadError('PILOT_PREFLIGHT_REQUIRED')
    # The transport cursor belongs to the complete page. Never slice a page and
    # retain its final cursor: it would silently skip the omitted source rows.
    if reader.page_size > limits.batch_records or limits.records % reader.page_size:
        raise ValueError('Pilot page size must divide its record limit and respect the batch limit')
    if source_id not in {'pessoas', 'pessoas_serasa'}:
        raise ValueError('Unknown source index')
    metadata = {'kind': 'pilot', 'dataset_kind': 'synthetic' if synthetic else 'real',
                'source': reader.identity, 'snapshot_uuid': snapshot_uuid,
                'destination_deployment_id': str(info['deployment_id']),
                'plan': plan, 'adapter_version': ADAPTER_VERSION, 'normalizer_version': NORMALIZER_VERSION}
    budget = PilotBudget(limits, probe, clock=clock)
    budget.check()
    job = store.create_job(job_key, source_id, metadata=metadata)
    if job.get('metadata') != metadata:
        raise MigrationReadError('PILOT_JOB_CONTRACT_CHANGED')
    if job['status'] in {'completed', 'failed', 'cancelled'}:
        raise MigrationReadError('PILOT_TERMINAL_REQUIRES_NEW_ATTEMPT')
    counter = job['checkpoint']
    cursor = job.get('cursor')
    if (cursor or {}).get('seen', 0) != counter or counter > limits.records:
        raise MigrationReadError('CHECKPOINT_CURSOR_MISMATCH')
    # A restarted pilot never resets the time allowance silently. The caller
    # must start a new explicitly bounded attempt; operation replay is lossless.
    if counter:
        raise MigrationReadError('PILOT_RESUME_REQUIRES_NEW_BOUNDED_ATTEMPT')
    iterator = iter(reader.pages(cursor))
    state, reason, leaves = 'pilot_ingested', 'record_limit', 0
    coverage = hashlib.sha256()
    try:
        while counter < limits.records:
            if cancelled():
                raise PilotLimitReached('PILOT_CANCELLED')
            budget.check()
            page = next(iterator, None)
            if page is None:
                if not cursor or cursor.get('complete') is not True:
                    raise MigrationReadError('PILOT_SOURCE_ENDED_WITHOUT_EOF')
                reason = 'source_exhausted'; break
            if counter + len(page.records) > limits.records:
                raise PilotLimitReached('PILOT_PAGE_EXCEEDS_REMAINING_BUDGET')
            if not page.records:
                if not page.complete:
                    raise MigrationReadError('EMPTY_NONTERMINAL_PAGE')
                reason = 'source_exhausted'; break
            def prepare(source, external_id, record, source_version=None):
                if source != source_id:
                    raise MigrationReadError('SOURCE_INDEX_CHANGED_WITHIN_JOB')
                mapped = map_record(source_id, external_id, record, source_version=source_version)
                if mapped.get('coverage', {}).get('passed') is not True:
                    raise MigrationReadError('SOURCE_FIELD_RECONCILIATION_FAILED')
                return mapped
            prepared = prepare_page(page.records, prepare)
            budget.check(before_commit=True)
            if cancelled():
                raise PilotLimitReached('PILOT_CANCELLED')
            receipt = store.apply_batch(prepared, job_id=job['id'], expected_checkpoint=counter,
                next_checkpoint=counter+len(prepared), next_cursor=page.checkpoint, actor_id=actor_id)
            counter += len(prepared); cursor = page.checkpoint
            for mapped in prepared:
                coverage.update(json.dumps([mapped['source_id'], mapped['source_record_id'], mapped['record_hash'],
                    [(fact['source_path'], fact['input_type'], fact['input_hash']) for fact in mapped['facts']]],
                    ensure_ascii=True, separators=(',', ':')).encode())
                leaves += mapped['input_leaf_count']
            budget.check()
            if page.complete:
                reason = 'source_exhausted'; break
    except PilotLimitReached as exc:
        state, reason = 'pilot_stopped', str(exc)
    finally:
        close = getattr(iterator, 'close', None)
        if close:
            close()
    # Intentionally keep the canonical migration job uncompleted: bounded
    # ingestion is not full-index EOF, representative sampling or reconciliation
    # of every destination row. The pilot receipt names those outstanding gates.
    current = store.get_job(job['id'])
    if current['checkpoint'] != counter or current['records_processed'] != counter:
        raise MigrationReadError('PILOT_CHECKPOINT_RECONCILIATION_FAILED')
    return {'state': state, 'reason': reason, 'job_id': str(job['id']),
            'records_processed': counter, 'input_leaves_verified': leaves,
            'coverage_sha256': coverage.hexdigest(), 'peak_allocated_bytes': budget.peak_bytes,
            'elapsed_seconds': clock() - budget.started,
            'real_migration': not synthetic, 'full_migration_complete': False,
            'representative_sample_verified': False, 'destination_reconciliation_complete': False,
            'remaining_validation': ['representative_sample', 'destination_atom_reconciliation',
                                     'search_rebuild', 'measured_capacity_components']}

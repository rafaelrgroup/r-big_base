"""Compare every reconstructed row with a PostgreSQL operation history.

COMPACT_REPLAY_REFERENCE may point to the immutable prior release's store file
for an independent before/after comparison. Only the private fictitious fixture
is accepted, even when a deployed source file is used as the reference code.
"""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import importlib.util
import os
from pathlib import Path
import random
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
import pytest

from bigbase.canonical_store import CanonicalStore, CanonicalError, IdempotencyConflict, VersionConflict, _prepare
from bigbase.source_adapters import ExactDecimal
from test_canonical_store import atom, record as make_record, job, apply
from block_codec import Block, encode_block
from field_replay import FieldReplay


@pytest.fixture(scope='module')
def store():
    dsn = os.environ.get('BIGBASE_TEST_PG_DSN')
    if not dsn:
        pytest.skip('Explicit isolated fictitious PostgreSQL fixture required')
    with psycopg.connect(dsn) as c:
        assert c.execute("SELECT current_database(),current_setting('port'),inet_server_addr()").fetchone() == ('bigbase_test', '18769', None)
    store_class = CanonicalStore
    reference = os.environ.get('COMPACT_REPLAY_REFERENCE')
    if reference:
        path = Path(reference)
        assert path.is_absolute() and path.name == 'canonical_store.py'
        spec = importlib.util.spec_from_file_location('bigbase._compact_prior_store', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert not hasattr(module, 'resolve_canonical_observation'), 'Reference must predate the extracted resolver'
        store_class = module.CanonicalStore
    repo = store_class(dsn, 'cbreplay_' + uuid4().hex)
    repo.initialize()
    try:
        yield repo
    finally:
        with repo.connection() as c:
            c.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(repo.schema)))


def record(source, external='1', facts=None, **extras):
    return make_record(source, external, facts, source_version='fixture-v1', normalizer_version='unchanged-v1', **extras)


def scenarios(name):
    if name == 'tristate':
        return [[atom(value=number, flags={'is_whatsapp': {'value': flag}})]
                for number, flag in [('old', True), ('old', False), ('old', None), ('new', None), ('new', True)]]
    if name == 'late_and_pending':
        return [[atom(value=value, status=status, source_updated_at=date, flags={'valid': {'value': flag, 'observed_at': date}})]
                for value, status, date, flag in [
                    ('first', 'unknown', '2026-01-05T00:00:00Z', True),
                    ('older', 'unknown', '2026-01-01T00:00:00Z', False),
                    ('undated', 'unknown', None, None),
                    ('future', 'unknown', '2030-01-01T00:00:00Z', True),
                    ('pending', 'pending', '2026-01-06T00:00:00Z', False),
                    ('new', 'unknown', '2026-01-07T00:00:00Z', False),
                ]]
    if name == 'exact_literals':
        return [[atom('/value/' + str(n), value=value, target='field-' + str(n), kind='custom', key='unknown-' + str(n))
                 for n, value in enumerate([None, False, 0, '', {}, [], 'a\x00b\ud800', Decimal('12345678901234567890.1234500')])],
                [atom(value=ExactDecimal('1.2300e+004'), input_json='1.2300e+004', normalized_json='1.2300e+004', input_encoding='source_decimal_lexeme')]]
    if name == 'normalization_binding':
        return [[atom(value='received-number', normalized_value='normalized-number', flags={'is_whatsapp': flag})]
                for flag in [
                    {'value': True},
                    {'value': True, 'confirmed_value_json': '"normalized-number"'},
                    {'value': False, 'confirmed_value_json': '"received-number"'},
                ]]
    if name == 'multiple_items':
        return [[atom('/' + str(n), value=value, target=field, kind=kind, key=key, flags={'valid': {'value': valid}})
                 for n, (value, field, kind, key, valid) in enumerate([
                     ('first', 'name', 'identity', 'identity', True),
                     ('second', 'name', 'identity', 'identity', None),
                     ('111', 'number', 'phone', 'phone-1', True),
                     ('222', 'number', 'phone', 'phone-2', False),
                     ('first@example.invalid', 'address', 'email', 'email-1', None),
                     ('street', 'street', 'address', 'address-1', True),
                     ('12345000', 'postal_code', 'address', 'address-1', None),
                     ('synthetic-relative', 'document', 'relationship', 'relative-1', None),
                 ])], [atom(value='third', target='name')]]
    rng = random.Random(5721)
    return [[atom(value=rng.choice([None, False, True, 0, '', 'value-a', 'value-b']),
                  status=rng.choice(['unknown', 'pending', 'valid', 'invalid']),
                  source_updated_at=rng.choice([None, '2026-01-01T00:00:00Z', '2026-01-05T00:00:00Z', '2030-01-01T00:00:00Z']),
                  flags={'valid': {'value': rng.choice([None, False, True])}, 'is_whatsapp': {'value': rng.choice([None, False, True])}})]
            for _ in range(45)]


@pytest.mark.parametrize('scenario', ['tristate', 'late_and_pending', 'exact_literals', 'normalization_binding', 'multiple_items', 'mixed_sequence'])
@pytest.mark.parametrize('entity_type', ['person', 'company'])
def test_decoded_blocks_reproduce_every_database_history_and_current_field(store, scenario, entity_type):
    sources = [job(store), job(store)]
    document = uuid4().hex
    records = [record(sources[n % 2]['source_id'], str(n), facts, document=document, entity_type=entity_type)
               for n, facts in enumerate(scenarios(scenario))]
    block = Block(encode_block(records))
    replay = None
    for n, original in enumerate(records):
        receipt = apply(store, sources[n % 2], [original])
        owner = UUID(receipt['entity_ids'][0])
        if replay is None:
            replay = FieldReplay(owner, entity_type)
        assert replay.owner == owner
        operation_id = _prepare(original)['operation_id']
        with store.connection() as c:
            operation = c.execute('SELECT * FROM operations WHERE operation_id=%s', (operation_id,)).fetchone()
            history = c.execute('SELECT * FROM observations WHERE operation_id=%s ORDER BY operation_sequence', (operation_id,)).fetchall()
            states = c.execute('SELECT * FROM field_state WHERE owner_id=%s', (owner,)).fetchall()
            items = c.execute('SELECT item_id,kind,item_key_json,version,created_at,updated_at FROM items WHERE owner_id=%s', (owner,)).fetchall()
        result = replay.apply(block.get(n), actor='test-actor', received_at=operation['received_at'], entity_version=operation['entity_version'])
        assert result['observations'] == history
        assert replay.fields == {(r['item_id'], r['target_path_hash'], r['dimension']): r for r in states}
        assert replay.items == {r['item_id']: {k: v for k, v in r.items() if k != 'item_id'} for r in items}


def test_operation_replay_preserves_original_actor_and_receipt():
    value = record('synthetic')
    replay = FieldReplay(uuid4(), 'person')
    received = datetime(2026, 1, 10, tzinfo=timezone.utc)
    replay.apply(value, actor='first', received_at=received, entity_version=1)
    before = deepcopy((replay.fields, replay.items, replay.operations))
    result = replay.apply(value, actor='retry', received_at=received, entity_version=2)
    assert result == {'replayed': True, 'observations': [], 'version': 1}
    assert (replay.fields, replay.items, replay.operations) == before
    changed = deepcopy(value)
    changed['facts'][0]['normalized_value'] = 'different'
    with pytest.raises(IdempotencyConflict):
        replay.apply(changed, actor='retry', received_at=received, entity_version=2)


def test_gap_or_failure_inside_operation_does_not_change_projection():
    replay = FieldReplay(uuid4(), 'person')
    received = datetime(2026, 1, 10, tzinfo=timezone.utc)
    with pytest.raises(VersionConflict):
        replay.apply(record('synthetic'), actor='test', received_at=received, entity_version=2)
    value = record('synthetic', facts=[atom('/first'), atom('/second', flags={'valid': {'value': True, 'confirmed_value_json': 7}})])
    with pytest.raises(CanonicalError):
        replay.apply(value, actor='test', received_at=received, entity_version=1)
    assert replay.version == 0 and replay.fields == replay.items == replay.operations == {}

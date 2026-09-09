from copy import deepcopy
from decimal import Decimal
import struct
import zlib

import pytest

from bigbase.canonical_store import _prepare
from bigbase.source_adapters import ExactDecimal, map_record, reconcile_coverage
from derived_codec import CodecError, HEADER, decode, encode, pack, unpack
from observation_codec import typed_bytes
from synthetic_samples import record, add_event_metadata


@pytest.mark.parametrize('profile', ['sparse', 'nested', 'wide'])
@pytest.mark.parametrize('metadata', [False, True])
def test_all_atoms_provenance_and_canonical_identities_are_exact(profile, metadata):
    source = record(profile, 3)
    mapped = map_record('pessoas_serasa', 'synthetic-derived-3', source, source_version='1:3')
    if metadata:
        mapped = add_event_metadata(mapped, 3)
    result = decode(encode(mapped))
    assert typed_bytes(result) == typed_bytes(mapped)
    assert _prepare(result) == _prepare(mapped)
    assert reconcile_coverage(source, result)['passed']


@pytest.mark.parametrize('value', [None, False, 0, '', [], {}, 'a\x00b\ud800',
                                  Decimal('12345678901234567890.1234500'), ExactDecimal('1.2300e+2'), -0.0])
def test_exact_scalar_types_and_lexical_decimals(value):
    mapped = map_record('pessoas', 'synthetic-derived', {'unknown': value}, source_version='1:2')
    assert typed_bytes(decode(encode(mapped))) == typed_bytes(mapped)


def test_missing_explicit_null_overridden_hash_and_id_remain_distinct():
    mapped = map_record('pessoas', 'synthetic-derived', {'one': False, 'two': None}, source_version='1:2')
    one, two = mapped['facts']
    del one['observed_at']
    one['flags'] = {'valid': {'value': False, 'source_updated_at': '2020-01-01T00:00:00Z'}}
    one['id'] = 'custom-id'
    one['input_hash'] = 'custom-hash'
    two['flags'] = {'valid': {'value': None}, 'whatsapp': {'value': False}}
    result = decode(encode(mapped))
    assert typed_bytes(result) == typed_bytes(mapped)
    result['facts'][1]['flags']['valid']['value'] = True
    assert mapped['facts'][1]['flags']['valid']['value'] is None


def test_masks_reject_duplicate_or_unknown_derivations():
    mapped = map_record('pessoas', 'synthetic-derived', {'unknown': True}, source_version='1:2')
    packed = pack(mapped)
    bad = deepcopy(packed)
    bad['facts'][0][0] = 1 << 30
    with pytest.raises(CodecError): unpack(bad)
    bad = deepcopy(packed)
    bad['facts'][0][1]['id'] = 'collision'
    with pytest.raises(CodecError): unpack(bad)


def test_frames_reject_corruption_truncation_and_trailing_data():
    mapped = map_record('pessoas', 'synthetic-derived', {'unknown': True}, source_version='1:2')
    frame = encode(mapped)
    for altered in [frame[:-1], frame + b'x', b'X' + frame[1:], frame[:20] + bytes([frame[20] ^ 1]) + frame[21:]]:
        with pytest.raises(CodecError): decode(altered)

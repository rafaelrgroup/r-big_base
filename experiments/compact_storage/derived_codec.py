"""Lossless normalized atom frames with explicitly versioned derived metadata.

This experimental codec stores fields, values and provenance. It does not store
whole source documents or change canonical identity/precedence semantics.
"""
import hashlib
import json
import struct
import zlib

from bigbase.source_adapters import ExactDecimal, exact_json, input_type
from observation_codec import (
    CodecError, MAX_FRAME_BYTES, MAX_EXPANDED_BYTES, _validate, _untyped,
    deepcopy, require,
)

from wire_codec import dumps as typed_bytes, loads as wire_loads

MAGIC = b'BBFC\x01'
HEADER = struct.Struct('>5sBQ32s32s')
DERIVED = (
    'id', 'input_hash', 'input_json', 'input_type', 'input_encoding',
    'input_lexeme_available', 'normalized_json', 'normalized_type',
    'source_id', 'source_record_id', 'source_version', 'adapter_version',
    'normalizer_version', 'observed_at', 'source_updated_at', 'flags',
    'normalized_value', 'item_key', 'item_attributes', 'notes', 'normalization',
    'status', 'pending_reason',
)


def sha(value):
    return hashlib.sha256(exact_json(value).encode('ascii')).hexdigest()


def input_hash(value):
    # Source atoms are scalar or empty containers. Freeze the mapper v1 hash.
    if type(value) is dict:
        require(not value, 'WHOLE_SOURCE_SUBTREE_REFUSED')
        return sha(['object', []])
    if type(value) is list:
        require(not value, 'WHOLE_SOURCE_SUBTREE_REFUSED')
        return sha(['array', []])
    return sha([input_type(value), exact_json(value)])


def expected(record, fact):
    """Freeze v1 derivations instead of changing them with a future mapper."""
    raw, normalized = fact['input_value'], fact.get('normalized_value', fact['input_value'])
    result = {name: record[name] for name in
              ('source_id', 'source_record_id', 'source_version', 'adapter_version', 'normalizer_version')}
    result.update(
        id='fact_' + sha([record['source_id'], record['source_record_id'], record['source_version'],
                         record['record_hash'], record['adapter_version'], record['normalizer_version'],
                         fact['source_path']]),
        input_hash=input_hash(raw), input_json=exact_json(raw), input_type=input_type(raw),
        input_encoding='source_decimal_lexeme' if isinstance(raw, ExactDecimal) else 'canonical_value',
        input_lexeme_available=isinstance(raw, ExactDecimal),
        normalized_json=exact_json(normalized), normalized_type=input_type(normalized),
        observed_at=None, source_updated_at=None, flags={}, normalized_value=raw,
        item_key='custom:' + sha([record['source_id'], fact['source_path']]),
        item_attributes={'field_id': 'source:' + record['source_id'] + ':' + fact['source_path']},
        notes=[], normalization={}, status='pending', pending_reason='unmapped_semantics',
    )
    return result


def same_value(left, right):
    if type(left) is not type(right):
        return False
    if left is None or type(left) in (str, bool, int):
        return left == right
    if type(left) is float:
        return left.hex() == right.hex()
    return typed_bytes(left) == typed_bytes(right)


def pack(record):
    _validate(record)
    required = ('source_id', 'source_record_id', 'source_version', 'record_hash',
                'adapter_version', 'normalizer_version')
    require(all(k in record for k in required), 'RECORD_METADATA_REQUIRED')
    result = {k: deepcopy(v) for k, v in record.items() if k != 'facts'}
    rows = []
    for fact in record['facts']:
        derived = expected(record, fact)
        mask = 0
        row = deepcopy(fact)
        for bit, key in enumerate(DERIVED):
            # Presence is explicit in the mask; missing remains different from null.
            if key in row and same_value(row[key], derived[key]):
                del row[key]
                mask |= 1 << bit
        rows.append([mask, row])
    result['facts'] = rows
    return result


def unpack(record):
    require(type(record) is dict and type(record.get('facts')) is list,
            'INVALID_DERIVED_ENVELOPE')
    require(1 <= len(record['facts']) <= 20000, 'FACT_COUNT_LIMIT')
    result = {k: deepcopy(v) for k, v in record.items() if k != 'facts'}
    facts, total = [], len(typed_bytes(result))
    for pair in record['facts']:
        require(type(pair) is list and len(pair) == 2 and type(pair[0]) is int
                and 0 <= pair[0] < (1 << len(DERIVED)) and type(pair[1]) is dict,
                'INVALID_DERIVED_ATOM')
        mask, row = pair[0], deepcopy(pair[1])
        require(all(not (mask & (1 << bit)) or key not in row
                    for bit, key in enumerate(DERIVED)), 'DUPLICATE_DERIVED_VALUE')
        require('input_value' in row and 'source_path' in row, 'ATOM_VALUES_REQUIRED')
        if mask & (1 << DERIVED.index('normalized_value')):
            row['normalized_value'] = deepcopy(row['input_value'])
        derived = expected(result, row)
        for bit, key in enumerate(DERIVED):
            if mask & (1 << bit):
                row[key] = deepcopy(derived[key])
        total += len(typed_bytes(row)) + 16
        require(total <= MAX_EXPANDED_BYTES, 'EXPANDED_RECORD_LIMIT')
        facts.append(row)
    result['facts'] = facts
    _validate(result)
    return result


def encode(record, level=1):
    require(type(level) is int and 0 <= level <= 9, 'COMPRESSION_LEVEL_INVALID')
    original = typed_bytes(record)
    require(len(original) <= MAX_EXPANDED_BYTES, 'EXPANDED_RECORD_LIMIT')
    payload = typed_bytes(pack(record))
    require(len(payload) <= MAX_FRAME_BYTES, 'FRAME_SIZE_LIMIT')
    compressed = zlib.compress(payload, level)
    require(len(compressed) <= MAX_FRAME_BYTES, 'FRAME_SIZE_LIMIT')
    return HEADER.pack(MAGIC, 1, len(payload), hashlib.sha256(payload).digest(),
                       hashlib.sha256(original).digest()) + compressed


def decode(frame):
    require(type(frame) is bytes and HEADER.size <= len(frame) <= MAX_FRAME_BYTES + HEADER.size,
            'FRAME_SIZE_LIMIT')
    magic, compression, length, payload_hash, original_hash = HEADER.unpack(frame[:HEADER.size])
    require(magic == MAGIC and compression == 1, 'UNSUPPORTED_FRAME_VERSION')
    require(length <= MAX_FRAME_BYTES, 'FRAME_SIZE_LIMIT')
    try:
        reader = zlib.decompressobj()
        payload = reader.decompress(frame[HEADER.size:], length + 1)
        require(reader.eof and not reader.unused_data and not reader.unconsumed_tail,
                'INCOMPLETE_OR_TRAILING_COMPRESSED_DATA')
    except zlib.error:
        raise CodecError('INVALID_COMPRESSED_DATA') from None
    require(len(payload) == length and hashlib.sha256(payload).digest() == payload_hash,
            'PAYLOAD_INTEGRITY_FAILED')
    try:
        result = unpack(wire_loads(payload))
    except (ValueError, KeyError, TypeError, RecursionError):
        raise CodecError('INVALID_DERIVED_PAYLOAD') from None
    require(hashlib.sha256(typed_bytes(result)).digest() == original_hash, 'ATOM_INTEGRITY_FAILED')
    return result

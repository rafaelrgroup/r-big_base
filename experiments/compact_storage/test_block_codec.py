from copy import deepcopy
import hashlib
import struct
import zlib

import pytest
from bigbase.source_adapters import map_record, reconcile_coverage
from bigbase.canonical_store import _prepare
from block_codec import Block, encode_block, CodecError, HEADER, ENTRY, MAGIC, CODEC
from wire_codec import dumps
from synthetic_samples import record,add_event_metadata


def records(profile='nested',size=4):
    return [add_event_metadata(map_record('pessoas_serasa',str(n),record(profile,n),source_version='1:'+str(n)),n) for n in range(size)]


@pytest.mark.parametrize('profile',['sparse','nested','wide'])
def test_all_values_metadata_and_canonical_preparation_roundtrip(profile):
    source=records(profile);block=Block(encode_block(source))
    assert len(block)==len(source)
    for expected,actual in zip(source,block):
        assert dumps(expected)==dumps(actual) and _prepare(expected)==_prepare(actual)
    assert dumps(block.get(2))==dumps(source[2])


def test_point_reads_are_independent_mutable_values():
    block=Block(encode_block(records()));first=block.get(0);original=dumps(first)
    first['facts'][0]['flags']={'valid':False}
    assert dumps(block.get(0))==original


@pytest.mark.parametrize('index',[-1,4,True,'0'])
def test_invalid_record_position_is_never_reinterpreted(index):
    with pytest.raises(CodecError):Block(encode_block(records())).get(index)


def test_truncation_corruption_and_trailing_data_rejected():
    raw=encode_block(records())
    for bad in [raw[:-1],raw+b'x',raw[:HEADER.size]+b'bad',raw[:10]+bytes([raw[10]^1])+raw[11:]]:
        with pytest.raises(CodecError):Block(bad)


def test_unknown_derived_codec_refused():
    raw=encode_block(records());fields=list(HEADER.unpack_from(raw));fields[1]+=1
    with pytest.raises(CodecError):Block(HEADER.pack(*fields)+raw[HEADER.size:])


def test_record_digest_cannot_be_replaced_by_valid_block_checksum():
    raw=encode_block(records());fields=list(HEADER.unpack_from(raw));payload=bytearray(zlib.decompress(raw[HEADER.size:]));payload[8]^=1
    fields[-1]=hashlib.sha256(payload).digest();block=Block(HEADER.pack(*fields)+zlib.compress(payload))
    with pytest.raises(CodecError,match='RECORD_INTEGRITY'):block.get(0)


def test_declared_length_prevents_unbounded_decompression():
    raw=encode_block(records());fields=list(HEADER.unpack_from(raw));fields[4]=1
    with pytest.raises(CodecError):Block(HEADER.pack(*fields)+raw[HEADER.size:])


def test_empty_and_overfull_blocks_fail_without_truncation():
    with pytest.raises(CodecError):encode_block([])
    base=records('sparse',1)[0]
    with pytest.raises(CodecError):encode_block(base for _ in range(129))


def test_source_coverage_includes_unknown_fields_and_empty_containers():
    source=record('wide',7);mapped=map_record('pessoas','synthetic-7',source,source_version='1:7')
    assert reconcile_coverage(source,Block(encode_block([mapped])).get(0))['passed']

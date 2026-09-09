"""Experimental lossless atom envelope. Standard library, no DB/filesystem I/O.

Input is already mapped field atoms, never an entire original source document.
Each immutable frame describes one operation; encoding grants no new semantics.
"""
from collections import Counter
from copy import deepcopy as _stdlib_deepcopy
from decimal import Decimal
import base64
import binascii
import hashlib
import json
import math
import re
import struct
import zlib

VERSION=1
MAGIC=b'BBAC\x01'
HEADER=struct.Struct('>5sBQ32s32s')
MAX_FRAME_BYTES=64*1024*1024
MAX_EXPANDED_BYTES=64*1024*1024
MAX_FACTS=20000
MAX_CONTAINERS=100000
MAX_DEPTH=96
TOP_KEYS={'source_id','source_record_id','source_version','record_hash','adapter_version','normalizer_version',
          'entity_type','identity_candidate','identity_status','input_leaf_count','coverage','facts','containers','operation'}
SHAREABLE={'source_id','source_record_id','source_version','adapter_version','normalizer_version','observed_at','source_updated_at',
           'flags','target_kind','target_path','item_key','item_attributes','normalization','notes','status','pending_reason',
           'input_type','normalized_type','input_encoding','input_lexeme_available','actor_id','operation_id','received_at'}


class CodecError(ValueError):pass


def require(condition,code):
    if not condition:raise CodecError(code)


def deepcopy(value):
    # Decimal itself is immutable, but ExactDecimal carries a mutable lexeme
    # attribute. Standard deepcopy reuses that instance; do not alias it across
    # fields that happened to share an encoded default.
    if type(value) is dict:return {k:deepcopy(v) for k,v in value.items()}
    if type(value) is list:return [deepcopy(v) for v in value]
    if isinstance(value,Decimal) and hasattr(value,'json_lexeme'):
        return type(value)(value.json_lexeme)
    return _stdlib_deepcopy(value)


def _typed(value,depth=0):
    require(depth<=MAX_DEPTH,'VALUE_DEPTH_LIMIT')
    if value is None or type(value) in (bool,int):return value
    if type(value) is str:
        if '\x00' in value or any(0xD800<=ord(ch)<=0xDFFF for ch in value):
            return ['s',base64.b64encode(value.encode('utf-8','surrogatepass')).decode('ascii')]
        return value
    if type(value) is float:
        require(math.isfinite(value),'NONFINITE_NUMBER');return ['f',value.hex()]
    if isinstance(value,Decimal):
        require(value.is_finite(),'NONFINITE_NUMBER')
        if hasattr(value,'json_lexeme'):
            lexeme=value.json_lexeme
            require(isinstance(lexeme,str) and re.fullmatch(r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?',lexeme)
                    and Decimal(lexeme)==value,'INVALID_DECIMAL_LEXEME')
            return ['x',lexeme]
        return ['d',str(value)]
    if type(value) is list:return ['a',[_typed(v,depth+1) for v in value]]
    if type(value) is dict:
        require(all(type(k) is str for k in value),'NON_STRING_KEY')
        return ['o',[[_typed(k,depth+1),_typed(value[k],depth+1)] for k in sorted(value)]]
    raise CodecError('UNSUPPORTED_VALUE_TYPE')


def _untyped(value,decimal_lexeme_factory,depth=0):
    require(depth<=MAX_DEPTH,'VALUE_DEPTH_LIMIT')
    if value is None or type(value) in (bool,int,str):return value
    require(type(value) is list and len(value)==2 and type(value[0]) is str,'INVALID_TYPE_NODE')
    tag,payload=value
    if tag=='s':
        require(type(payload) is str,'INVALID_STRING_NODE')
        try:return base64.b64decode(payload,validate=True).decode('utf-8','surrogatepass')
        except (ValueError,UnicodeError,binascii.Error):raise CodecError('INVALID_STRING_NODE') from None
    if tag in ('f','d','x'):
        require(type(payload) is str,'INVALID_NUMBER_NODE')
        try:
            if tag=='f':
                result=float.fromhex(payload);require(math.isfinite(result),'NONFINITE_NUMBER');return result
            if tag=='d':
                result=Decimal(payload);require(result.is_finite(),'NONFINITE_NUMBER');return result
            require(decimal_lexeme_factory is not None,'LEXICAL_DECIMAL_FACTORY_REQUIRED')
            result=decimal_lexeme_factory(payload)
            require(isinstance(result,Decimal) and getattr(result,'json_lexeme',None)==payload,'INVALID_DECIMAL_FACTORY')
            return result
        except (ArithmeticError,ValueError) as error:
            if isinstance(error,CodecError):raise
            raise CodecError('INVALID_NUMBER_NODE') from None
    require(type(payload) is list,'INVALID_COLLECTION_NODE')
    if tag=='a':return [_untyped(v,decimal_lexeme_factory,depth+1) for v in payload]
    require(tag=='o','UNKNOWN_TYPE_TAG')
    result={}
    for pair in payload:
        require(type(pair) is list and len(pair)==2,'INVALID_OBJECT_PAIR')
        key=_untyped(pair[0],decimal_lexeme_factory,depth+1)
        require(type(key) is str and key not in result,'INVALID_OBJECT_PAIR')
        result[key]=_untyped(pair[1],decimal_lexeme_factory,depth+1)
    return result


def typed_bytes(value):
    try:return json.dumps(_typed(value),ensure_ascii=True,separators=(',',':'),allow_nan=False).encode('ascii')
    except (RecursionError,ValueError,OverflowError) as error:
        if isinstance(error,CodecError):raise
        raise CodecError('VALUE_ENCODING_FAILED') from None


def exact_hash(value):return hashlib.sha256(typed_bytes(value)).digest()


def _validate(record):
    require(type(record) is dict and not set(record)-TOP_KEYS,'UNSUPPORTED_RECORD_ENVELOPE')
    facts=record.get('facts');containers=record.get('containers')
    require(type(facts) is list and 1<=len(facts)<=MAX_FACTS,'FACT_COUNT_LIMIT')
    require(type(containers) is list and len(containers)<=MAX_CONTAINERS,'CONTAINER_COUNT_LIMIT')
    ids=set();paths=set()
    for fact in facts:
        require(type(fact) is dict,'FACT_OBJECT_REQUIRED')
        for name in ('id','source_path','item_key','target_kind','target_path'):
            require(type(fact.get(name)) is str,'FACT_REFERENCE_REQUIRED')
        require(fact['id'] not in ids and fact['source_path'] not in paths,'DUPLICATE_FACT_REFERENCE')
        ids.add(fact['id']);paths.add(fact['source_path'])
        require('input_value' in fact and 'normalized_value' in fact,'EXPLICIT_VALUES_REQUIRED')
        require(not isinstance(fact['input_value'],(dict,list)) or not fact['input_value'],'WHOLE_SOURCE_SUBTREE_REFUSED')
    for row in containers:
        require(type(row) is dict and type(row.get('source_path')) is str and row.get('type') in ('object','array')
                and type(row.get('length')) is int and row['length']>=0,'INVALID_CONTAINER')
    if 'input_leaf_count' in record:
        require(type(record['input_leaf_count']) is int and record['input_leaf_count']==len(facts),'LEAF_COUNT_MISMATCH')


def _split_defaults(rows,shareable):
    """Only keys present in every row can default: absence never becomes null."""
    if len(rows)<2:return {},deepcopy(rows)
    common=set.intersection(*(set(row) for row in rows))&shareable
    defaults={};encoded={}
    for key in sorted(common):
        enc=[typed_bytes(row[key]) for row in rows];counts=Counter(enc)
        winner,count=min(counts.items(),key=lambda pair:(-pair[1],len(pair[0]),pair[0]))
        # Require conservative positive savings even before dictionary/tag costs.
        if count>=2 and (count-1)*(len(key)+len(winner))>16:
            defaults[key]=deepcopy(rows[enc.index(winner)][key]);encoded[key]=winner
    rest=[{k:deepcopy(v) for k,v in row.items() if k not in encoded or typed_bytes(v)!=encoded[k]} for row in rows]
    return defaults,rest


def envelope(record):
    _validate(record)
    global_defaults,rest=_split_defaults(record['facts'],SHAREABLE)
    groups={}
    for index,fact in enumerate(record['facts']):
        key=(fact['target_kind'],fact['item_key']);groups.setdefault(key,[]).append((index,rest[index]))
    packed=[]
    for rows in groups.values():
        defaults,overrides=_split_defaults([row for _,row in rows],SHAREABLE)
        packed.append({'defaults':defaults,'rows':[[rows[i][0],row] for i,row in enumerate(overrides)]})
    container_defaults,containers=_split_defaults(record['containers'],{'type','length'})
    return {'version':VERSION,'record':{k:deepcopy(v) for k,v in record.items() if k not in ('facts','containers')},
            'fact_count':len(rest),'fact_defaults':global_defaults,'groups':packed,
            'containers':{'defaults':container_defaults,'rows':containers}}


def restore_envelope(value):
    require(type(value) is dict and set(value)=={'version','record','fact_count','fact_defaults','groups','containers'},'INVALID_COMPACT_ENVELOPE')
    require(type(value['version']) is int and value['version']==VERSION,'UNSUPPORTED_CODEC_VERSION')
    count=value['fact_count'];require(type(count) is int and 1<=count<=MAX_FACTS,'FACT_COUNT_LIMIT')
    require(type(value['record']) is dict and not {'facts','containers'}&set(value['record']),'INVALID_RECORD_METADATA')
    require(type(value['fact_defaults']) is dict and not set(value['fact_defaults'])-SHAREABLE,'INVALID_SHARED_METADATA')
    require(type(value['groups']) is list and 1<=len(value['groups'])<=count,'INVALID_ITEM_GROUPS')
    expanded=len(typed_bytes(value['record']))
    require(expanded<=MAX_EXPANDED_BYTES,'EXPANDED_RECORD_LIMIT')
    facts=[None]*count
    for group in value['groups']:
        require(type(group) is dict and set(group)=={'defaults','rows'} and type(group['defaults']) is dict
                and not set(group['defaults'])-SHAREABLE and type(group['rows']) is list and group['rows'],'INVALID_ITEM_GROUP')
        for pair in group['rows']:
            require(type(pair) is list and len(pair)==2 and type(pair[0]) is int and 0<=pair[0]<count and facts[pair[0]] is None
                    and type(pair[1]) is dict,'INVALID_ATOM_POSITION')
            # Independent objects, even when flags/metadata were stored once.
            combined={**value['fact_defaults'],**group['defaults'],**pair[1]}
            expanded+=len(typed_bytes(combined))+16
            require(expanded<=MAX_EXPANDED_BYTES,'EXPANDED_RECORD_LIMIT')
            facts[pair[0]]=deepcopy(combined)
    require(all(fact is not None for fact in facts),'MISSING_ATOM_POSITION')
    c=value['containers'];require(type(c) is dict and set(c)=={'defaults','rows'} and type(c['defaults']) is dict
                                and not set(c['defaults'])-{'type','length'} and type(c['rows']) is list
                                and len(c['rows'])<=MAX_CONTAINERS,'INVALID_CONTAINER_ENVELOPE')
    require(all(type(row) is dict for row in c['rows']),'INVALID_CONTAINER')
    containers=[]
    for row in c['rows']:
        combined={**c['defaults'],**row};expanded+=len(typed_bytes(combined))+16
        require(expanded<=MAX_EXPANDED_BYTES,'EXPANDED_RECORD_LIMIT')
        containers.append(deepcopy(combined))
    result={**deepcopy(value['record']),'facts':facts,'containers':containers}
    _validate(result);return result


def encode(record,*,compression='zlib'):
    require(compression in ('none','zlib'),'UNSUPPORTED_COMPRESSION')
    _validate(record)
    require(len(typed_bytes(record))+16*(len(record['facts'])+len(record['containers']))<=MAX_EXPANDED_BYTES,'EXPANDED_RECORD_LIMIT')
    payload=typed_bytes(envelope(record));require(len(payload)<=MAX_FRAME_BYTES,'FRAME_SIZE_LIMIT')
    data=zlib.compress(payload,6) if compression=='zlib' else payload
    require(len(data)<=MAX_FRAME_BYTES,'FRAME_SIZE_LIMIT')
    return HEADER.pack(MAGIC,int(compression=='zlib'),len(payload),hashlib.sha256(payload).digest(),exact_hash(record))+data


def decode(frame,*,decimal_lexeme_factory=None):
    require(type(frame) is bytes and HEADER.size<=len(frame)<=MAX_FRAME_BYTES+HEADER.size,'FRAME_SIZE_LIMIT')
    magic,compressed,length,payload_hash,record_hash=HEADER.unpack(frame[:HEADER.size])
    require(magic==MAGIC and compressed in (0,1),'UNSUPPORTED_FRAME_VERSION')
    require(length<=MAX_FRAME_BYTES,'FRAME_SIZE_LIMIT')
    data=frame[HEADER.size:]
    if compressed:
        try:
            reader=zlib.decompressobj();payload=reader.decompress(data,min(length,MAX_FRAME_BYTES)+1)
            require(reader.eof and not reader.unused_data and not reader.unconsumed_tail,'INCOMPLETE_OR_TRAILING_COMPRESSED_DATA')
        except zlib.error:raise CodecError('INVALID_COMPRESSED_DATA') from None
    else:payload=data
    require(len(payload)==length and hashlib.sha256(payload).digest()==payload_hash,'PAYLOAD_INTEGRITY_FAILED')
    try:typed=json.loads(payload,parse_constant=lambda _: (_ for _ in ()).throw(CodecError('NONFINITE_NUMBER')))
    except (ValueError,RecursionError):raise CodecError('INVALID_PAYLOAD_JSON') from None
    value=_untyped(typed,decimal_lexeme_factory)
    result=restore_envelope(value)
    require(exact_hash(result)==record_hash,'ATOM_INTEGRITY_FAILED')
    return result


def stats(record):
    value=envelope(record);raw=typed_bytes(value)
    return {'facts':len(record['facts']),'containers':len(record['containers']),'item_groups':len(value['groups']),
            'global_shared_keys':len(value['fact_defaults']),'group_shared_keys':sum(len(g['defaults']) for g in value['groups']),
            'compact_payload_bytes':len(raw),'frame_bytes':len(encode(record)),'frame_header_bytes':HEADER.size}

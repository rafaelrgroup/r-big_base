"""Experimental immutable blocks of normalized atoms; no source-document blobs.

Each record retains its complete canonical payload digest. Bounded decompression
validates the entire block before exposing records, and point reads unpack only
the selected record. Metadata derivations are frozen by the embedded codec ID.
"""
import hashlib
import struct
import zlib

from derived_codec import pack, unpack, CodecError, require
from wire_codec import dumps, loads

MAGIC=b'BBBK\x01'
CODEC=2  # Frozen v1 derivations/wire plus explicit operation flag order.
HEADER=struct.Struct('>5sBBII32s')
ENTRY=struct.Struct('>II32s')
MAX_RECORDS=128
MAX_PAYLOAD=32*1024*1024
MAX_COMPRESSED=16*1024*1024
MAX_RECORD=64*1024*1024


def flag_orders(record):
    """JSON object order is irrelevant except where flags assign event positions."""
    orders=[]
    for index,fact in enumerate(record['facts']):
        flags=fact.get('flags',{})
        require(type(flags) is dict and all(type(name) is str for name in flags),'INVALID_FLAG_OBJECT')
        names=list(flags)
        if names!=sorted(names):orders.append([index,names])
    return orders


def restore_flag_orders(record,orders):
    require(type(orders) is list,'INVALID_FLAG_ORDER')
    previous=-1
    for entry in orders:
        require(type(entry) is list and len(entry)==2,'INVALID_FLAG_ORDER')
        index,names=entry
        require(type(index) is int and previous<index<len(record['facts']),'INVALID_FLAG_ORDER')
        flags=record['facts'][index].get('flags',{})
        require(type(names) is list and len(names)>1 and all(type(name) is str for name in names)
                and len(names)==len(set(names)) and set(names)==set(flags) and names!=sorted(names),'INVALID_FLAG_ORDER')
        record['facts'][index]['flags']={name:flags[name] for name in names}
        previous=index
    require(flag_orders(record)==orders,'INVALID_FLAG_ORDER')
    return record


def encode_block(records, *, level=1):
    require(type(level) is int and 0<=level<=9,'INVALID_BLOCK_COMPRESSION_LEVEL')
    compressor=zlib.compressobj(level);chunks=[];digest=hashlib.sha256();count=total=size=0
    for record in records:
        require(count<MAX_RECORDS,'BLOCK_RECORD_LIMIT')
        packed=pack(record);orders=flag_orders(record)
        original=dumps([record,orders]);require(len(original)<=MAX_RECORD,'EXPANDED_RECORD_LIMIT')
        payload=dumps([packed,orders])
        entry=ENTRY.pack(len(payload),len(original),hashlib.sha256(original).digest())+payload
        total+=len(entry);require(total<=MAX_PAYLOAD,'BLOCK_PAYLOAD_LIMIT')
        digest.update(entry);compressed=compressor.compress(entry);size+=len(compressed)
        require(size<=MAX_COMPRESSED,'BLOCK_COMPRESSED_LIMIT');chunks.append(compressed);count+=1
    require(count>0,'EMPTY_BLOCK_REFUSED')
    end=compressor.flush();size+=len(end);require(size<=MAX_COMPRESSED,'BLOCK_COMPRESSED_LIMIT');chunks.append(end)
    return HEADER.pack(MAGIC,CODEC,1,count,total,digest.digest())+b''.join(chunks)


class Block:
    def __init__(self, raw):
        require(type(raw) is bytes and HEADER.size<=len(raw)<=HEADER.size+MAX_COMPRESSED,'BLOCK_COMPRESSED_LIMIT')
        magic,codec,compression,count,length,digest=HEADER.unpack_from(raw)
        require(magic==MAGIC and codec in (1,CODEC) and compression==1,'UNSUPPORTED_BLOCK_VERSION')
        require(1<=count<=MAX_RECORDS and 0<length<=MAX_PAYLOAD,'INVALID_BLOCK_BOUNDS')
        try:
            reader=zlib.decompressobj();payload=reader.decompress(raw[HEADER.size:],length+1)
            require(reader.eof and not reader.unused_data and not reader.unconsumed_tail,'INCOMPLETE_OR_TRAILING_BLOCK')
        except zlib.error:raise CodecError('INVALID_COMPRESSED_BLOCK') from None
        require(len(payload)==length and hashlib.sha256(payload).digest()==digest,'BLOCK_INTEGRITY_FAILED')
        entries=[];offset=0
        for _ in range(count):
            require(offset+ENTRY.size<=length,'TRUNCATED_BLOCK_ENTRY')
            size,expanded,original_hash=ENTRY.unpack_from(payload,offset);offset+=ENTRY.size
            require(0<size<=MAX_PAYLOAD and 0<expanded<=MAX_RECORD and offset+size<=length,'INVALID_BLOCK_ENTRY')
            entries.append((offset,size,expanded,original_hash));offset+=size
        require(offset==length,'TRAILING_BLOCK_ENTRY')
        self.payload,self.entries,self.codec=payload,tuple(entries),codec

    def __len__(self):return len(self.entries)

    def get(self,index):
        require(type(index) is int and 0<=index<len(self),'INVALID_BLOCK_POSITION')
        offset,size,expanded,digest=self.entries[index]
        try:
            payload=loads(self.payload[offset:offset+size])
            if self.codec==1:
                record=unpack(payload)
                # v1 did not record insertion order. Never invent event positions
                # for an ambiguous old experimental block; reencode its inputs.
                require(all(len(f.get('flags',{}))<=1 for f in record['facts']),'LEGACY_FLAG_ORDER_NOT_RECORDED')
                original=dumps(record)
            else:
                require(type(payload) is list and len(payload)==2,'INVALID_ORDERED_RECORD')
                record=restore_flag_orders(unpack(payload[0]),payload[1])
                original=dumps([record,flag_orders(record)])
        except (ValueError,KeyError,TypeError,RecursionError):raise CodecError('INVALID_BLOCK_RECORD') from None
        require(len(original)==expanded and hashlib.sha256(original).digest()==digest,'BLOCK_RECORD_INTEGRITY_FAILED')
        return record

    def __iter__(self):
        for index in range(len(self)):yield self.get(index)

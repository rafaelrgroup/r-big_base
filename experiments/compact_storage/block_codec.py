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
CODEC=1  # BBFC v1 derivations + exact wire v1, never redefined in place
HEADER=struct.Struct('>5sBBII32s')
ENTRY=struct.Struct('>II32s')
MAX_RECORDS=128
MAX_PAYLOAD=32*1024*1024
MAX_COMPRESSED=16*1024*1024
MAX_RECORD=64*1024*1024


def encode_block(records, *, level=1):
    require(type(level) is int and 0<=level<=9,'INVALID_BLOCK_COMPRESSION_LEVEL')
    compressor=zlib.compressobj(level);chunks=[];digest=hashlib.sha256();count=total=size=0
    for record in records:
        require(count<MAX_RECORDS,'BLOCK_RECORD_LIMIT')
        original=dumps(record);require(len(original)<=MAX_RECORD,'EXPANDED_RECORD_LIMIT')
        payload=dumps(pack(record))
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
        require(magic==MAGIC and codec==CODEC and compression==1,'UNSUPPORTED_BLOCK_VERSION')
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
        self.payload,self.entries=payload,tuple(entries)

    def __len__(self):return len(self.entries)

    def get(self,index):
        require(type(index) is int and 0<=index<len(self),'INVALID_BLOCK_POSITION')
        offset,size,expanded,digest=self.entries[index]
        try:record=unpack(loads(self.payload[offset:offset+size]))
        except (ValueError,KeyError,TypeError,RecursionError):raise CodecError('INVALID_BLOCK_RECORD') from None
        original=dumps(record)
        require(len(original)==expanded and hashlib.sha256(original).digest()==digest,'BLOCK_RECORD_INTEGRITY_FAILED')
        return record

    def __iter__(self):
        for index in range(len(self)):yield self.get(index)

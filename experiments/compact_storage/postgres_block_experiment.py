"""Synthetic-only storage experiment, not the application's canonical store.

Measures physical block/pointer/index costs and atomic checkpoint replay. It
does not implement entity merging, field-state precedence or search projection.
"""
from contextlib import contextmanager
import hashlib
import re
from uuid import UUID,uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from block_codec import Block,encode_block,CodecError,require
from wire_codec import dumps,loads
from bigbase.canonical_store import _prepare

DDL='''
CREATE TABLE experiment_meta(singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),purpose text NOT NULL CHECK(purpose='synthetic-compact-block-experiment'));
INSERT INTO experiment_meta VALUES(true,'synthetic-compact-block-experiment');
CREATE TABLE blocks(block_hash bytea PRIMARY KEY CHECK(octet_length(block_hash)=32),payload bytea NOT NULL,records integer NOT NULL CHECK(records BETWEEN 1 AND 128));
CREATE TABLE positions(job_id uuid NOT NULL,record_position bigint NOT NULL CHECK(record_position>=0),block_hash bytea NOT NULL REFERENCES blocks(block_hash),slot integer NOT NULL CHECK(slot BETWEEN 0 AND 127),operation_id uuid NOT NULL,prepared_hash bytea NOT NULL CHECK(octet_length(prepared_hash)=32),actor_wire bytea NOT NULL,received_at timestamptz NOT NULL DEFAULT clock_timestamp(),PRIMARY KEY(job_id,record_position));
CREATE INDEX positions_operation ON positions(operation_id);
CREATE TABLE checkpoints(job_id uuid PRIMARY KEY,cursor_wire bytea NOT NULL,processed bigint NOT NULL CHECK(processed>=0));
CREATE TABLE receipts(job_id uuid NOT NULL,request_id uuid NOT NULL,request_hash bytea NOT NULL CHECK(octet_length(request_hash)=32),start_position bigint NOT NULL,end_position bigint NOT NULL CHECK(end_position>start_position),PRIMARY KEY(job_id,request_id));
CREATE FUNCTION immutable_experiment() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'immutable experiment history' USING ERRCODE='55000'; END $$;
CREATE TRIGGER blocks_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON blocks FOR EACH STATEMENT EXECUTE FUNCTION immutable_experiment();
CREATE TRIGGER positions_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON positions FOR EACH STATEMENT EXECUTE FUNCTION immutable_experiment();
CREATE TRIGGER receipts_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON receipts FOR EACH STATEMENT EXECUTE FUNCTION immutable_experiment();
'''


class BlockExperiment:
    def __init__(self,dsn,schema):
        require(type(schema) is str and re.fullmatch(r'blockexp_[0-9a-f]{32}',schema),'EXPERIMENT_SCHEMA_REQUIRED')
        self.dsn,self.schema=dsn,schema

    @contextmanager
    def connection(self, *, initialize=False):
        with psycopg.connect(self.dsn,row_factory=dict_row,connect_timeout=5) as c:
            identity=c.execute("SELECT current_database() AS database,current_setting('port') AS port,inet_server_addr() AS address").fetchone()
            require(identity=={'database':'bigbase_test','port':'18769','address':None},'ISOLATED_SYNTHETIC_DATABASE_REQUIRED')
            c.execute(sql.SQL('SET LOCAL search_path TO {},pg_catalog').format(sql.Identifier(self.schema)))
            if not initialize:
                require(c.execute('SELECT purpose FROM experiment_meta WHERE singleton').fetchone()=={'purpose':'synthetic-compact-block-experiment'},'EXPERIMENT_IDENTITY_CHANGED')
            yield c

    def initialize(self):
        with self.connection(initialize=True) as c:
            c.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)));c.execute(DDL)

    def append(self,job_id,request_id,records,*,expected_cursor,next_cursor,actor):
        job_id,request_id=UUID(str(job_id)),UUID(str(request_id))
        require(type(actor) is str and 1<=len(actor)<=200,'ACTOR_REQUIRED')
        records=list(records);raw=encode_block(records);block_hash=hashlib.sha256(raw).digest()
        prepared=[_prepare(r) for r in records]
        old_cursor,new_cursor=dumps(expected_cursor),dumps(next_cursor)
        require(old_cursor!=new_cursor,'CHECKPOINT_MUST_ADVANCE')
        request_hash=hashlib.sha256(dumps([str(job_id),str(request_id),block_hash.hex(),expected_cursor,next_cursor,actor])).digest()
        with self.connection() as c:
            c.execute('SELECT pg_advisory_xact_lock(%s)',(int.from_bytes(hashlib.sha256(job_id.bytes).digest()[:8],signed=True),))
            prior=c.execute('SELECT * FROM receipts WHERE job_id=%s AND request_id=%s',(job_id,request_id)).fetchone()
            if prior:
                require(bytes(prior['request_hash'])==request_hash,'REQUEST_IDEMPOTENCY_CONFLICT')
                return {'processed':prior['end_position'],'replayed':True}
            current=c.execute('SELECT * FROM checkpoints WHERE job_id=%s FOR UPDATE',(job_id,)).fetchone()
            expected=bytes(current['cursor_wire']) if current else dumps(None)
            require(expected==old_cursor,'CHECKPOINT_CONFLICT')
            offset=current['processed'] if current else 0
            existing=c.execute('SELECT payload,records FROM blocks WHERE block_hash=%s',(block_hash,)).fetchone()
            if existing:require(bytes(existing['payload'])==raw and existing['records']==len(records),'BLOCK_HASH_COLLISION')
            else:c.execute('INSERT INTO blocks VALUES(%s,%s,%s)',(block_hash,raw,len(records)))
            with c.cursor().copy('COPY positions(job_id,record_position,block_hash,slot,operation_id,prepared_hash,actor_wire) FROM STDIN') as copy:
                for slot,p in enumerate(prepared):
                    copy.write_row((job_id,offset+slot,block_hash,slot,p['operation_id'],bytes.fromhex(p['prepared_hash']),dumps(actor)))
            c.execute('INSERT INTO checkpoints VALUES(%s,%s,%s) ON CONFLICT(job_id) DO UPDATE SET cursor_wire=EXCLUDED.cursor_wire,processed=EXCLUDED.processed',(job_id,new_cursor,offset+len(records)))
            c.execute('INSERT INTO receipts VALUES(%s,%s,%s,%s,%s)',(job_id,request_id,request_hash,offset,offset+len(records)))
            return {'processed':offset+len(records),'replayed':False}

    def get(self,job_id,position):
        require(type(position) is int and position>=0,'INVALID_POSITION')
        with self.connection() as c:
            row=c.execute('SELECT p.*,b.payload FROM positions p JOIN blocks b USING(block_hash) WHERE job_id=%s AND record_position=%s',(UUID(str(job_id)),position)).fetchone()
            require(row is not None,'RECORD_NOT_FOUND')
            raw=bytes(row['payload']);require(hashlib.sha256(raw).digest()==bytes(row['block_hash']),'BLOCK_STORAGE_CORRUPTION')
            record=Block(raw).get(row['slot']);prepared=_prepare(record)
            require(prepared['operation_id']==row['operation_id'] and bytes.fromhex(prepared['prepared_hash'])==bytes(row['prepared_hash']),'POINTER_CONTENT_MISMATCH')
            return {'record':record,'actor':loads(bytes(row['actor_wire'])),'received_at':row['received_at']}

    def size(self):
        with self.connection() as c:
            return dict(c.execute("SELECT sum(pg_total_relation_size(c.oid))::bigint AS bytes,sum(pg_indexes_size(c.oid))::bigint AS index_bytes FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relkind='r'",(self.schema,)).fetchone())

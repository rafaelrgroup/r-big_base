"""Explicit, read-only search activation for a provisioned canonical deployment.

No index creation, consumer, source credentials or generated cursor key. Root
attests the reconciled initial projection; every read keeps that exact binding.
"""
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat

import httpx

from .canonical_search import ElasticProjection, PROJECTION_VERSION, ProjectionError
from .canonical_search_reader import CanonicalSearchReader, SearchReadError


_fstat = os.fstat
HASH = re.compile(r"[0-9a-f]{64}")
NAME = re.compile(r"bigbase-canonical-[a-z0-9][a-z0-9_-]{0,120}")


def need(value, code):
    if not value:
        raise ValueError(code)


def root_bytes(value, maximum=65536):
    """Open through root-owned, non-writable directory descriptors, no links."""
    path = Path(value)
    need(path.is_absolute() and '..' not in path.parts and len(path.parts)>1,
         'SEARCH_PRIVATE_PATH_REQUIRED')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            info = _fstat(fd)
            need(info.st_uid == 0 and not info.st_mode & 0o022, 'SEARCH_PARENT_NOT_PROTECTED')
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = child
        info = _fstat(fd)
        need(info.st_uid == 0 and not info.st_mode & 0o022, 'SEARCH_PARENT_NOT_PROTECTED')
        child = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
        os.close(fd); fd = child
        before = _fstat(fd)
        need(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_uid == 0
             and not before.st_mode & 0o037 and before.st_size <= maximum,
             'SEARCH_PRIVATE_FILE_REQUIRED')
        raw = bytearray()
        while len(raw) <= maximum:
            part = os.read(fd, min(65536, maximum+1-len(raw)))
            if not part: break
            raw.extend(part)
        after = _fstat(fd)
        need(len(raw) <= maximum and (before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)
             == (after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns), 'SEARCH_PRIVATE_FILE_CHANGED')
        return bytes(raw)
    finally:
        os.close(fd)


def object_json(raw):
    def pairs(items):
        result = {}
        for key,value in items:
            need(key not in result, 'SEARCH_DUPLICATE_CONFIGURATION_KEY'); result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('SEARCH_NONFINITE_CONFIGURATION')))
    need(isinstance(value,dict), 'SEARCH_CONFIGURATION_OBJECT_REQUIRED')
    return value


def sha(raw):return hashlib.sha256(raw).hexdigest()


def projection_binding(config):
    fields = {key:config['elasticsearch'][key] for key in
        ('url','cluster_uuid','index_uuid','index','read_alias','projection_version','mapping_sha256')}
    return sha(json.dumps(fields,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode('ascii'))


def validate(config, identity):
    need(set(config)=={'version','enabled','canonical','elasticsearch','cursor_key_file','activation_receipt_file','max_response_bytes'},
         'UNSUPPORTED_SEARCH_CONFIGURATION')
    need(type(config['version']) is int and config['version']==1 and type(config['enabled']) is bool,
         'INVALID_SEARCH_CONFIGURATION_VERSION')
    need(config['canonical']==asdict(identity), 'SEARCH_CANONICAL_IDENTITY_CHANGED')
    es = config['elasticsearch']
    need(isinstance(es,dict) and set(es)=={'url','cluster_uuid','index_uuid','index','read_alias',
         'projection_version','mapping_sha256','api_key_file'}, 'UNSUPPORTED_SEARCH_DESTINATION')
    need(es['url']=='http://127.0.0.1:19200', 'SEARCH_LOOPBACK_DESTINATION_REQUIRED')
    need(all(isinstance(es[k],str) and re.fullmatch(r'[A-Za-z0-9_-]{4,160}',es[k]) for k in ('cluster_uuid','index_uuid')),
         'SEARCH_UUID_REQUIRED')
    need(all(isinstance(es[k],str) and NAME.fullmatch(es[k]) for k in ('index','read_alias'))
         and es['index']!=es['read_alias'], 'SEARCH_EXPLICIT_READ_ALIAS_REQUIRED')
    need(es['projection_version']==PROJECTION_VERSION, 'SEARCH_PROJECTION_VERSION_CHANGED')
    need(isinstance(es['mapping_sha256'],str) and HASH.fullmatch(es['mapping_sha256']), 'SEARCH_MAPPING_DIGEST_REQUIRED')
    need(type(config['max_response_bytes']) is int and 65536<=config['max_response_bytes']<=2*1024*1024,
         'SEARCH_RESPONSE_LIMIT_REQUIRED')
    paths=[config['cursor_key_file'],config['activation_receipt_file'],es['api_key_file']]
    need(all(isinstance(p,str) and Path(p).is_absolute() and '..' not in Path(p).parts for p in paths)
         and len(set(paths))==3, 'SEARCH_PRIVATE_PATH_REQUIRED')


def validate_receipt(receipt, rawconfig, config, identity):
    required={'version','status','scope','completed_at','config_sha256','canonical_deployment_id',
              'projection_binding_sha256','verification_sha256','source_records_verified','entities_expected',
              'entities_verified','indexed_entities','missing_entities','extra_entities','mismatched_entities',
              'unpreserved_fields','omissions_reviewed'}
    need(set(receipt)==required and type(receipt['version']) is int and receipt['version']==1
         and receipt['status']=='verified' and receipt['scope'] in {'pilot_10000','canonical_snapshot'},
         'SEARCH_RECONCILIATION_RECEIPT_REQUIRED')
    need(receipt['config_sha256']==sha(rawconfig) and receipt['canonical_deployment_id']==identity.deployment_id
         and receipt['projection_binding_sha256']==projection_binding(config), 'SEARCH_ACTIVATION_BINDING_CHANGED')
    need(isinstance(receipt['verification_sha256'],str) and HASH.fullmatch(receipt['verification_sha256']),
         'SEARCH_VERIFICATION_DIGEST_REQUIRED')
    for key in ('source_records_verified','entities_expected','entities_verified','indexed_entities',
                'missing_entities','extra_entities','mismatched_entities','unpreserved_fields'):
        need(type(receipt[key]) is int and receipt[key]>=0, 'SEARCH_RECONCILIATION_COUNTS_REQUIRED')
    need(receipt['entities_expected']>0 and receipt['entities_expected']==receipt['entities_verified']==receipt['indexed_entities']
         and receipt['source_records_verified']>=receipt['entities_expected']
         and all(receipt[k]==0 for k in ('missing_entities','extra_entities','mismatched_entities','unpreserved_fields'))
         and receipt['omissions_reviewed'] is True, 'SEARCH_RECONCILIATION_NOT_COMPLETE')
    need(receipt['scope']!='pilot_10000' or receipt['source_records_verified']==10000,'SEARCH_PILOT_COHORT_CHANGED')
    at=datetime.fromisoformat(receipt['completed_at'].replace('Z','+00:00'))
    need(at.tzinfo is not None and (datetime.now(timezone.utc)-at).total_seconds()>=-5,
         'SEARCH_RECEIPT_TIMESTAMP_INVALID')


class PinnedReadTarget(ElasticProjection):
    def __init__(self, client, config):
        self.expected_physical_index=config['elasticsearch']['index']
        self.expected_mapping_hash=config['elasticsearch']['mapping_sha256']
        es=config['elasticsearch']
        super().__init__(client,base_url=es['url'],alias=es['read_alias'],
            expected_cluster_uuid=es['cluster_uuid'],expected_index_uuid=es['index_uuid'],
            max_response_bytes=config['max_response_bytes'])

    def verify_target(self):
        super().verify_target()
        if self.index!=self.expected_physical_index:raise ProjectionError('SEARCH_PHYSICAL_INDEX_CHANGED')
        response=self._request('GET','/'+self.index+'/_mapping')
        if response.status_code!=200:raise ProjectionError('SEARCH_MAPPING_UNAVAILABLE')
        mappings=self._json(response)
        if set(mappings)!={self.index}:raise ProjectionError('SEARCH_MAPPING_DESTINATION_CHANGED')
        fingerprint=sha(json.dumps(mappings[self.index]['mappings'],sort_keys=True,
            separators=(',',':'),ensure_ascii=True).encode('ascii'))
        if fingerprint!=self.expected_mapping_hash:raise ProjectionError('SEARCH_MAPPING_CHANGED')


class SearchRuntime:
    def __init__(self, config_path, config_raw, config, receipt_raw, reader, client, cursor_raw, auth_raw):
        self.config_path,self.config_raw,self.config = config_path,config_raw,config
        self.receipt_raw,self.reader,self.client = receipt_raw,reader,client
        self.cursor_raw,self.auth_raw = cursor_raw,auth_raw
        self.closed = False
        self.coverage_scope=object_json(receipt_raw)['scope']

    def authorize(self):
        try:
            need(not self.closed and root_bytes(self.config_path)==self.config_raw
                 and root_bytes(self.config['activation_receipt_file'])==self.receipt_raw
                 and root_bytes(self.config['cursor_key_file'])==self.cursor_raw
                 and root_bytes(self.config['elasticsearch']['api_key_file'])==self.auth_raw,
                 'SEARCH_ACTIVATION_CHANGED')
        except (OSError,ValueError,TypeError):
            raise SearchReadError('SEARCH_ACTIVATION_UNAVAILABLE') from None

    def available(self):
        try:self.authorize();return True
        except SearchReadError:return False

    def search(self,*args,**kwargs):
        self.authorize()
        result=self.reader.search(*args,**kwargs)
        self.authorize()
        return result

    def close(self,*args,**kwargs):
        # This method releases a caller-authorized PIT, not the shared client.
        self.authorize()
        return self.reader.close(*args,**kwargs)

    def shutdown(self):
        if not self.closed:self.closed=True;self.client.close()


def load_search(path, identity, *, client_factory=httpx.Client):
    raw=root_bytes(path);config=object_json(raw);validate(config,identity)
    if not config['enabled']:return None
    receipt_raw=root_bytes(config['activation_receipt_file'])
    validate_receipt(object_json(receipt_raw),raw,config,identity)
    auth_raw=root_bytes(config['elasticsearch']['api_key_file']);auth=object_json(auth_raw)
    need(set(auth)=={'api_key'} and isinstance(auth['api_key'],str)
         and re.fullmatch(r'[A-Za-z0-9+/=_-]{8,4096}',auth['api_key']), 'SEARCH_READ_API_KEY_REQUIRED')
    cursor_raw=root_bytes(config['cursor_key_file']);key=cursor_raw.strip()
    client=client_factory(headers={'Authorization':'ApiKey '+auth['api_key']},follow_redirects=False,trust_env=False,
        timeout=httpx.Timeout(10,connect=3,pool=3),limits=httpx.Limits(max_connections=32,max_keepalive_connections=16))
    try:
        es=config['elasticsearch']
        reader=CanonicalSearchReader(client,base_url=es['url'],alias=es['read_alias'],
            expected_cluster_uuid=es['cluster_uuid'],expected_index_uuid=es['index_uuid'],
            cursor_key=key,max_response_bytes=config['max_response_bytes'])
        reader.target=PinnedReadTarget(client,config)
        reader.target.verify_target()
        result=SearchRuntime(path,raw,config,receipt_raw,reader,client,cursor_raw,auth_raw)
        result.authorize()
        return result
    except BaseException:
        client.close();raise

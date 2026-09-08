"""Bounded migration readers. No connection or data write occurs on import.

The caller commits each page with its checkpoint in the canonical transaction.
An expired PIT is an explicit interruption: its shard positions are never reused
against a new PIT. Real migrations require a separately approved destination.
"""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import time
from urllib.parse import quote, urlsplit

import httpx


class MigrationReadError(RuntimeError):
    """Safe operational code; never includes source records or credentials."""


class MigrationInterrupted(MigrationReadError):
    pass


def decode_source_json(raw):
    from .source_adapters import ExactDecimal

    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise MigrationReadError('DUPLICATE_JSON_KEY')
            result[key] = value
        return result

    def invalid_constant(_):
        raise MigrationReadError('NON_FINITE_JSON_NUMBER')

    try:
        return json.loads(raw, parse_float=ExactDecimal, object_pairs_hook=object_pairs,
                          parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise MigrationReadError('INVALID_SOURCE_JSON') from None


@dataclass(frozen=True)
class Page:
    records: list
    checkpoint: dict
    complete: bool = False


def _page_size(value):
    if type(value) is not int or not 1 <= value <= 1000:
        raise ValueError('page_size must be between 1 and 1000')
    return value


class JsonlSource:
    """Immutable, checksummed local input; one source envelope per line."""

    def __init__(self, path, *, page_size=100, max_line_bytes=2 * 1024 * 1024):
        self.path = Path(path)
        self.page_size = _page_size(page_size)
        if type(max_line_bytes) is not int or not 1 <= max_line_bytes <= 16 * 1024 * 1024:
            raise ValueError('Invalid maximum source line size')
        self.max_line_bytes = max_line_bytes
        self.file = None

    def __enter__(self):
        self.file = self.path.open('rb')
        try:
            self.stat = self._stat()
            digest = hashlib.sha256()
            while block := self.file.read(1024 * 1024):
                digest.update(block)
            self.identity = {'kind': 'jsonl', 'sha256': digest.hexdigest(), 'bytes': self.stat[2]}
            self._unchanged()
            self.file.seek(0)
            return self
        except BaseException:
            self.file.close()
            raise

    def __exit__(self, *_):
        if self.file:
            self.file.close()

    def _stat(self):
        import os
        s = os.fstat(self.file.fileno())
        return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns

    def _unchanged(self):
        if self._stat() != self.stat:
            raise MigrationInterrupted('SOURCE_FILE_CHANGED')

    def rewind(self):
        self._unchanged()
        self.file.seek(0)

    def pages(self, checkpoint=None):
        checkpoint = checkpoint or {}
        if checkpoint and checkpoint.get('source') != self.identity:
            raise MigrationInterrupted('SOURCE_IDENTITY_CHANGED')
        offset, seen = checkpoint.get('offset', 0), checkpoint.get('seen', 0)
        if type(offset) is not int or not 0 <= offset <= self.stat[2] or type(seen) is not int or seen < 0:
            raise MigrationInterrupted('INVALID_CHECKPOINT')
        if offset:
            self.file.seek(offset - 1)
            if self.file.read(1) != b'\n' and offset != self.stat[2]:
                raise MigrationInterrupted('CHECKPOINT_NOT_AT_LINE_BOUNDARY')
        self.file.seek(offset)
        if checkpoint.get('complete'):
            if offset != self.stat[2]:
                raise MigrationInterrupted('INVALID_COMPLETE_CHECKPOINT')
            self._unchanged()
            return
        while True:
            self._unchanged()
            records = []
            eof = False
            for _ in range(self.page_size):
                line = self.file.readline(self.max_line_bytes + 1)
                if not line:
                    eof = True
                    break
                if len(line) > self.max_line_bytes:
                    raise MigrationReadError('SOURCE_LINE_TOO_LARGE')
                value = decode_source_json(line)
                if not isinstance(value, dict) or set(value) - {'source_id', 'external_id', 'source_version', 'record'}:
                    raise MigrationReadError('INVALID_SOURCE_ENVELOPE')
                if value.get('source_id') not in {'pessoas', 'pessoas_serasa'}:
                    raise MigrationReadError('UNKNOWN_SOURCE_INDEX')
                if not isinstance(value.get('external_id'), str) or not value['external_id'] or not isinstance(value.get('record'), dict):
                    raise MigrationReadError('INVALID_SOURCE_ENVELOPE')
                if value.get('source_version') is not None and type(value['source_version']) not in (str, int):
                    raise MigrationReadError('INVALID_SOURCE_VERSION')
                records.append(value)
            self._unchanged()
            seen += len(records)
            next_checkpoint = {'source': self.identity, 'offset': self.file.tell(), 'seen': seen, 'complete': eof}
            yield Page(records, next_checkpoint, eof)
            if eof:
                return


class ElasticsearchPitSource:
    """Read-only ES transport, pinned to one index UUID and cluster.

Use a restored, immutable source for complete migration. This reader does not
freeze writes, restore snapshots, create indices, or silently open a new PIT.
"""

    def __init__(self, base_url, index, source_id, *, expected_cluster_uuid, expected_index_uuid,
                 page_size=100, max_response_bytes=16 * 1024 * 1024, headers=None,
                 client=None, page_interval=0.25, require_immutable=False,
                 expected_count=None, expected_mapping_sha256=None):
        parsed = urlsplit(base_url)
        if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
            raise ValueError('Provide a base URL without credentials, path or query')
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
            raise ValueError('Invalid source URL')
        if parsed.scheme == 'http' and parsed.hostname not in {'localhost', '127.0.0.1', '::1'}:
            raise ValueError('Remote source connections require HTTPS')
        if source_id not in {'pessoas', 'pessoas_serasa'} or not isinstance(index, str) or not index or any(c in index for c in '/*?,#'):
            raise ValueError('One explicit source index is required')
        if not expected_cluster_uuid or not expected_index_uuid:
            raise ValueError('Pin both source cluster and index identities')
        if not 1024 <= max_response_bytes <= 64 * 1024 * 1024 or not 0 <= page_interval <= 60:
            raise ValueError('Invalid transport limits')
        self.index, self.source_id = index, source_id
        self.expected_cluster_uuid, self.expected_index_uuid = expected_cluster_uuid, expected_index_uuid
        self.page_size, self.max_response_bytes = _page_size(page_size), max_response_bytes
        self.page_interval = page_interval
        if type(require_immutable) is not bool:
            raise ValueError('require_immutable must be a boolean')
        if require_immutable and (type(expected_count) is not int or expected_count < 0
                                  or not isinstance(expected_mapping_sha256, str)
                                  or len(expected_mapping_sha256) != 64):
            raise ValueError('Immutable migration requires approved count and mapping hash')
        self.require_immutable = require_immutable
        self.expected_count, self.expected_mapping_sha256 = expected_count, expected_mapping_sha256
        self.base_url = base_url.rstrip('/')
        self.owned_client = client is None
        self.client = client or httpx.Client(base_url=base_url.rstrip('/'), headers=headers,
                                            timeout=30, follow_redirects=False, trust_env=False,
                                            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1))
        self.pit_id = None
        self.exhausted = False
        self.close_error = None

    def _request(self, method, path, body=None):
        try:
            with self.client.stream(method, self.base_url + path, json=body,
                                    timeout=30, follow_redirects=False,
                                    headers={'Accept-Encoding': 'identity'}) as response:
                if response.status_code == 404 and path.split('?', 1)[0] == '/_search':
                    raise MigrationInterrupted('PIT_EXPIRED_RESTART_REQUIRED')
                if response.status_code >= 300:
                    raise MigrationReadError('SOURCE_HTTP_' + str(response.status_code))
                if response.headers.get('content-encoding', 'identity').strip().lower() not in {'', 'identity'}:
                    raise MigrationReadError('COMPRESSED_SOURCE_RESPONSE_NOT_ALLOWED')
                content = bytearray()
                for chunk in response.iter_bytes(chunk_size=65536):
                    content.extend(chunk)
                    if len(content) > self.max_response_bytes:
                        raise MigrationReadError('SOURCE_RESPONSE_TOO_LARGE')
                result = decode_source_json(bytes(content))
                if not isinstance(result, dict):
                    raise MigrationReadError('INVALID_SOURCE_RESPONSE')
                return result
        except httpx.HTTPError:
            raise MigrationReadError('SOURCE_CONNECTION_FAILED') from None

    def __enter__(self):
        try:
            root = self._request('GET', '/')
            settings = self._request('GET', '/' + quote(self.index, safe='') + '/_settings?flat_settings=true')
            actual = settings.get(self.index, {}).get('settings', {}).get('index.uuid')
            if root.get('cluster_uuid') != self.expected_cluster_uuid or actual != self.expected_index_uuid:
                raise MigrationInterrupted('SOURCE_IDENTITY_CHANGED')
            if self.require_immutable:
                options = settings[self.index]['settings']
                if not any(options.get(key) is True or options.get(key) == 'true' for key in
                           ['index.blocks.write', 'index.blocks.read_only', 'index.blocks.read_only_allow_delete']):
                    raise MigrationInterrupted('FULL_MIGRATION_REQUIRES_IMMUTABLE_SOURCE')
                count = self._request('GET', '/' + quote(self.index, safe='') + '/_count')
                mapping = self._request('GET', '/' + quote(self.index, safe='') + '/_mapping')
                digest = hashlib.sha256(json.dumps(mapping.get(self.index, {}).get('mappings'),
                    sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()
                if (type(count.get('count')) is not int or count['count'] != self.expected_count
                        or count.get('_shards', {}).get('failed') != 0
                        or digest != self.expected_mapping_sha256):
                    raise MigrationInterrupted('SOURCE_APPROVED_CONTENT_MISMATCH')
            self.identity = {'kind': 'elasticsearch_pit', 'cluster_uuid': root['cluster_uuid'],
                             'index_uuid': actual, 'index': self.index, 'source_id': self.source_id}
            return self
        except BaseException:
            if self.owned_client:
                self.client.close()
            raise

    def __exit__(self, exc_type, *_):
        # A failed destination commit still has a durable checkpoint referring
        # to this PIT. Preserve it until keep_alive expires so an immediate
        # restart can resume; deleting it here would force a full source scan.
        # Successful/explicitly stopped contexts release their PIT normally.
        if self.pit_id and (exc_type is None or self.exhausted):
            try:
                self._request('DELETE', '/_pit', {'id': self.pit_id})
            except MigrationReadError as exc:
                self.close_error = str(exc)
        if self.owned_client:
            self.client.close()

    def rewind(self):
        """A second immutable-source pass for destination reconciliation."""
        if not self.require_immutable:
            raise MigrationInterrupted('RECONCILIATION_REQUIRES_IMMUTABLE_SOURCE')
        if self.pit_id:
            self._request('DELETE', '/_pit', {'id': self.pit_id})
            self.pit_id = None
        self.exhausted = False
        # Recheck cluster/index/count/mapping and protection before the new PIT.
        self.__enter__()

    def pages(self, checkpoint=None):
        checkpoint = checkpoint or {}
        if checkpoint and checkpoint.get('source') != self.identity:
            raise MigrationInterrupted('SOURCE_IDENTITY_CHANGED')
        seen = checkpoint.get('seen', 0)
        if type(seen) is not int or seen < 0:
            raise MigrationInterrupted('INVALID_CHECKPOINT')
        if checkpoint.get('complete'):
            return
        self.pit_id = checkpoint.get('pit_id')
        position = checkpoint.get('search_after')
        if checkpoint and (not isinstance(self.pit_id, str) or not self.pit_id or not isinstance(position, list)
                           or len(position) != 1 or type(position[0]) is not int):
            raise MigrationInterrupted('INVALID_CHECKPOINT')
        if not self.pit_id:
            opened = self._request('POST', '/' + quote(self.index, safe='') + '/_pit?keep_alive=5m&allow_partial_search_results=false')
            self.pit_id = opened.get('id')
            if not isinstance(self.pit_id, str) or not self.pit_id or opened.get('_shards', {}).get('failed', 0):
                raise MigrationReadError('PIT_NOT_COMPLETE')
        while True:
            body = {'size': self.page_size, 'pit': {'id': self.pit_id, 'keep_alive': '5m'},
                    'sort': [{'_shard_doc': 'asc'}], 'track_total_hits': False,
                    'seq_no_primary_term': True, 'query': {'match_all': {}}}
            if position is not None:
                body['search_after'] = position
            result = self._request('POST', '/_search?allow_partial_search_results=false', body)
            shards = result.get('_shards', {})
            if (result.get('timed_out') is not False or type(shards.get('failed')) is not int
                    or shards['failed'] != 0 or type(shards.get('total')) is not int
                    or type(shards.get('successful')) is not int or shards['successful'] != shards['total']):
                raise MigrationReadError('INCOMPLETE_SOURCE_PAGE')
            if 'pit_id' in result:
                if not isinstance(result['pit_id'], str) or not result['pit_id']:
                    raise MigrationReadError('INVALID_SOURCE_PAGE')
                self.pit_id = result['pit_id']
            hits = result.get('hits', {}).get('hits')
            if not isinstance(hits, list) or len(hits) > self.page_size:
                raise MigrationReadError('INVALID_SOURCE_PAGE')
            records, positions = [], set()
            for hit in hits:
                order = hit.get('sort')
                if (hit.get('_index') != self.index or not isinstance(hit.get('_source'), dict)
                        or not isinstance(hit.get('_id'), str) or not hit['_id']
                        or not isinstance(order, list) or len(order) != 1 or type(order[0]) is not int
                        or order[0] in positions or (position is not None and order[0] <= position[0])
                        or type(hit.get('_seq_no')) is not int or type(hit.get('_primary_term')) is not int):
                    raise MigrationReadError('INVALID_SOURCE_HIT')
                positions.add(order[0])
                position = order
                records.append({'source_id': self.source_id, 'external_id': hit['_id'],
                                'source_version': str(hit['_primary_term']) + ':' + str(hit['_seq_no']),
                                'record': hit['_source']})
            seen += len(records)
            complete = not hits
            self.exhausted = complete
            next_checkpoint = {'source': self.identity, 'pit_id': self.pit_id,
                               'search_after': position or [], 'seen': seen, 'complete': complete}
            yield Page(records, next_checkpoint, complete)
            if complete:
                return
            time.sleep(self.page_interval)


def prepare_page(rows, mapper, *, max_atoms=20000, max_prepared_bytes=16 * 1024 * 1024):
    """Bound transformed data too: a small JSON leaf expands into provenance.

Limits stop before committing a partial page. The same persisted cursor remains
valid with a smaller page_size. Single oversized records require an explicit
large-record pathway; no leaf is truncated or silently skipped.
"""
    from .source_adapters import exact_json
    if type(max_atoms) is not int or not 1 <= max_atoms <= 20000 or type(max_prepared_bytes) is not int or not 1 <= max_prepared_bytes <= 64 * 1024 * 1024:
        raise ValueError('Invalid prepared page limits')
    prepared, atoms, size = [], 0, 0
    for row in rows:
        record = mapper(row['source_id'], row['external_id'], row['record'], source_version=row.get('source_version'))
        atoms += len(record.get('facts', []))
        size += len(exact_json(record))  # ASCII JSON: character count equals bytes.
        if atoms > max_atoms or size > max_prepared_bytes:
            raise MigrationInterrupted('PREPARED_PAGE_LIMIT_REDUCE_PAGE_SIZE')
        prepared.append(record)
    return prepared


def transfer_pages(reader, store, job_id, *, checkpoint, mapper, actor_id, cancelled=lambda: False,
                   progress=None, max_atoms=20000, max_prepared_bytes=16 * 1024 * 1024):
    """Commit-before-advance: a rejected page leaves its checkpoint unchanged."""
    current = checkpoint
    counter = (current or {}).get('seen', 0)
    for page in reader.pages(current):
        if cancelled():
            return {'state': 'cancelled', 'checkpoint': current}
        records = prepare_page(page.records, mapper, max_atoms=max_atoms, max_prepared_bytes=max_prepared_bytes)
        if not records:
            if not page.complete:
                raise MigrationReadError('EMPTY_NONTERMINAL_PAGE')
            return {'state': 'source_exhausted', 'checkpoint': page.checkpoint,
                    'records_processed': counter}
        receipt = store.apply_batch(records, job_id=job_id, expected_checkpoint=counter,
                          next_checkpoint=counter + len(records), next_cursor=page.checkpoint,
                          actor_id=actor_id)
        current = page.checkpoint
        counter += len(records)
        if progress:
            progress(counter, receipt)
        if page.complete:
            return {'state': 'source_exhausted', 'checkpoint': current, 'records_processed': counter}
    return {'state': 'source_exhausted' if (current or {}).get('complete') else 'interrupted',
            'checkpoint': current, 'records_processed': counter}

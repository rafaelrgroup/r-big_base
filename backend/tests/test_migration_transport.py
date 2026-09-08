import json
import hashlib
from decimal import Decimal

import httpx
import pytest

from bigbase.migration_transport import (
    ElasticsearchPitSource, JsonlSource, MigrationInterrupted,
    MigrationReadError, Page, decode_source_json, transfer_pages,
    prepare_page,
)


def envelope(i, record=None):
    return {'source_id': 'pessoas', 'external_id': str(i), 'source_version': '1',
            'record': record if record is not None else {'NOME': 'Pessoa sintética ' + str(i)}}


def source_file(tmp_path, count=5):
    path = tmp_path / 'synthetic.jsonl'
    path.write_text(''.join(json.dumps(envelope(i)) + '\n' for i in range(count)))
    return path


def test_jsonl_pages_resume_and_terminal_checkpoint(tmp_path):
    path = source_file(tmp_path)
    with JsonlSource(path, page_size=2) as reader:
        first = next(reader.pages())
    with JsonlSource(path, page_size=2) as reader:
        tail = list(reader.pages(first.checkpoint))
        assert [r['external_id'] for p in tail for r in p.records] == ['2', '3', '4']
        assert tail[-1].complete and tail[-1].checkpoint['seen'] == 5
        assert list(reader.pages(tail[-1].checkpoint)) == []


def test_jsonl_source_changed_between_runs_or_during_read_fails(tmp_path):
    path = source_file(tmp_path)
    with JsonlSource(path, page_size=1) as reader:
        page = next(reader.pages())
        with path.open('a') as out:
            out.write(json.dumps(envelope(99)) + '\n')
        with pytest.raises(MigrationInterrupted, match='SOURCE_FILE_CHANGED'):
            next(reader.pages(page.checkpoint))
    with JsonlSource(path) as reader:
        with pytest.raises(MigrationInterrupted, match='SOURCE_IDENTITY_CHANGED'):
            next(reader.pages(page.checkpoint))


def test_jsonl_oversized_invalid_and_duplicate_inputs_never_skip(tmp_path):
    path = tmp_path / 'input.jsonl'
    cases = [(b'x' * 200, 100, 'TOO_LARGE'), (b'{"record":1,"record":2}\n', 200, 'DUPLICATE'),
             (b'not-json\n', 200, 'INVALID_SOURCE_JSON'), (b'{}\n', 200, 'UNKNOWN_SOURCE_INDEX')]
    for raw, limit, error in cases:
        path.write_bytes(raw)
        with JsonlSource(path, max_line_bytes=limit) as reader:
            with pytest.raises(MigrationReadError, match=error):
                list(reader.pages())


def test_jsonl_rejects_wrong_cursor_boundary_and_boolean_offset(tmp_path):
    with JsonlSource(source_file(tmp_path)) as reader:
        for offset in (True, -1, 3):
            with pytest.raises(MigrationInterrupted):
                list(reader.pages({'source': reader.identity, 'offset': offset, 'seen': 0}))


def test_exact_source_json_preserves_numbers_and_null_characters():
    value = decode_source_json(b'{"large":9007199254740993123456,"decimal":1.2300000000000000000001e-10,"s":"a\\u0000b","empty":[],"false":false,"null":null}')
    assert value['large'] == 9007199254740993123456
    assert isinstance(value['decimal'], Decimal)
    assert value['decimal'] == Decimal('1.2300000000000000000001e-10')
    assert value['decimal'].json_lexeme == '1.2300000000000000000001e-10'
    assert value['s'] == 'a\x00b' and value['empty'] == []
    assert value['false'] is False and value['null'] is None
    for raw in (b'NaN', b'Infinity', b'{"a":1,"\\u0061":1}'):
        with pytest.raises(MigrationReadError):
            decode_source_json(raw)


def hit(number):
    return {'_index': 'restored_people', '_id': str(number), '_seq_no': number,
            '_primary_term': 1, 'sort': [number], '_source': {'NOME': 'Sintética'}}


def page(hits, pit_id='pit-next', **overrides):
    return {'timed_out': False, '_shards': {'total': 1, 'successful': 1, 'failed': 0},
            'hits': {'hits': hits}, 'pit_id': pit_id, **overrides}


def es_reader(pages, *, cluster='cluster', index_uuid='index', options=None, count=2, mapping=None, **kwargs):
    requests = []
    iterator = iter(pages)

    def handle(request):
        body = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, body))
        if request.url.path == '/':
            return httpx.Response(200, json={'cluster_uuid': cluster})
        if request.url.path.endswith('/_settings'):
            return httpx.Response(200, json={'restored_people': {'settings': {'index.uuid': index_uuid, **(options or {})}}})
        if request.url.path.endswith('/_count'):
            return httpx.Response(200, json={'count': count, '_shards': {'failed': 0}})
        if request.url.path.endswith('/_mapping'):
            return httpx.Response(200, json={'restored_people': {'mappings': mapping or {}}})
        if request.url.path == '/restored_people/_pit':
            return httpx.Response(200, json={'id': 'pit-initial', '_shards': {'failed': 0}})
        if request.url.path == '/_pit':
            return httpx.Response(200, json={'succeeded': True})
        result = next(iterator)
        return result if isinstance(result, httpx.Response) else httpx.Response(200, json=result)

    client = httpx.Client(base_url='https://synthetic.invalid', transport=httpx.MockTransport(handle))
    reader = ElasticsearchPitSource('https://synthetic.invalid', 'restored_people', 'pessoas',
                                    expected_cluster_uuid='cluster', expected_index_uuid='index',
                                    client=client, page_size=2, page_interval=0, **kwargs)
    return reader, client, requests


@pytest.mark.parametrize('violation,code', [('mutable', 'IMMUTABLE_SOURCE'), ('count', 'CONTENT_MISMATCH'), ('mapping', 'CONTENT_MISMATCH')])
def test_es_full_migration_rejects_unapproved_content_before_pit(violation, code):
    mapping = {'properties': {'nome': {'type': 'keyword'}}}
    expected = hashlib.sha256(json.dumps(mapping, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    reader, client, requests = es_reader([], require_immutable=True,
        options={} if violation == 'mutable' else {'index.blocks.write': 'true'},
        count=3 if violation == 'count' else 2, mapping={} if violation == 'mapping' else mapping,
        expected_count=2, expected_mapping_sha256=expected)
    with client, pytest.raises(MigrationInterrupted, match=code):
        with reader:
            pass
    assert not any(path in ('/_search', '/restored_people/_pit') for _, path, _ in requests)


def test_es_full_migration_checks_metadata_then_reads_approved_immutable_index():
    digest = hashlib.sha256(b'{}').hexdigest()
    reader, client, requests = es_reader([page([hit(1), hit(2)]), page([])], require_immutable=True,
        options={'index.blocks.write': True}, expected_count=2, expected_mapping_sha256=digest)
    with client, reader:
        assert sum(len(p.records) for p in reader.pages()) == 2
    assert [path for _, path, _ in requests[:4]] == ['/', '/restored_people/_settings', '/restored_people/_count', '/restored_people/_mapping']


def test_es_reconciliation_rewinds_only_immutable_source_and_closes_old_pit():
    digest=hashlib.sha256(b'{}').hexdigest()
    reader,client,requests=es_reader([page([hit(1)]),page([]),page([hit(1)]),page([])],require_immutable=True,
        options={'index.blocks.write':True},expected_count=2,expected_mapping_sha256=digest)
    with client,reader:
        assert sum(len(p.records) for p in reader.pages()) == 1
        reader.rewind()
        assert sum(len(p.records) for p in reader.pages()) == 1
    assert sum(path == '/restored_people/_pit' for _,path,_ in requests)==2
    assert sum(method == 'DELETE' for method,_,_ in requests)==2
    mutable,client,_=es_reader([])
    with client,mutable,pytest.raises(MigrationInterrupted,match='IMMUTABLE_SOURCE'):
        mutable.rewind()


def test_jsonl_source_version_boolean_is_not_an_integer(tmp_path):
    path = tmp_path / 'bad-version.jsonl'
    path.write_text(json.dumps({**envelope(0), 'source_version': True}) + '\n')
    with JsonlSource(path) as reader, pytest.raises(MigrationReadError, match='INVALID_SOURCE_VERSION'):
        list(reader.pages())


def test_es_supplied_client_cannot_change_the_validated_destination():
    urls = []
    def handle(request):
        urls.append(str(request.url))
        if request.url.path == '/':
            return httpx.Response(200,json={'cluster_uuid':'cluster'})
        return httpx.Response(200,json={'restored_people':{'settings':{'index.uuid':'index'}}})
    with httpx.Client(base_url='http://wrong.invalid',transport=httpx.MockTransport(handle)) as client:
        with ElasticsearchPitSource('https://approved.invalid','restored_people','pessoas',expected_cluster_uuid='cluster',expected_index_uuid='index',client=client):
            pass
    assert all(url.startswith('https://approved.invalid/') for url in urls)


def test_prepared_page_budget_stops_incrementally_without_committing_partial_page(tmp_path):
    mapped_ids = []
    class Store:
        def apply_batch(self,*args,**kwargs):
            pytest.fail('Oversized prepared page cannot commit a subset with final-page cursor')
    def mapper(source, external, record, source_version=None):
        mapped_ids.append(external)
        return {'facts':[{'input_json':'x'*1000}]}
    path=source_file(tmp_path,5)
    with JsonlSource(path,page_size=5) as reader,pytest.raises(MigrationInterrupted,match='REDUCE_PAGE_SIZE'):
        transfer_pages(reader,Store(),'test',checkpoint=None,mapper=mapper,actor_id='test',max_prepared_bytes=1500)
    assert mapped_ids == ['0','1']
    with pytest.raises(MigrationInterrupted,match='REDUCE_PAGE_SIZE'):
        prepare_page([envelope(1),envelope(2)],lambda *args,**kwargs:{'facts':[{},{}]},max_atoms=3)


def test_progress_is_emitted_only_for_committed_pages(tmp_path):
    updates=[]
    class Store:
        def apply_batch(self,*args,**kwargs):
            if kwargs['expected_checkpoint']:
                raise RuntimeError('synthetic-failure')
            return {'committed':True}
    with JsonlSource(source_file(tmp_path,3),page_size=2) as reader,pytest.raises(RuntimeError):
        transfer_pages(reader,Store(),'test',checkpoint=None,mapper=lambda *args,**kwargs:{},actor_id='test',progress=lambda count,receipt:updates.append((count,receipt)))
    assert updates == [(2,{'committed':True})]


def test_es_uses_latest_pit_and_committable_position_and_closes_only_own_pit():
    reader, client, requests = es_reader([page([hit(1), hit(2)]), page([])])
    with client, reader:
        pages = list(reader.pages())
    assert pages[0].checkpoint['pit_id'] == 'pit-next'
    assert pages[-1].complete and pages[-1].checkpoint['seen'] == 2
    searches = [body for _, path, body in requests if path == '/_search']
    assert searches[0]['pit']['id'] == 'pit-initial'
    assert searches[1]['pit']['id'] == 'pit-next' and searches[1]['search_after'] == [2]
    assert requests[-1] == ('DELETE', '/_pit', {'id': 'pit-next'})
    assert all(path in ('/', '/restored_people/_settings', '/restored_people/_pit', '/_search', '/_pit')
               for _, path, _ in requests)


def test_es_expired_pit_never_reopens_or_reuses_position_on_new_pit():
    reader, client, requests = es_reader([httpx.Response(404, json={'error': 'sensitive data'})])
    with client, reader:
        cursor = {'source': reader.identity, 'pit_id': 'previous-pit', 'search_after': [50], 'seen': 50}
        with pytest.raises(MigrationInterrupted, match='PIT_EXPIRED_RESTART_REQUIRED'):
            list(reader.pages(cursor))
    assert not any(path == '/restored_people/_pit' for _, path, _ in requests)


@pytest.mark.parametrize('bad', [page([], timed_out=True), page([], _shards={'total': 2, 'successful': 1, 'failed': 1}),
                                 page([hit(1), hit(1)]), page([hit(2), hit(1)]), page([], pit_id=123)])
def test_es_incomplete_malformed_or_duplicate_page_never_yields(bad):
    reader, client, _ = es_reader([bad])
    with client, reader, pytest.raises(MigrationReadError):
        next(reader.pages())


def test_es_identity_failure_prevents_pit_or_data_reads():
    reader, client, requests = es_reader([], cluster='other-cluster')
    with client, pytest.raises(MigrationInterrupted, match='SOURCE_IDENTITY_CHANGED'):
        with reader:
            pass
    assert not any(path in ('/_search', '/restored_people/_pit') for _, path, _ in requests)


def test_es_destination_failure_keeps_pit_for_resume_from_committed_page():
    reader, client, requests = es_reader([page([hit(1), hit(2)], 'pit-committed'), page([hit(3)], 'pit-latest')])
    committed = []
    class Store:
        def apply_batch(self, records, **kwargs):
            if kwargs['expected_checkpoint'] == 2:
                raise RuntimeError('synthetic-destination-failure')
            committed.append(kwargs)
    mapper = lambda source, external, record, source_version=None: {'id': external}
    with client, pytest.raises(RuntimeError, match='synthetic-destination-failure'):
        with reader:
            transfer_pages(reader, Store(), 'synthetic-job', checkpoint=None, mapper=mapper, actor_id='test')
    assert not any(method == 'DELETE' for method, _, _ in requests)
    assert committed[0]['next_cursor']['pit_id'] == 'pit-committed'
    resumed, resumed_client, resumed_requests = es_reader([page([hit(3)], 'pit-after-resume'), page([])])
    class ResumedStore:
        def apply_batch(self, records, **kwargs):
            assert kwargs['expected_checkpoint'] == 2 and kwargs['next_checkpoint'] == 3
    with resumed_client, resumed:
        result = transfer_pages(resumed, ResumedStore(), 'synthetic-job', checkpoint=committed[0]['next_cursor'], mapper=mapper, actor_id='test')
    assert result['records_processed'] == 3
    assert not any(path == '/restored_people/_pit' for _, path, _ in resumed_requests)
    assert resumed_requests[-1][0] == 'DELETE'


def test_es_response_limit_and_errors_do_not_disclose_server_body():
    for response, code in [(httpx.Response(503, text='SECRET-SOURCE'), 'SOURCE_HTTP_503'),
                           (httpx.Response(200, text='x' * 2048), 'SOURCE_RESPONSE_TOO_LARGE')]:
        reader, client, _ = es_reader([response], max_response_bytes=1024)
        with client, reader, pytest.raises(MigrationReadError) as error:
            list(reader.pages())
        assert str(error.value) == code


def test_es_refuses_compressed_stream_before_expanding_it():
    class UnreadCompressed(httpx.SyncByteStream):
        def __iter__(self):
            pytest.fail('Compressed body must not be read or expanded')
    response=httpx.Response(200,headers={'Content-Encoding':'gzip'},stream=UnreadCompressed())
    reader,client,_=es_reader([response])
    with client,reader,pytest.raises(MigrationReadError,match='COMPRESSED_SOURCE_RESPONSE_NOT_ALLOWED'):
        next(reader.pages())


def test_transfers_advance_only_after_atomic_commit_and_support_replay(tmp_path):
    path = source_file(tmp_path, 3)
    committed = []

    class Store:
        def apply_batch(self, records, **kwargs):
            if kwargs['expected_checkpoint'] == 2:
                raise RuntimeError('simulated-rollback')
            committed.append((records, kwargs))

    def mapper(source, external, record, source_version=None):
        return {'id': external, 'record': record}

    with JsonlSource(path, page_size=2) as reader, pytest.raises(RuntimeError, match='simulated-rollback'):
        transfer_pages(reader, Store(), 'job', checkpoint=None, mapper=mapper, actor_id='synthetic')
    assert len(committed) == 1 and committed[0][1]['next_checkpoint'] == 2
    checkpoint = committed[0][1]['next_cursor']

    class ResumedStore:
        def apply_batch(self, records, **kwargs):
            assert kwargs['expected_checkpoint'] == 2 and kwargs['next_checkpoint'] == 3
            assert [r['id'] for r in records] == ['2']

    with JsonlSource(path, page_size=2) as reader:
        result = transfer_pages(reader, ResumedStore(), 'job', checkpoint=checkpoint,
                                mapper=mapper, actor_id='synthetic')
    assert result['state'] == 'source_exhausted' and result['records_processed'] == 3


def test_cancel_and_empty_source_do_not_issue_writes(tmp_path):
    class NoWrites:
        def apply_batch(self, *_args, **_kwargs):
            pytest.fail('Unexpected write')

    with JsonlSource(source_file(tmp_path)) as reader:
        result = transfer_pages(reader, NoWrites(), 'job', checkpoint=None, mapper=None,
                                actor_id='synthetic', cancelled=lambda: True)
        assert result['state'] == 'cancelled' and result['checkpoint'] is None
    with JsonlSource(source_file(tmp_path, 0)) as reader:
        result = transfer_pages(reader, NoWrites(), 'job', checkpoint=None, mapper=None, actor_id='synthetic')
        assert result['state'] == 'source_exhausted' and result['records_processed'] == 0


def test_remote_plaintext_and_url_secrets_are_rejected_before_connection():
    for url in ['http://remote.invalid', 'https://user:password@remote.invalid', 'https://remote.invalid?apikey=secret']:
        with pytest.raises(ValueError):
            ElasticsearchPitSource(url, 'index', 'pessoas', expected_cluster_uuid='c', expected_index_uuid='i')

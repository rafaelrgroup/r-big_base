"""Synthetic projection/query tests and HTTP fakes; no ES/PG connection."""
from copy import deepcopy
from decimal import Decimal, localcontext
import json
from uuid import uuid4

import httpx
import pytest

from bigbase.canonical_search import (
    ElasticProjection, OutboxConsumer, PROJECTION_VERSION, ProjectionError,
    build_projection, build_query, index_definition,
)


OWNER = "03f4d773-b699-4aa5-92d6-b1f518ec91c6"
INDEX = "bigbase-search-synthetic-000001"
ALIAS = "bigbase-search-synthetic-write"
CLUSTER_UUID = "synthetic-cluster"
INDEX_UUID = "synthetic-index"


def field(path, value, **extras):
    return {"path": path, "value": value, "source_id": "synthetic-source", "status": "mapped",
            "metadata": {}, "observation_id": str(uuid4()), "effective_at": None,
            "received_at": "2026-09-08T01:00:00+00:00", **extras}


def item(kind, fields, flags=None):
    return {"id": str(uuid4()), "kind": kind, "fields": fields, "flags": flags or []}


def entity(items=None, version=1):
    return {"id": OWNER, "entity_type": "person", "version": version, "items": items or []}


def flag(path, name, value, **extras):
    return {**field(path, value), "name": name, "applicable": True, **extras}


def flat_fields(document):
    return [field for item in document["items"] for field in item["fields"]]


def successful(document, **changes):
    return {"_id": document["id"], "_index": INDEX, "_version": document["record_version"],
            "result": "updated", "_shards": {"total": 1, "successful": 1, "failed": 0}, **changes}


def publisher(handler):
    def transport(request):
        if request.url.path == "/":
            return httpx.Response(200, json={"cluster_uuid": CLUSTER_UUID})
        if request.url.path.endswith("/_settings"):
            return httpx.Response(200, json={INDEX: {"settings": {"index.uuid": INDEX_UUID}}})
        if request.url.path.endswith("/_mapping"):
            return httpx.Response(200, json={INDEX: {"mappings": index_definition()["mappings"]}})
        return handler(request)
    return ElasticProjection(httpx.Client(transport=httpx.MockTransport(transport)),
                             base_url="https://synthetic-search.invalid:18770", alias=ALIAS,
                             expected_cluster_uuid=CLUSTER_UUID, expected_index_uuid=INDEX_UUID)


def test_projection_deterministic_with_input_reordering_and_does_not_mutate():
    original = entity([item("identity", [field("name", "Pessoa Sintética"), field("sex", "X")]),
                       item("email", [field("email", "synthetic@example.invalid")])])
    before = deepcopy(original)
    first = build_projection(original)
    reordered = deepcopy(original)
    reordered["items"].reverse()
    for value in reordered["items"]:
        value["fields"].reverse()
    assert build_projection(reordered) == first
    assert original == before
    assert first["projection_version"] == PROJECTION_VERSION
    assert len(first["projection_hash"]) == 64


def test_null_false_zero_and_decimal_never_round_or_conflate():
    values = [None, False, 0, Decimal("12345678901234567890.1234567890123456789")]
    entries = [item("address", [field("number", value)]) for value in values]
    with localcontext() as context:
        context.prec = 2
        document = build_projection(entity(entries))
    rows = {row["value_type"]: row for row in flat_fields(document)}
    assert rows["null"].get("value_boolean") is None
    assert rows["boolean"]["value_boolean"] is False
    assert rows["integer"]["number_exact"] == "0e0"
    assert rows["decimal"]["number_exact"] == "123456789012345678901234567890123456789e-19"
    assert "123456789012345678901234567890123456789e-19" in json.dumps(document)
    assert "items.fields.value_boolean" in json.dumps(build_query({"kind": "address", "field": "number", "value": False}))
    assert "items.fields.number_exact" in json.dumps(build_query({"kind": "address", "field": "number", "value": 0}))
    assert build_query({"kind": "address", "field": "number", "value": 1}) == build_query({"kind": "address", "field": "number", "value": Decimal("1.00000")})


def test_unknowns_unsafe_text_and_large_values_are_counted_not_mutated_or_dynamic():
    source = "synthetic\x00source\ud800"
    original = entity([item("identity", [field("name", "A\x00B"), field("sex", "\ud800"),
                                         field("nationality", "x" * 8193), field("unmapped.key", Decimal("1.23"))]),
                       item("custom", [field("unbounded", {"new_key": "data"})]),
                       item("email", [field("email", "test@example.invalid", source_id=source)])])
    document = build_projection(original)
    assert document["omitted"]["unsafe_text"] == 2
    assert document["omitted"]["long_text"] == 1
    assert document["omitted"]["unknown_items"] == 0
    assert document["omitted"]["unknown_fields"] == 2
    assert len(flat_fields(document)) == 1
    metadata = json.loads(flat_fields(document)[0]["metadata_json"])
    assert metadata["source_id"] == source
    assert original["items"][0]["fields"][0]["value"] == "A\x00B"
    assert "new_key" not in json.dumps(index_definition())
    assert "unmapped.key" not in json.dumps(document)


def test_flags_keep_tristate_apply_only_to_matching_field_and_never_confirm_pending():
    data = entity([
        item("phone", [field("number", "+5511999990000"), field("extension", "123")],
             [flag("number", "is_whatsapp", False), flag("number", "valid", True)]),
        item("phone", [field("number", "+5511999990001", status="pending")],
             [flag("number", "is_whatsapp", True), flag("number", "valid", True)]),
        item("phone", [field("number", "+5511999990002")],
             [flag("number", "is_whatsapp", True, applicable=False)]),
    ])
    rows = {row.get("value_exact"): row for row in flat_fields(build_projection(data))}
    confirmed = rows["+5511999990000"]
    assert confirmed["confirmation_recorded"] is True
    assert confirmed["flags"]["is_whatsapp"]["value"] is False
    assert confirmed["flags"]["is_whatsapp"]["state"] == "false"
    assert rows["123"]["flags"]["is_whatsapp"]["value"] is None
    for value in ("+5511999990001", "+5511999990002"):
        assert rows[value]["flags"]["is_whatsapp"]["value"] is None
        assert rows[value]["confirmation_recorded"] is False


def test_whitelisted_attributes_and_relationship_document_get_parent_provenance():
    phone = field("number", "+5511999990000", metadata={"item_attributes": {"type": "mobile", "new_dynamic_key": "omit"}})
    relation = field("target_document", {"type": "CPF", "number": "synthetic", "country": "TEST"}, status="pending")
    document = build_projection(entity([item("phone", [phone], [flag("number", "valid", True)]), item("relationship", [relation])]))
    attrs = next(row for row in flat_fields(document) if row["key"] == "type")
    assert attrs["observation_id"] == phone["observation_id"]
    assert json.loads(attrs["metadata_json"])["derived_from"] == "number"
    assert attrs["flags"]["valid"]["value"] is None and attrs["confirmation_recorded"] is False
    related = [row for row in flat_fields(document) if row["key"].startswith("target_document.")]
    assert len(related) == 3 and not any(row["resolved"] or row["confirmation_recorded"] for row in related)
    assert "new_dynamic_key" not in json.dumps(document)


def test_mapping_is_explicit_nested_and_all_projection_keys_are_declared():
    mapping = index_definition()["mappings"]
    document = build_projection(entity([item("username", [field("username", "synthetic"), field("platform", "instagram")])]))
    def validate(value, schema):
        if isinstance(value, list):
            for member in value:
                validate(member, schema)
        elif isinstance(value, dict):
            assert schema.get("dynamic") == "strict"
            assert set(value) <= set(schema["properties"])
            for key, member in value.items():
                if isinstance(member, (dict, list)):
                    validate(member, schema["properties"][key])
    validate(document, mapping)
    assert mapping["properties"]["items"]["type"] == "nested"
    assert mapping["properties"]["items"]["properties"]["fields"]["type"] == "nested"


def test_same_item_query_does_not_flatten_cross_address_or_cross_phone_flags():
    criteria = {"same_item": {"kind": "address", "conditions": [
        {"field": "city", "value": "Cidade A"}, {"field": "street", "value": "Rua B"}]}}
    query = build_query(criteria)["query"]
    assert query["nested"]["path"] == "items"
    conditions = query["nested"]["query"]["bool"]["filter"]
    assert conditions[0] == {"term": {"items.kind": "address"}}
    assert [entry["nested"]["path"] for entry in conditions[1:]] == ["items.fields", "items.fields"]
    phone = build_query({"kind": "phone", "field": "number", "value": "+5511999990000", "flags": {"is_whatsapp": True}})
    inside_field = phone["query"]["nested"]["query"]["bool"]["filter"][1]["nested"]["query"]
    text = json.dumps(inside_field)
    assert "items.fields.value_exact" in text and "items.fields.flags.is_whatsapp.state" in text
    assert "items.fields.resolved" in text


def test_expired_confirmation_is_unknown_at_query_time_without_nondeterministic_document():
    original = entity([item("phone", [field("number", "+5511999990000")],
                            [flag("number", "is_whatsapp", True, metadata={"expires_at": "2026-09-08T02:00:00Z"})])])
    document = build_projection(original)
    row = flat_fields(document)[0]
    assert row["flags"]["is_whatsapp"]["expires_at"] == "2026-09-08T02:00:00Z"
    assert build_projection(original) == document
    query = build_query({"kind": "phone", "field": "number", "value": "+5511999990000", "flags": {"is_whatsapp": True}})
    assert '"gt": "now"' in json.dumps(query)
    unknown = build_query({"kind": "phone", "field": "number", "value": "+5511999990000", "flags": {"is_whatsapp": None}})
    assert '"terms": {"items.fields.flags.is_whatsapp.state": ["true", "false"]}' in json.dumps(unknown)


def test_partial_filters_escape_wildcards_and_fold_accents():
    query = build_query({"kind": "identity", "field": "name", "op": "contains", "value": "Ána*?"})
    clauses = query["query"]["nested"]["query"]["bool"]["filter"][1]["nested"]["query"]["bool"]["filter"]
    assert clauses[-1] == {"wildcard": {"items.fields.value_contains": {"value": "*ana\\*\\?*"}}}
    group = build_query({"any": [{"kind": "email", "field": "email", "op": "prefix", "value": "TEST"},
                                 {"kind": "username", "field": "username", "op": "match", "value": "synthetic"}]}, entity_type="person")
    assert group["_source"] is False
    assert group["fields"] == ["id", "record_version"]


@pytest.mark.parametrize("criteria", [
    {"kind": "custom", "field": "new_key", "value": "x"},
    {"kind": "identity", "field": "name", "value": "x", "script": "arbitrary"},
    {"kind": "identity", "field": "name", "op": "contains", "value": ""},
    {"kind": "phone", "field": "number", "value": "x", "flags": {"valid": 1}},
    {"kind": "identity", "field": "name", "op": "range", "value": {"gte": "a"}},
    {"kind": "identity", "field": "birth_date", "op": "range", "value": {"gte": True}},
    {"all": []}, {"same_item": {"kind": "phone", "conditions": []}},
])
def test_query_rejects_unmapped_injection_invalid_ranges_and_boolean_coercion(criteria):
    with pytest.raises(ProjectionError):
        build_query(criteria)


def test_date_range_and_explicit_pending_invalid_selection():
    query = build_query({"kind": "identity", "field": "birth_date", "op": "range", "value": {"gte": "2000-01-01"}},
                        include_pending=True, include_invalid=True)
    text = json.dumps(query)
    assert "value_date" in text and "resolved" not in text and "flags.valid" not in text


def test_publish_sends_version_current_document_require_alias_and_verifies_ack():
    document = build_projection(entity(version=3))
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=successful(document))
    transport = publisher(handle)
    assert transport.publish(document) == "indexed"
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "PUT" and request.url.path == f"/{ALIAS}/_doc/{OWNER}"
    assert dict(request.url.params) == {"version": "3", "version_type": "external_gte", "require_alias": "true", "wait_for_active_shards": "all"}
    assert json.loads(request.content) == document


@pytest.mark.parametrize("changes", [{"_version": 2}, {"_version": True}, {"_id": "other"}, {"_index": "other"},
                                     {"_shards": {"total": 2, "successful": 1, "failed": 0}},
                                     {"_shards": {"successful": 1, "failed": 1}}, {"result": "noop"}])
def test_bad_publication_ack_is_not_success(changes):
    document = build_projection(entity())
    with pytest.raises(ProjectionError, match="SEARCH_ACK_UNVERIFIED"):
        publisher(lambda _: httpx.Response(200, json=successful(document, **changes))).publish(document)


def test_stale_event_conflict_requires_newer_document_proof():
    document = build_projection(entity(version=2))
    newer = build_projection(entity(version=3))
    def handle(request):
        if request.method == "PUT":
            return httpx.Response(409, json={"error": "not trusted"})
        return httpx.Response(200, json={"found": True, "_id": OWNER, "_index": INDEX, "_version": 3, "_source": newer})
    assert publisher(handle).publish(document) == "obsolete"
    with pytest.raises(ProjectionError, match="SEARCH_CONFLICT_UNVERIFIED"):
        publisher(lambda request: httpx.Response(409 if request.method == "PUT" else 404, json={})).publish(document)


def test_equal_version_different_content_is_not_acknowledged_on_conflict():
    document = build_projection(entity(version=2))
    changed = build_projection(entity([item("identity", [field("name", "Different synthetic")])], version=2))
    def handle(request):
        if request.method == "PUT":
            return httpx.Response(409, json={})
        return httpx.Response(200, json={"found": True, "_id": OWNER, "_index": INDEX, "_version": 2, "_source": changed})
    with pytest.raises(ProjectionError, match="SEARCH_VERSION_CONTENT_CONFLICT"):
        publisher(handle).publish(document)


@pytest.mark.parametrize("response", [httpx.Response(429, json={"private": "do not echo"}),
                                      httpx.Response(500, text="secret body"), httpx.Response(200, text="not json")])
def test_error_response_bodies_never_echo(response):
    with pytest.raises(ProjectionError) as exc:
        publisher(lambda _: response).publish(build_projection(entity()))
    assert str(exc.value) in {"SEARCH_INDEX_REJECTED", "SEARCH_INVALID_RESPONSE"}


def test_mapping_verification_never_creates_index_and_port_9200_is_blocked():
    requests = []
    def handle(request):
        requests.append(request)
        if request.url.path == "/":
            return httpx.Response(200, json={"cluster_uuid": CLUSTER_UUID})
        if request.url.path.endswith("/_settings"):
            return httpx.Response(200, json={INDEX: {"settings": {"index.uuid": INDEX_UUID}}})
        return httpx.Response(200, json={INDEX: {"mappings": {"_meta": {"projection_version": "wrong"}}}})
    client = httpx.Client(transport=httpx.MockTransport(handle))
    instance = ElasticProjection(client, base_url="https://synthetic.invalid:18770", alias=ALIAS,
                                 expected_cluster_uuid=CLUSTER_UUID, expected_index_uuid=INDEX_UUID)
    assert not requests
    with pytest.raises(ProjectionError, match="SEARCH_MAPPING_VERSION_MISMATCH"):
        instance.publish(build_projection(entity()))
    assert len(requests) == 3 and all(request.method == "GET" for request in requests)
    with pytest.raises(ProjectionError, match="LEGACY_SEARCH_PORT_FORBIDDEN"):
        ElasticProjection(client, base_url="http://localhost:9200", alias=ALIAS,
                          expected_cluster_uuid=CLUSTER_UUID, expected_index_uuid=INDEX_UUID)


class FakeStore:
    def __init__(self, versions, current, *, ack=True):
        self.events = [{"event_id": str(uuid4()), "owner_id": OWNER, "entity_version": version, "lease_token": str(uuid4())} for version in versions]
        self.current, self.ack, self.acknowledged, self.leased = current, ack, [], []

    def claim_outbox(self, limit, lease_seconds):
        self.leased = [event for event in self.events if event["event_id"] not in self.acknowledged][:limit]
        return deepcopy(self.leased)

    def get_entity(self, owner):
        assert owner == OWNER
        return deepcopy(self.current)

    def acknowledge_outbox(self, event_id, token):
        event = next(event for event in self.leased if event["event_id"] == event_id)
        assert token == event["lease_token"]
        if self.ack:
            self.acknowledged.append(event_id)
        return self.ack


def test_out_of_order_events_load_current_version_and_replay_without_data_loss():
    store = FakeStore([2, 1], entity(version=2))
    requests = []
    def handle(request):
        document = json.loads(request.content)
        requests.append(document)
        return httpx.Response(200, json=successful(document))
    worker = OutboxConsumer(store, publisher(handle))
    report = worker.run_once()
    assert report["acknowledged"] == 2 and report["failed"] == 0
    assert [row["record_version"] for row in requests] == [2, 2]
    assert requests[0] == requests[1]
    assert worker.run_once()["claimed"] == 0


def test_index_failure_leaves_event_for_retry_and_never_calls_ack():
    store = FakeStore([1], entity())
    state = {"fail": True}
    def handle(request):
        if state["fail"]:
            raise httpx.ReadTimeout("private URL or token", request=request)
        return httpx.Response(200, json=successful(json.loads(request.content)))
    worker = OutboxConsumer(store, publisher(handle))
    first = worker.run_once()
    assert first["failed"] == 1 and not store.acknowledged
    assert first["results"][0]["code"] == "SEARCH_TRANSPORT_ERROR"
    state["fail"] = False
    assert worker.run_once()["acknowledged"] == 1


def test_lease_loss_after_indexing_is_retried_idempotently():
    store = FakeStore([1], entity(), ack=False)
    requests = []
    def handle(request):
        document = json.loads(request.content)
        requests.append(document)
        return httpx.Response(200, json=successful(document))
    worker = OutboxConsumer(store, publisher(handle))
    first = worker.run_once()
    assert first["indexed"] == first["lease_lost"] == 1 and first["acknowledged"] == 0
    store.ack = True
    assert worker.run_once()["acknowledged"] == 1
    assert requests[0] == requests[1]


def test_source_behind_event_or_changed_owner_never_publishes():
    store = FakeStore([2], entity(version=1))
    requests = []
    worker = OutboxConsumer(store, publisher(lambda request: requests.append(request)))
    report = worker.run_once()
    assert report["results"][0]["code"] == "SOURCE_VERSION_BEHIND_EVENT"
    assert not requests and not store.acknowledged
    store.current["id"] = str(uuid4())
    assert worker.run_once()["results"][0]["code"] == "SOURCE_IDENTITY_MISMATCH"


def test_projection_limit_does_not_partially_publish_or_ack(monkeypatch):
    import bigbase.canonical_search as module
    monkeypatch.setattr(module, "MAX_NESTED_OBJECTS", 1)
    store = FakeStore([1], entity([item("identity", [field("name", "synthetic")])]))
    requests = []
    report = OutboxConsumer(store, publisher(lambda request: requests.append(request))).run_once()
    assert report["results"][0]["code"] == "PROJECTION_NESTED_LIMIT"
    assert not requests and not store.acknowledged


def test_interleaved_workers_cannot_overwrite_newer_index_and_old_event_can_ack():
    old = build_projection(entity(version=1))
    newer = build_projection(entity(version=2))
    indexed = {"document": None}
    def handle(request):
        if request.method == "GET":
            current = indexed["document"]
            return httpx.Response(200, json={"found": True, "_id": OWNER, "_index": INDEX,
                                            "_version": current["record_version"], "_source": current})
        candidate = json.loads(request.content)
        current = indexed["document"]
        if current and candidate["record_version"] < current["record_version"]:
            return httpx.Response(409, json={})
        indexed["document"] = candidate
        return httpx.Response(200, json=successful(candidate))
    first_worker, second_worker = publisher(handle), publisher(handle)
    assert second_worker.publish(newer) == "indexed"
    assert first_worker.publish(old) == "obsolete"
    assert indexed["document"] == newer


def test_corrupt_newer_source_or_index_redirect_does_not_count_as_publication_proof():
    old = build_projection(entity(version=1))
    corrupt = build_projection(entity(version=2))
    corrupt["items"] = ["changed after its hash"]
    def handle(request):
        if request.method == "PUT":
            return httpx.Response(409, json={})
        return httpx.Response(200, json={"found": True, "_id": OWNER, "_index": INDEX, "_version": 2, "_source": corrupt})
    with pytest.raises(ProjectionError, match="SEARCH_CONFLICT_UNVERIFIED"):
        publisher(handle).publish(old)
    with pytest.raises(ProjectionError, match="SEARCH_INDEX_REJECTED"):
        publisher(lambda _: httpx.Response(307, headers={"Location": "http://localhost:9200/pessoas"})).publish(old)


def test_ack_exception_retries_without_losing_event_and_output_has_no_source_values():
    store = FakeStore([1], entity([item("identity", [field("name", "Never log synthetic source value")])]))
    saved_ack = store.acknowledge_outbox
    def failed_ack(*args):
        raise RuntimeError("Never log database credentials")
    store.acknowledge_outbox = failed_ack
    worker = OutboxConsumer(store, publisher(lambda request: httpx.Response(200, json=successful(json.loads(request.content)))))
    first = worker.run_once()
    assert first["failed"] == first["indexed"] == 1 and not store.acknowledged
    assert first["results"][0]["code"] == "OUTBOX_PROCESSING_ERROR"
    assert "Never log" not in json.dumps(first)
    store.acknowledge_outbox = saved_ack
    assert worker.run_once()["acknowledged"] == 1


@pytest.mark.parametrize("version", [True, 0, -1, 2**63, "1"])
def test_external_version_bounds_reject_boolean_and_invalid_integer(version):
    with pytest.raises(ProjectionError, match="INVALID_RECORD_VERSION"):
        build_projection(entity(version=version))


def test_query_limits_and_unknown_null_are_explicit():
    with pytest.raises(ProjectionError):
        build_query({"all": [{"kind": "identity", "field": "name", "value": "x"}] * 101})
    with pytest.raises(ProjectionError, match="INVALID_PROJECTION_DATE"):
        build_query({"kind": "identity", "field": "birth_date", "op": "range", "value": {"gte": None}})
    query = build_query({"kind": "address", "field": "number", "value": None})
    assert '"items.fields.value_type": "null"' in json.dumps(query)


@pytest.mark.parametrize("url", ["http://remote.invalid", "https://user:secret@remote.invalid", "https://remote.invalid?token=secret"])
def test_remote_transport_and_credentials_cannot_bypass_destination_guard(url):
    with pytest.raises(ProjectionError):
        ElasticProjection(httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("No I/O allowed"))),
                          base_url=url, alias=ALIAS, expected_cluster_uuid=CLUSTER_UUID, expected_index_uuid=INDEX_UUID)


@pytest.mark.parametrize("changed", ["cluster", "index"])
def test_destination_uuid_change_is_rechecked_before_each_publication(changed):
    state = {"changed": False, "writes": 0}
    def handle(request):
        assert request.url.host == "approved.invalid"
        if request.url.path == "/":
            return httpx.Response(200, json={"cluster_uuid": "other" if state["changed"] and changed == "cluster" else CLUSTER_UUID})
        if request.url.path.endswith("/_settings"):
            return httpx.Response(200, json={INDEX: {"settings": {"index.uuid": "other" if state["changed"] and changed == "index" else INDEX_UUID}}})
        if request.url.path.endswith("/_mapping"):
            return httpx.Response(200, json={INDEX: {"mappings": index_definition()["mappings"]}})
        state["writes"] += 1
        return httpx.Response(200, json=successful(json.loads(request.content)))
    client = httpx.Client(base_url="http://localhost:9200", transport=httpx.MockTransport(handle))
    instance = ElasticProjection(client, base_url="https://approved.invalid", alias=ALIAS,
                                 expected_cluster_uuid=CLUSTER_UUID, expected_index_uuid=INDEX_UUID)
    assert instance.publish(build_projection(entity())) == "indexed"
    state["changed"] = True
    with pytest.raises(ProjectionError, match="SEARCH_.*_IDENTITY_CHANGED"):
        instance.publish(build_projection(entity(version=2)))
    assert state["writes"] == 1


def test_conflict_response_is_streamed_bounded_closed_and_left_unacknowledged():
    class Oversized(httpx.SyncByteStream):
        def __init__(self):
            self.chunks, self.closed = 0, False
        def __iter__(self):
            for _ in range(10000):
                self.chunks += 1
                yield b"x" * 8192
        def close(self):
            self.closed = True
    source = Oversized()
    def handle(request):
        assert request.headers["Accept-Encoding"] == "identity"
        if request.method == "PUT":
            return httpx.Response(409, json={})
        return httpx.Response(200, stream=source)
    instance = publisher(handle)
    instance.max_response_bytes = 65536
    store = FakeStore([1], entity())
    report = OutboxConsumer(store, instance).run_once()
    assert report["results"][0]["code"] == "SEARCH_RESPONSE_TOO_LARGE"
    assert source.closed and source.chunks <= 16
    assert not store.acknowledged


def test_compressed_response_is_not_expanded_before_the_limit_guard():
    class NeverRead(httpx.SyncByteStream):
        def __iter__(self):
            pytest.fail("A compressed body must not be read/expanded")
            yield b""
    instance = publisher(lambda request: httpx.Response(200, stream=NeverRead(), headers={"Content-Encoding": "gzip"}))
    with pytest.raises(ProjectionError, match="SEARCH_UNSUPPORTED_RESPONSE_ENCODING"):
        instance.publish(build_projection(entity()))


def test_unpinned_identities_and_response_limits_fail_before_network():
    client = httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("No network")))
    for kwargs in ({"expected_cluster_uuid": ""}, {"expected_index_uuid": None}, {"max_response_bytes": True},
                   {"max_response_bytes": 16 * 1024 * 1024 + 1}):
        args = {"expected_cluster_uuid": CLUSTER_UUID, "expected_index_uuid": INDEX_UUID, **kwargs}
        with pytest.raises(ProjectionError):
            ElasticProjection(client, base_url="https://approved.invalid", alias=ALIAS, **args)


@pytest.mark.parametrize("value", ["a", "áé", "  "])
def test_contains_requires_three_non_padding_characters_and_dedicated_index(value):
    with pytest.raises(ProjectionError):
        build_query({"kind": "identity", "field": "name", "op": "contains", "value": value})
    mapping = index_definition()["mappings"]["properties"]["items"]["properties"]["fields"]["properties"]
    assert mapping["value_contains"]["type"] == "wildcard"


def test_one_character_prefix_requires_selectivity_in_the_same_boolean_branch():
    prefix = {"kind": "identity", "field": "name", "op": "prefix", "value": "A"}
    exact = {"kind": "document", "field": "number", "value": "synthetic-exact"}
    with pytest.raises(ProjectionError, match="SHORT_PREFIX_REQUIRES_SELECTIVE_FILTER"):
        build_query(prefix)
    assert build_query({"all": [prefix, exact]})
    with pytest.raises(ProjectionError, match="SHORT_PREFIX_REQUIRES_SELECTIVE_FILTER"):
        build_query({"any": [prefix, exact]})
    with pytest.raises(ProjectionError, match="SHORT_PREFIX_REQUIRES_SELECTIVE_FILTER"):
        build_query({"all": [prefix, {"any": [exact, {"kind": "identity", "field": "sex", "value": "X"}]}]})
    assert build_query({"all": [prefix, {"kind": "identity", "field": "birth_date", "op": "range", "value": {"gte": "2000-01-01", "lt": "2010-01-01"}}]})


def test_invalid_parent_cannot_qualify_via_derived_attribute_without_inheriting_flag():
    value = field("number", "+5511999990000", metadata={"item_attributes": {"country": "BR"}})
    rows = flat_fields(build_projection(entity([item("phone", [value], [flag("number", "valid", False)])])))
    country = next(row for row in rows if row["key"] == "country")
    assert country["flags"]["valid"]["value"] is None
    assert country["parent_valid"]["value"] is False
    query = build_query({"kind": "phone", "field": "country", "value": "BR"})
    assert "items.fields.parent_valid.state" in json.dumps(query)
    with_invalid = build_query({"kind": "phone", "field": "country", "value": "BR"}, include_invalid=True)
    assert "parent_valid" not in json.dumps(with_invalid)

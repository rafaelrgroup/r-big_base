"""PIT protocol simulation, encrypted cursors and authorization; no real ES/PG."""
import base64
from copy import deepcopy
import json

import httpx
import pytest

from bigbase.canonical_search import index_definition
from bigbase.canonical_search_reader import (
    CanonicalSearchReader, InvalidSearchCursor, SearchAuthorizationError, SearchReadError,
)


KEY = base64.urlsafe_b64encode(b"S" * 32)  # Synthetic fixture only.
INDEX = "bigbase-search-fixture-000001"
ALIAS = "bigbase-search-fixture-write"
AUTH = {"active": True, "can_search": True, "revision": "permissions-1", "scopes": ["search"], "source_revision": "sources-1"}
CRITERIA = {"kind": "identity", "field": "name", "value": "Pessoa Sintética"}


def owner(number):
    return f"00000000-0000-4000-8000-{number:012x}"


class ElasticFake:
    def __init__(self, count=5):
        self.docs = [{"id": owner(i), "record_version": 1} for i in range(1, count+1)]
        self.requests, self.pits, self.counter = [], {}, 0
        self.search_fault = None
        self.open_fault = None
        self.close_fault = False

    def transport(self, request):
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, body))
        if request.url.path == "/":
            return httpx.Response(200, json={"cluster_uuid": "synthetic-cluster"})
        if request.url.path.endswith("/_settings"):
            return httpx.Response(200, json={INDEX: {"settings": {"index.uuid": "synthetic-index"}}})
        if request.url.path.endswith("/_mapping"):
            return httpx.Response(200, json={INDEX: {"mappings": index_definition()["mappings"]}})
        if request.url.path == f"/{ALIAS}/_pit":
            self.counter += 1
            pit = "synthetic-pit-" + str(self.counter)
            self.pits[pit] = {"docs": deepcopy(self.docs), "aliases": [pit]}
            response = {"id": pit, "_shards": {"total": 1, "successful": 1, "failed": 0}}
            if self.open_fault:
                self.open_fault(response)
            return httpx.Response(200, json=response)
        if request.url.path == "/_pit":
            assert request.method == "DELETE"
            if self.close_fault:
                return httpx.Response(503, json={"error": "do not log"})
            group = self.pits.get(body["id"])
            if group:
                for alias in group["aliases"]:
                    self.pits.pop(alias, None)
            return httpx.Response(200, json={"succeeded": True})
        assert request.url.path == "/_search" and request.method == "POST"
        assert body["_source"] is False and body["fields"] == ["id", "record_version"]
        assert body["sort"] == [{"id": "asc"}, {"_shard_doc": "asc"}]
        group = self.pits.get(body["pit"]["id"])
        if group is None:
            return httpx.Response(404, json={"error": "sensitive PIT failure"})
        documents = group["docs"]
        selected = [(index, doc) for index, doc in enumerate(documents) if "search_after" not in body or doc["id"] > body["search_after"][0]][:body["size"]]
        hits = [{"_index": INDEX, "_id": doc["id"], "sort": [doc["id"], index],
                 "fields": {"id": [doc["id"]], "record_version": [doc["record_version"]]}} for index, doc in selected]
        self.counter += 1
        latest = "synthetic-pit-" + str(self.counter)
        group["aliases"].append(latest)
        self.pits[latest] = group
        response = {"timed_out": False, "_shards": {"total": 1, "successful": 1, "failed": 0},
                    "hits": {"total": {"value": len(documents), "relation": "eq"}, "hits": hits}, "pit_id": latest}
        if self.search_fault:
            outcome = self.search_fault(body, response)
            if isinstance(outcome, httpx.Response):
                return outcome
        return httpx.Response(200, json=response)


def reader(fake=None, clock=None, key=KEY, **kwargs):
    fake = fake or ElasticFake()
    clock = clock or [1800000000]
    result = CanonicalSearchReader(httpx.Client(transport=httpx.MockTransport(fake.transport)),
        base_url="https://synthetic.invalid", alias=ALIAS, expected_cluster_uuid="synthetic-cluster",
        expected_index_uuid="synthetic-index", cursor_key=key, clock=lambda: clock[0], **kwargs)
    return result, fake, clock


def search(instance, **kwargs):
    values = {"principal_id": "synthetic-user", "authorization": deepcopy(AUTH), "page_size": 2, **kwargs}
    return instance.search(values.pop("criteria", deepcopy(CRITERIA)), **values)


def test_pages_are_complete_in_order_snapshot_stable_and_release_explicit():
    instance, fake, _ = reader()
    first = search(instance)
    fake.docs.insert(0, {"id": owner(0), "record_version": 7})
    fake.docs[1]["record_version"] = 99
    second = search(instance, cursor=first["next_cursor"])
    third = search(instance, cursor=second["next_cursor"])
    assert [row["id"] for page in (first, second, third) for row in page["items"]] == [owner(i) for i in range(1, 6)]
    assert all(row["record_version"] == 1 for page in (first, second, third) for row in page["items"])
    assert third["returned"] == 1 and third["seen"] == 5 and third["total"] == {"value": 5, "relation": "eq"}
    assert third["has_more"] is False and third["next_cursor"] is None
    assert not any(method == "DELETE" for method, _, _ in fake.requests)
    repeated_last = search(instance, cursor=second["next_cursor"])
    assert repeated_last["items"] == third["items"]
    assert instance.close(third["release_cursor"], principal_id="synthetic-user", authorization=AUTH) == {"released": True}
    assert not fake.pits


def test_cursor_restarts_in_new_process_with_same_persistent_key_and_latest_pit():
    first_instance, fake, clock = reader()
    first = search(first_instance)
    second_instance, _, _ = reader(fake, clock)
    second = search(second_instance, cursor=first["next_cursor"])
    opens = [row for row in fake.requests if row[1] == f"/{ALIAS}/_pit"]
    searches = [body for _, path, body in fake.requests if path == "/_search"]
    assert len(opens) == 1
    assert searches[1]["pit"]["id"] == "synthetic-pit-2"
    assert searches[1]["search_after"] == [owner(2), 1]
    assert second["items"][0]["id"] == owner(3)


def test_cursor_is_authenticated_encrypted_and_contains_no_visible_query_or_identity():
    instance, fake, _ = reader()
    first = search(instance)
    token = first["next_cursor"]
    ciphertext = base64.urlsafe_b64decode(token[4:])
    for text in ("Pessoa Sintética", "synthetic-user", "synthetic-pit", owner(2), "permissions-1"):
        assert text not in token and text.encode() not in ciphertext
    before = len(fake.requests)
    with pytest.raises(InvalidSearchCursor):
        search(instance, cursor=token[:-3] + "AAA")
    assert len(fake.requests) == before


@pytest.mark.parametrize("change", [
    {"principal_id": "other-user"}, {"authorization": {**AUTH, "revision": "permissions-2"}},
    {"authorization": {**AUTH, "scopes": ["search", "different"]}},
    {"authorization": {**AUTH, "source_revision": "sources-2"}},
    {"criteria": {**CRITERIA, "value": "Other synthetic"}}, {"page_size": 3},
    {"include_pending": True}, {"include_invalid": True}, {"entity_type": "company"},
])
def test_cursor_binds_principal_permissions_sources_query_options_and_page_size(change):
    instance, fake, _ = reader()
    first = search(instance)
    before = len(fake.requests)
    with pytest.raises(InvalidSearchCursor):
        search(instance, cursor=first["next_cursor"], **change)
    assert len(fake.requests) == before


@pytest.mark.parametrize("authorization", [{**AUTH, "active": False}, {**AUTH, "can_search": False},
                                            {**AUTH, "active": 1}, {**AUTH, "revision": ""}, {**AUTH, "scopes": []}])
def test_revoked_or_incomplete_authorization_is_refused_before_any_network(authorization):
    instance, fake, _ = reader()
    with pytest.raises(SearchAuthorizationError):
        search(instance, authorization=authorization)
    assert not fake.requests


def test_idle_and_absolute_ttl_are_enforced_even_with_renewal():
    instance, fake, clock = reader(ElasticFake(count=20))
    first = search(instance)
    clock[0] += 900
    before = len(fake.requests)
    with pytest.raises(InvalidSearchCursor):
        search(instance, cursor=first["next_cursor"])
    assert len(fake.requests) == before
    instance, fake, clock = reader(ElasticFake(count=20))
    page = search(instance)
    started = clock[0]
    for _ in range(4):
        clock[0] += 800
        page = search(instance, cursor=page["next_cursor"])
    assert page["snapshot"]["deadline_at"] == first["snapshot"]["deadline_at"]
    clock[0] = started + 3600
    with pytest.raises(InvalidSearchCursor):
        search(instance, cursor=page["next_cursor"])


def test_wrong_key_and_release_token_cannot_resume_search():
    instance, fake, clock = reader()
    page = search(instance)
    other, _, _ = reader(fake, clock, key=base64.urlsafe_b64encode(b"x" * 32))
    before = len(fake.requests)
    with pytest.raises(InvalidSearchCursor):
        search(other, cursor=page["next_cursor"])
    with pytest.raises(InvalidSearchCursor):
        search(instance, cursor=page["release_cursor"])
    assert len(fake.requests) == before


def test_pit_expiration_never_opens_new_pit_or_silently_restarts():
    instance, fake, _ = reader()
    first = search(instance)
    fake.pits.clear()
    with pytest.raises(InvalidSearchCursor, match="SEARCH_PIT_EXPIRED_RESTART_REQUIRED"):
        search(instance, cursor=first["next_cursor"])
    assert len([row for row in fake.requests if row[1] == f"/{ALIAS}/_pit"]) == 1


@pytest.mark.parametrize("fault", [
    lambda response: response.update(timed_out=True),
    lambda response: response.update(terminated_early=True),
    lambda response: response.update(_shards={"total": 2, "successful": 1, "failed": 0}),
    lambda response: response["hits"].update(hits=[response["hits"]["hits"][0]] * 2),
    lambda response: response["hits"]["hits"].reverse(),
    lambda response: response["hits"]["hits"][0].update(_index="wrong"),
    lambda response: response["hits"]["hits"][0]["fields"].update(record_version=[True]),
    lambda response: response["hits"].update(total={"value": 5, "relation": "wrong"}),
    lambda response: response["hits"].update(hits=[]),
    lambda response: response["hits"]["hits"][0].update(sort=[owner(1), True]),
])
def test_partial_malformed_or_unordered_page_never_returns_a_continuation(fault):
    instance, fake, _ = reader()
    fake.search_fault = lambda body, response: fault(response)
    with pytest.raises(SearchReadError):
        search(instance)
    assert not fake.pits


def test_failure_mid_search_preserves_previous_cursor_and_pit_for_retry():
    instance, fake, _ = reader()
    first = search(instance)
    fake.search_fault = lambda body, response: httpx.Response(503, json={"secret": "never echo this"})
    with pytest.raises(SearchReadError, match="SEARCH_READ_HTTP_ERROR"):
        search(instance, cursor=first["next_cursor"])
    assert fake.pits and not any(method == "DELETE" for method, _, _ in fake.requests)
    fake.search_fault = None
    assert search(instance, cursor=first["next_cursor"])["items"][0]["id"] == owner(3)


def test_flag_expiry_uses_same_reference_time_across_pages():
    instance, fake, clock = reader()
    criteria = {"kind": "phone", "field": "number", "value": "+5511999990000", "flags": {"is_whatsapp": True}}
    first = search(instance, criteria=criteria)
    clock[0] += 100
    search(instance, criteria=criteria, cursor=first["next_cursor"])
    queries = [body["query"] for _, path, body in fake.requests if path == "/_search"]
    assert queries[0] == queries[1]
    assert '"gt": "now"' not in json.dumps(queries[0])
    assert first["snapshot"]["started_at"] in json.dumps(queries[0])


def test_empty_search_total_and_release_failure_are_explicit():
    instance, fake, _ = reader(ElasticFake(count=0))
    page = search(instance)
    assert page["items"] == [] and page["seen"] == 0 and page["has_more"] is False
    fake.close_fault = True
    assert instance.close(page["release_cursor"], principal_id="synthetic-user", authorization=AUTH) == {"released": False}
    with pytest.raises(InvalidSearchCursor):
        instance.close(page["release_cursor"], principal_id="other", authorization=AUTH)


@pytest.mark.parametrize("kwargs", [{"page_size": True}, {"page_size": 101}, {"page_size": 0},
                                    {"sort": [{"field": "name", "direction": "asc"}]},
                                    {"sort": [{"field": "id", "direction": "desc"}]}])
def test_unsupported_sort_and_page_sizes_are_never_ignored(kwargs):
    instance, fake, _ = reader()
    with pytest.raises(SearchReadError):
        search(instance, **kwargs)
    assert not fake.requests


def test_bounded_response_failure_has_no_raw_server_content():
    instance, fake, _ = reader(max_response_bytes=16384)
    fake.search_fault = lambda body, response: httpx.Response(200, content=b"secret-source-" * 2000)
    with pytest.raises(SearchReadError, match="SEARCH_RESPONSE_TOO_LARGE") as exc:
        search(instance)
    assert "secret" not in str(exc.value)


def test_gte_total_cannot_report_exhausted_before_its_lower_bound():
    instance, fake, _ = reader(ElasticFake(count=1))
    fake.search_fault = lambda body, response: response["hits"].update(total={"value": 10000, "relation": "gte"})
    with pytest.raises(SearchReadError, match="SEARCH_COUNT_RECONCILIATION_FAILED"):
        search(instance)


def test_projection_only_reader_never_writes_documents_or_creates_indices():
    instance, fake, _ = reader()
    page = search(instance)
    instance.close(page["release_cursor"], principal_id="synthetic-user", authorization=AUTH)
    assert not any(method == "PUT" for method, _, _ in fake.requests)
    assert all(path in {"/", f"/{ALIAS}/_mapping", f"/{ALIAS}/_settings", f"/{ALIAS}/_pit", "/_search", "/_pit"} for _, path, _ in fake.requests)

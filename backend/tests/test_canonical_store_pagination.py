"""Stable canonical pages against the actual private PostgreSQL 18 fixture."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal
import hashlib
import re
from uuid import UUID, uuid4

import pytest

from bigbase import canonical_store as module
from bigbase.canonical_store import CanonicalError, CanonicalRowTooLarge, CanonicalStore, InvalidCursor, json_text
from test_canonical_store import store, job, record, atom, apply


def collect(method, owner, *, cursor=None, **kwargs):
    result, frontiers, tokens = [],set(),set()
    for _ in range(100):
        page = method(owner,cursor=cursor,**kwargs)
        result.extend(page["items"])
        frontiers.add(page["snapshot"]["entity_version"])
        if not page["has_more"]:
            assert page["next_cursor"] is None
            assert len(frontiers) == 1
            return result
        cursor = page["next_cursor"]
        assert cursor not in tokens
        tokens.add(cursor)
    pytest.fail("Pagination did not terminate")


def history_fixture(store):
    work = job(store)
    receipt = apply(store,work,[record(work["source_id"],facts=[atom("/a","A",target="a"),atom("/b",False,target="b")])])
    for i in range(3):
        apply(store,work,[record(work["source_id"],facts=[atom("/a",f"A{i}",target="a")])])
    return work,receipt["entity_ids"][0]


@pytest.mark.parametrize("order",["asc","desc"])
def test_history_cut_excludes_concurrent_late_events_and_has_no_duplicates(store,order):
    work,owner = history_fixture(store)
    expected = store.page_history(owner,limit=200,order=order)["items"]
    first = store.page_history(owner,limit=2,order=order)
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(apply,store,work,[record(work["source_id"],facts=[atom("/late","late",target="a",observed_at="1990-01-01T00:00:00Z")])]).result(timeout=15)
    remaining = collect(store.page_history,owner,limit=1,order=order,cursor=first["next_cursor"])
    received = first["items"]+remaining
    assert [row["observation_id"] for row in received] == [row["observation_id"] for row in expected]
    assert len({row["observation_id"] for row in received}) == len(expected)
    assert len(store.page_history(owner,limit=200)["items"]) == len(expected)+1


def test_cursor_is_opaque_persistent_replayable_and_binds_owner_filter_order_kind(store):
    work,owner = history_fixture(store)
    first = store.page_history(owner,field_path="a",limit=1)
    token = first["next_cursor"]
    assert re.fullmatch(r"cb1_[A-Za-z0-9_-]{43}",token)
    assert owner not in token and work["source_id"] not in token
    with store.connection() as c:
        row = c.execute("SELECT * FROM read_cursors WHERE token_hash=%s",(hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        assert row is not None and token not in repr(row)
    restarted = CanonicalStore(store.dsn,store.schema)
    page = restarted.page_history(owner,field_path="a",limit=1,cursor=token)
    replay = restarted.page_history(owner,field_path="a",limit=1,cursor=token)
    assert replay["items"] == page["items"]
    other = job(store)
    other_owner = apply(store,other,[record(other["source_id"])])["entity_ids"][0]
    wrong = [lambda:store.page_history(other_owner,field_path="a",cursor=token),
             lambda:store.page_history(owner,field_path="b",cursor=token),
             lambda:store.page_history(owner,field_path="a",source_id=work["source_id"],cursor=token),
             lambda:store.page_history(owner,field_path="a",order="desc",cursor=token),
             lambda:store.page_fields(owner,field_path="a",cursor=token),
             lambda:store.page_history(owner,field_path="a",cursor=token[:-1]+("A" if token[-1]!="A" else "B"))]
    for call in wrong:
        with pytest.raises(InvalidCursor): call()


def test_history_filters_and_complete_original_provenance(store):
    document = uuid4().hex
    one,two = job(store),job(store)
    first = record(one["source_id"],document=document,facts=[
        atom("/number","+5511999990000",target="number",kind="phone",key="contact",flags={"is_whatsapp":{"value":False,"observed_at":"2025-01-01T00:00:00Z"}}),
        atom("/arbitrary\x00",Decimal("0.123456789012345678901"),target="custom\x00",kind="custom",key="unknown",normalization={"rule":"preserved"})])
    owner = apply(store,one,[first])["entity_ids"][0]
    apply(store,two,[record(two["source_id"],document=document,facts=[atom("/number","+5511999990000",target="number",kind="phone",key="contact")])])
    phone = next(item for item in store.get_entity(owner)["items"] if item["kind"] == "phone")
    page = store.page_history(owner,item_id=phone["id"],field_path="number",source_id=one["source_id"],dimension="flag:is_whatsapp")
    assert len(page["items"]) == 1
    row = page["items"][0]
    assert row["input_json"] == "false" and row["input_value"] is False
    assert row["observed_at"] == "2025-01-01T00:00:00+00:00"
    assert row["source_id"] == one["source_id"] and row["actor_id"] == "test-actor"
    assert row["operation"]["source_record_id"] == "1"
    assert row["operation"]["adapter_version"] == "synthetic-v1"
    exact = store.page_history(owner,field_path="custom\x00")["items"][0]
    assert exact["input_value"] == Decimal("0.123456789012345678901")
    assert exact["source_path"] == "/arbitrary\x00"
    assert exact["metadata"]["normalization"] == {"rule":"preserved"}


def test_fields_snapshot_reconstructs_values_before_concurrent_update_and_new_keys(store):
    work = job(store)
    atoms = [atom(f"/{i}",f"old-{i}",target=f"field-{i}") for i in range(5)]
    owner = apply(store,work,[record(work["source_id"],facts=atoms)])["entity_ids"][0]
    expected = store.page_fields(owner,limit=200)["items"]
    first = store.page_fields(owner,limit=1)
    updated = [atom(f"/{i}",f"new-{i}",target=f"field-{i}") for i in range(5)]
    updated += [atom("/new","new-key",target="new-field")]
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(apply,store,work,[record(work["source_id"],facts=updated)]).result(timeout=15)
    remaining = collect(store.page_fields,owner,limit=2,cursor=first["next_cursor"])
    assert [(r["field_path"],r["value"]) for r in first["items"]+remaining] == [(r["field_path"],r["value"]) for r in expected]
    assert len(store.page_fields(owner)["items"]) == 6


@pytest.mark.parametrize("order",["asc","desc"])
def test_item_pages_and_child_collections_share_cut_across_concurrent_changes(store,order):
    work = job(store)
    initial = [atom(f"/{i}",f"old-{i}",kind="custom",target="value",key=f"item-{i}") for i in range(4)]
    owner = apply(store,work,[record(work["source_id"],facts=initial)])["entity_ids"][0]
    initial_items = store.page_items(owner,limit=200,order=order)["items"]
    first = store.page_items(owner,limit=1,order=order)
    changed = [atom(f"/{i}",f"new-{i}",kind="custom",target="value",key=f"item-{i}") for i in range(4)]
    changed += [atom("/new","new-item",kind="custom",key="new-item")]
    apply(store,work,[record(work["source_id"],facts=changed)])
    remaining = collect(store.page_items,owner,limit=1,order=order,cursor=first["next_cursor"])
    stable = first["items"]+remaining
    assert [r["id"] for r in stable] == [r["id"] for r in initial_items]
    assert all(r["version"] == 1 for r in stable)
    assert len(store.page_items(owner)["items"]) == 5
    for item in stable:
        assert item["fields"]["included"] is False and item["history"]["included"] is False
        children = collect(store.page_fields,owner,item_id=item["id"],cursor=item["fields"]["cursor"])
        assert len(children) == 1 and children[0]["value"].startswith("old-")
        history = collect(store.page_history,owner,item_id=item["id"],cursor=item["history"]["cursor"])
        assert len(history) == 1 and history[0]["entity_version"] == 1


def test_fields_flags_are_evaluated_against_value_in_same_snapshot(store):
    work = job(store)
    old = atom(value="number-A",kind="phone",target="number",key="contact",flags={"is_whatsapp":{"value":True}})
    owner = apply(store,work,[record(work["source_id"],facts=[old])])["entity_ids"][0]
    item = store.page_items(owner)["items"][0]
    apply(store,work,[record(work["source_id"],facts=[atom(value="number-B",kind="phone",target="number",key="contact")])])
    at_cut = collect(store.page_fields,owner,item_id=item["id"],cursor=item["fields"]["cursor"],limit=1)
    assert next(row for row in at_cut if row["dimension"] == "value")["value"] == "number-A"
    old_flag = next(row for row in at_cut if row["dimension"] == "flag:is_whatsapp")
    assert old_flag["value"] is True and old_flag["applicable"] is True
    current = store.page_fields(owner,dimension="flag:is_whatsapp")["items"][0]
    assert current["value"] is None and current["value_json"] == "null" and current["applicable"] is False
    assert current["normalized_value"] is True and current["normalized_json"] == "true"


def test_field_projection_follows_last_applied_sequence_inside_one_operation(store):
    work = job(store)
    # UUID ordering does not define the winning fact when source dates differ.
    pair = [atom("/a","older",observed_at="2025-01-01T00:00:00Z"),
            atom("/b","newer",observed_at="2025-02-01T00:00:00Z")]
    owner = apply(store,work,[record(work["source_id"],facts=pair)])["entity_ids"][0]
    page = store.page_fields(owner)
    assert page["items"][0]["value"] == "newer"
    assert page["items"][0]["operation_sequence"] == 2
    assert page["items"][0]["item_version"] == 1
    assert store.page_items(owner)["items"][0]["version"] == 1


def test_expired_cursor_is_rejected_cleanup_keeps_canonical_history(store):
    _,owner = history_fixture(store)
    token = store.page_history(owner,limit=1)["next_cursor"]
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    before = store.page_history(owner,limit=200)["items"]
    with store.connection() as c:
        c.execute("UPDATE read_cursors SET started_at=clock_timestamp()-interval '2 hour',expires_at=clock_timestamp()-interval '1 hour',deadline_at=clock_timestamp()-interval '30 minute' WHERE token_hash=%s",(token_hash,))
    with pytest.raises(InvalidCursor): store.page_history(owner,cursor=token)
    assert store.cleanup_read_cursors(1) == 1
    with pytest.raises(InvalidCursor): store.page_history(owner,cursor=token)
    assert store.page_history(owner,limit=200)["items"] == before


def test_sliding_cursor_ttl_never_exceeds_original_deadline(store):
    _,owner = history_fixture(store)
    token = store.page_history(owner,limit=1)["next_cursor"]
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with store.connection() as c:
        row = c.execute("UPDATE read_cursors SET started_at=clock_timestamp()-interval '59 minute',expires_at=clock_timestamp()+interval '30 second',deadline_at=clock_timestamp()+interval '1 minute' WHERE token_hash=%s RETURNING deadline_at",(token_hash,)).fetchone()
    page = store.page_history(owner,limit=1,cursor=token)
    assert page["snapshot"]["expires_at"] == row["deadline_at"].isoformat()
    assert page["snapshot"]["deadline_at"] == row["deadline_at"].isoformat()


@pytest.mark.parametrize("bad",[0,201,-1,True,1.5,"2",None])
def test_limits_are_strict_and_bounded(store,bad):
    _,owner = history_fixture(store)
    for method in (store.page_history,store.page_fields,store.page_items):
        with pytest.raises(CanonicalError): method(owner,limit=bad)


def test_invalid_context_and_empty_filters_are_explicit(store):
    _,owner = history_fixture(store)
    assert store.page_history(owner,field_path="missing")["items"] == []
    assert store.page_fields(owner,source_id="missing")["has_more"] is False
    assert store.page_items(owner,kind="missing")["next_cursor"] is None
    with pytest.raises(CanonicalError): store.page_history(owner,item_id="bad-uuid")
    with pytest.raises(CanonicalError): store.page_history(owner,field_path="x"*9000)
    with pytest.raises(CanonicalError): store.page_items(owner,order=[])
    with pytest.raises(InvalidCursor): store.page_history(owner,cursor="x"*10000)


def test_page_byte_budget_reduces_count_without_skipping_and_reports_giant_row(store,monkeypatch):
    _,owner = history_fixture(store)
    expected = store.page_history(owner,limit=200)["items"]
    sizes = [len(json_text(row)) for row in expected]
    monkeypatch.setattr(module,"MAX_PAGE_BYTES",max(sizes)+module.PAGE_ENVELOPE_BYTES+100)
    page = store.page_history(owner,limit=200)
    assert len(page["items"]) == 1 and page["has_more"] is True
    assert len(json_text(page)) <= module.MAX_PAGE_BYTES
    actual = page["items"]+collect(store.page_history,owner,cursor=page["next_cursor"],limit=200)
    assert [r["observation_id"] for r in actual] == [r["observation_id"] for r in expected]
    monkeypatch.setattr(module,"MAX_PAGE_BYTES",10)
    with pytest.raises(CanonicalRowTooLarge): store.page_history(owner,limit=1)


def test_item_collection_tokens_fit_within_full_response_budget(store,monkeypatch):
    work = job(store)
    facts = [atom(f"/{i}","value",kind="custom",key=f"item-{i}") for i in range(5)]
    owner = apply(store,work,[record(work["source_id"],facts=facts)])["entity_ids"][0]
    monkeypatch.setattr(module,"MAX_PAGE_BYTES",2000)
    page = store.page_items(owner,limit=200)
    assert 1 <= len(page["items"]) < 5 and page["has_more"] is True
    assert len(json_text(page)) <= module.MAX_PAGE_BYTES


def test_pagination_uses_per_owner_partition_indexes(store):
    with store.connection() as c:
        rows = c.execute("SELECT indexname,indexdef FROM pg_indexes WHERE schemaname=%s AND indexname IN ('observation_item_page_idx','observation_field_page_idx','observation_source_page_idx','observation_projection_page_idx','item_kind_page_idx')",(store.schema,)).fetchall()
    assert len(rows) == 5
    assert all("owner_id" in row["indexdef"] for row in rows)
    projection = next(row for row in rows if row["indexname"] == "observation_projection_page_idx")
    assert "WHERE applied" in projection["indexdef"]

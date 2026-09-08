"""PostgreSQL integration, restricted to the private synthetic fixture on 18769."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
from threading import Barrier
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
import pytest

from bigbase.canonical_store import (
    CanonicalStore, CanonicalError, CheckpointConflict, DeploymentMismatch,
    IdempotencyConflict, IdentityConflict, decode, digest, identifier, json_text,
)


@pytest.fixture(scope="module")
def store():
    dsn = os.environ.get("BIGBASE_TEST_PG_DSN")
    if not dsn:
        pytest.skip("Requires the explicit isolated PostgreSQL fixture DSN")
    with psycopg.connect(dsn) as c:
        database, port, address, version = c.execute("SELECT current_database(),current_setting('port'),inet_server_addr(),current_setting('server_version_num')::integer").fetchone()
        assert database == "bigbase_test" and port == "18769" and address is None
        assert 180000 <= version < 190000
    repository = CanonicalStore(dsn, "cbtest_" + uuid4().hex)
    repository.initialize()
    try:
        yield repository
    finally:
        with repository.connection() as c:
            c.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(repository.schema)))


def atom(path="/name", value="Pessoa sintética", *, target="name", kind="identity", key="identity", **extras):
    input_type = ("null" if value is None else "boolean" if type(value) is bool else "integer" if type(value) is int
                  else "decimal" if isinstance(value, Decimal) else "empty_object" if value == {} else "empty_array" if value == [] else "text")
    return {"id": digest(path), "source_path": path, "target_path": target, "target_kind": kind,
            "item_key": key, "input_value": value, "input_type": input_type,
            "normalized_value": value, "status": "unknown", **extras}


def record(source, external="1", facts=None, document=None, **extras):
    facts = facts if facts is not None else [atom()]
    data = {"source_id":source,"source_record_id":external,"adapter_version":"synthetic-v1",
            "entity_type":"person","facts":facts,"containers":[{"source_path":"","type":"object","length":len(facts)}], **extras}
    data["record_hash"] = digest(data)
    if document:
        data["identity_candidate"] = {"country":"TEST","type":"SYNTHETIC","value":document}
    return data


def job(store, source=None):
    source = source or "test-" + uuid4().hex
    return store.create_job(uuid4().hex, source, {"immutable_source":"synthetic", "source_version":"fixture-v1"})


def apply(store, work, records, checkpoint=None, cursor=None):
    current = store.get_job(work["id"])["checkpoint"] if checkpoint is None else checkpoint
    return store.apply_batch(records,job_id=work["id"],expected_checkpoint=current,next_checkpoint=current+len(records),
                             next_cursor=cursor,actor_id="test-actor")


def observations(store, owner):
    with store.connection() as c:
        return c.execute("SELECT * FROM observations WHERE owner_id=%s ORDER BY received_at,observation_id", (UUID(owner),)).fetchall()


def fields(entity):
    return {(item["kind"], field["path"]):field for item in entity["items"] for field in item["fields"]}


def test_deployment_identity_is_explicit_immutable_and_partitioned(store):
    info = store.deployment_info()
    assert info["environment"] == "synthetic" and info["schema"] == store.schema
    assert info["database"] == "bigbase_test" and info["server_port"] == 18769
    assert store.initialize()["deployment_id"] == info["deployment_id"]
    with pytest.raises(DeploymentMismatch):
        store.initialize(environment="production")
    with store.connection() as c:
        counts = c.execute("SELECT p.relname,count(*) AS n FROM pg_inherits i JOIN pg_class p ON p.oid=i.inhparent JOIN pg_namespace n ON n.oid=p.relnamespace WHERE n.nspname=%s GROUP BY p.relname", (store.schema,)).fetchall()
        assert {r["relname"]:r["n"] for r in counts if r["relname"] in {"identity_keys","items","observations","field_state"}} == {name:64 for name in ("identity_keys","items","observations","field_state")}
    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
        with store.connection() as c:
            c.execute("UPDATE canonical_meta SET environment='production'")


def test_exact_atoms_null_false_zero_decimal_unicode_and_structure(store):
    work = job(store)
    values = [None, False, 0, "", {}, [], Decimal("12345678901234567890.12345678901234567890"), "a\x00b\ud800"]
    atoms = [atom(f"/odd\x00/{i}", value, target=f"field\x00{i}", kind="custom", key=f"unknown/{i}",
                  input_json=json_text(value), normalized_json=json_text(value),
                  item_attributes={"unknown\x00":"escaped\ud800"}, normalization={"rule":"preserved"}) for i,value in enumerate(values)]
    prepared = record(work["source_id"], "source\x00record", atoms)
    prepared["containers"] = [{"source_path":"","type":"object","length":1},
                              {"source_path":"/odd\x00","type":"array","length":len(values)}]
    receipt = apply(store,work,[prepared],cursor={"seen":1,"last_id":"source\x00record"})
    history = observations(store,receipt["entity_ids"][0])
    assert len(history) == len(values)
    by_path = {decode(row["source_path_json"]):row for row in history}
    for i,value in enumerate(values):
        row = by_path[f"/odd\x00/{i}"]
        assert row["input_json"] == json_text(value)
        assert type(decode(row["input_json"])) is type(value)
        assert decode(row["input_json"]) == value
        assert decode(row["metadata_json"])["item_attributes"]["unknown\x00"] == "escaped\ud800"
    assert store.get_job(work["id"])["cursor"] == {"seen":1,"last_id":"source\x00record"}
    with store.connection() as c:
        containers = c.execute("SELECT path_json,container_type,length FROM source_containers WHERE operation_id=%s", (history[0]["operation_id"],)).fetchall()
        assert {(decode(r["path_json"]),r["container_type"],r["length"]) for r in containers} == {("","object",1),("/odd\x00","array",8)}


def test_decimal_source_lexeme_survives_without_canonical_reformatting(store):
    from bigbase.source_adapters import ExactDecimal
    work = job(store)
    literal = "1.23000000000000000001e+004"
    value = ExactDecimal(literal)
    prepared = record(work["source_id"], facts=[atom(value=value,input_json=literal,normalized_json=literal,input_encoding="source_decimal_lexeme",input_lexeme_available=True)])
    receipt = apply(store,work,[prepared])
    row = observations(store,receipt["entity_ids"][0])[0]
    assert row["input_json"] == literal and row["normalized_json"] == literal
    assert row["input_encoding"] == "source_decimal_lexeme"


def test_binary_float_and_whole_subtree_are_rejected_before_writes(store):
    work = job(store)
    with pytest.raises(CanonicalError,match="Binary floats"):
        apply(store,work,[record(work["source_id"],facts=[atom(value=0.1)])])
    with pytest.raises(CanonicalError,match="source subtree"):
        apply(store,work,[record(work["source_id"],facts=[atom(value={"name":"do not store whole record"})])])
    assert store.get_job(work["id"])["checkpoint"] == 0


def test_batch_replay_operation_replay_and_altered_cursor_conflict(store):
    work = job(store)
    prepared = record(work["source_id"])
    first = apply(store,work,[prepared],checkpoint=0,cursor={"seen":1,"offset":12})
    second = apply(store,work,[deepcopy(prepared)],checkpoint=0,cursor={"seen":1,"offset":12})
    assert second == {**first,"replayed":True}
    with pytest.raises(IdempotencyConflict):
        apply(store,work,[prepared],checkpoint=0,cursor={"seen":1,"offset":13})
    repeated_job = job(store,work["source_id"])
    third = apply(store,repeated_job,[prepared])
    assert third["entity_ids"] == first["entity_ids"] and third["operations_created"] == third["observations_created"] == 0
    assert store.get_entity(first["entity_ids"][0])["version"] == 1
    assert len(observations(store,first["entity_ids"][0])) == 1
    changed = deepcopy(prepared)
    changed["facts"][0]["normalized_value"] = "different prepared output with same version/hash"
    with pytest.raises(IdempotencyConflict):
        apply(store,job(store,work["source_id"]),[changed])


def test_multiple_sources_share_document_but_never_shared_phone(store):
    document = uuid4().hex
    one,two = job(store),job(store)
    first = apply(store,one,[record(one["source_id"],document=document)])
    second = apply(store,two,[record(two["source_id"],document=document)])
    assert first["entity_ids"] == second["entity_ids"]
    entity = store.get_entity(first["entity_ids"][0])
    assert entity["version"] == 2 and len(entity["items"]) == 1
    assert {decode(r["source_id_json"]) for r in observations(store,entity["id"])} == {one["source_id"],two["source_id"]}
    work = job(store)
    phone = atom(value="+5511999990000",kind="phone",target="number",key="same-shared-phone")
    result = apply(store,work,[record(work["source_id"],"1",[phone]),record(work["source_id"],"2",[phone])])
    assert len(set(result["entity_ids"])) == 2


def test_ambiguous_identity_rolls_back_entire_batch_and_cursor(store):
    source_job = job(store)
    a = apply(store,source_job,[record(source_job["source_id"],"existing")])
    doc_job = job(store)
    document = uuid4().hex
    b = apply(store,doc_job,[record(doc_job["source_id"],document=document)])
    assert a["entity_ids"] != b["entity_ids"]
    before = store.get_job(source_job["id"])
    with pytest.raises(IdentityConflict):
        apply(store,source_job,[record(source_job["source_id"],"must-rollback"),
                               record(source_job["source_id"],"existing",document=document)],cursor={"offset":999})
    assert store.get_job(source_job["id"]) == before
    source_key = digest(["source",source_job["source_id"],"must-rollback"])
    with store.connection() as c:
        assert c.execute("SELECT * FROM identity_keys WHERE key_hash=%s", (source_key,)).fetchone() is None
    assert len(observations(store,a["entity_ids"][0])) == 1
    assert len(observations(store,b["entity_ids"][0])) == 1


def test_injected_outbox_failure_rolls_back_history_projection_and_checkpoint(store):
    work = job(store)
    trigger_name = "fail_" + uuid4().hex
    with store.connection() as c:
        c.execute(sql.SQL("CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected synthetic outbox failure'; END; $$").format(sql.Identifier(trigger_name)))
        c.execute(sql.SQL("CREATE TRIGGER {} BEFORE INSERT ON outbox FOR EACH ROW EXECUTE FUNCTION {}()").format(sql.Identifier(trigger_name),sql.Identifier(trigger_name)))
    try:
        with pytest.raises(psycopg.errors.RaiseException):
            apply(store,work,[record(work["source_id"])],cursor={"offset":1})
    finally:
        with store.connection() as c:
            c.execute(sql.SQL("DROP TRIGGER {} ON outbox").format(sql.Identifier(trigger_name)))
            c.execute(sql.SQL("DROP FUNCTION {}()").format(sql.Identifier(trigger_name)))
    after = store.get_job(work["id"])
    assert after["checkpoint"] == 0 and after["cursor"] is None and after["records_processed"] == 0
    with store.connection() as c:
        assert c.execute("SELECT * FROM operations WHERE source_id_json=%s", (json_text(work["source_id"]),)).fetchone() is None
    retry = apply(store,work,[record(work["source_id"])],cursor={"offset":1})
    assert retry["observations_created"] == 1


def test_field_precedence_is_independent_and_keeps_original_metadata(store):
    work = job(store)
    first = record(work["source_id"], facts=[
        atom("/street","Rua sintética",target="street",kind="address",key="address",observed_at="2025-01-01T00:00:00Z"),
        atom("/city","Cidade antiga",target="city",kind="address",key="address",observed_at="2025-01-01T00:00:00Z")])
    receipt = apply(store,work,[first])
    updated = record(work["source_id"],facts=[atom("/city","Cidade atual",target="city",kind="address",key="address",source_updated_at="2025-02-01T00:00:00Z",item_attributes={"country":"BR"})])
    apply(store,work,[updated])
    apply(store,work,[record(work["source_id"],facts=[atom("/city","Sem data",target="city",kind="address",key="address")])])
    apply(store,work,[record(work["source_id"],facts=[atom("/city","Antiga atrasada",target="city",kind="address",key="address",observed_at="2025-01-20T00:00:00Z")])])
    future = (datetime.now(timezone.utc)+timedelta(days=1)).isoformat()
    apply(store,work,[record(work["source_id"],facts=[atom("/city","Futura",target="city",kind="address",key="address",observed_at=future)])])
    current = fields(store.get_entity(receipt["entity_ids"][0]))
    assert current[("address","city")]["value"] == "Cidade atual"
    assert current[("address","city")]["metadata"]["item_attributes"] == {"country":"BR"}
    assert current[("address","street")]["effective_at"] == "2025-01-01T00:00:00+00:00"
    history = observations(store,receipt["entity_ids"][0])
    assert {r["pending_reason"] for r in history if not r["applied"]} == {"future_date","undated_against_dated","older_observation"}
    changed = next(r for r in history if decode(r["normalized_json"]) == "Cidade atual")
    assert decode(changed["previous_value_json"]) == "Cidade antiga"


def test_independent_flags_preserve_false_null_and_do_not_follow_changed_value(store):
    work = job(store)
    facts = [atom(value="+5511999990000",kind="phone",target="number",key="stable-contact",observed_at="2025-01-01T00:00:00Z",
                  flags={"is_whatsapp":{"value":False,"observed_at":"2025-01-03T00:00:00Z"},"valid":{"value":None}})]
    receipt = apply(store,work,[record(work["source_id"],facts=facts)])
    owner = receipt["entity_ids"][0]
    contact = store.get_entity(owner)["items"][0]
    assert {flag["name"]:flag["value"] for flag in contact["flags"]} == {"is_whatsapp":False,"valid":None}
    apply(store,work,[record(work["source_id"],facts=[atom(value="+5511999990001",kind="phone",target="number",key="stable-contact",observed_at="2025-02-01T00:00:00Z")])])
    contact = store.get_entity(owner)["items"][0]
    assert all(flag["value"] is None and flag["applicable"] is False for flag in contact["flags"])
    old = record(work["source_id"],facts=[atom(value="+5511999990000",kind="phone",target="number",key="stable-contact",observed_at="2025-01-01T00:00:00Z",flags={"is_whatsapp":{"value":True,"observed_at":"2025-03-01T00:00:00Z"}})])
    apply(store,work,[old])
    history = observations(store,owner)
    delayed = [r for r in history if r["dimension"] == "flag:is_whatsapp" and decode(r["normalized_json"]) is True][0]
    assert delayed["applied"] is False and delayed["pending_reason"] == "value_mismatch"


@pytest.mark.parametrize("statement",[
    "UPDATE observations SET status='invalid' WHERE owner_id=%s",
    "DELETE FROM observations WHERE owner_id=%s",
    "DELETE FROM operations WHERE owner_id=%s",
    "DELETE FROM identity_keys WHERE owner_id=%s",
])
def test_history_and_identity_cannot_be_rewritten_or_deleted(store,statement):
    work = job(store)
    owner = apply(store,work,[record(work["source_id"])])["entity_ids"][0]
    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
        with store.connection() as c:
            c.execute(statement,(UUID(owner),))
    assert len(observations(store,owner)) == 1


def test_concurrent_replay_commits_once_and_new_aliases_deduplicate(store):
    work = job(store)
    prepared = record(work["source_id"])
    barrier = Barrier(2)
    def replay():
        barrier.wait(timeout=10)
        return apply(store,work,[prepared],checkpoint=0,cursor={"seen":1})
    with ThreadPoolExecutor(max_workers=2) as executor:
        first,second = list(executor.map(lambda _:replay(),range(2)))
    assert sorted([first["replayed"],second["replayed"]]) == [False,True]
    assert store.get_job(work["id"])["records_processed"] == 1
    assert len(observations(store,first["entity_ids"][0])) == 1
    one,two = job(store),job(store)
    document = uuid4().hex
    barrier = Barrier(2)
    def shared_document(work):
        barrier.wait(timeout=10)
        return apply(store,work,[record(work["source_id"],document=document)])
    with ThreadPoolExecutor(max_workers=2) as executor:
        result = list(executor.map(shared_document,[one,two]))
    assert result[0]["entity_ids"] == result[1]["entity_ids"]
    assert store.get_entity(result[0]["entity_ids"][0])["version"] == 2


def test_concurrent_different_batches_at_same_checkpoint_reject_one(store):
    work = job(store)
    barrier = Barrier(2)
    def competing(external):
        barrier.wait(timeout=10)
        try:
            return apply(store,work,[record(work["source_id"],external)],checkpoint=0)
        except IdempotencyConflict:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as executor:
        result = list(executor.map(competing,["one","two"]))
    assert result.count("conflict") == 1
    assert store.get_job(work["id"])["records_processed"] == 1


def test_outbox_claim_lease_ack_and_reclaim(store):
    # Acknowledge pending events generated by prior cases in this module.
    for event in store.claim_outbox(1000):
        assert store.acknowledge_outbox(event["event_id"],event["lease_token"])
    work = job(store)
    receipt = apply(store,work,[record(work["source_id"],"1"),record(work["source_id"],"2")])
    barrier = Barrier(2)
    def claim():
        barrier.wait(timeout=10)
        return store.claim_outbox(1)
    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(lambda _:claim(),range(2)))
    left,right = claims[0][0],claims[1][0]
    assert left["event_id"] != right["event_id"]
    assert {left["owner_id"],right["owner_id"]} == set(receipt["entity_ids"])
    assert store.claim_outbox() == []
    assert not store.acknowledge_outbox(left["event_id"],str(uuid4()))
    with store.connection() as c:
        c.execute("UPDATE outbox SET lease_until=clock_timestamp()-interval '1 second' WHERE event_id=%s",(UUID(left["event_id"]),))
    reclaimed = store.claim_outbox(1)[0]
    assert reclaimed["event_id"] == left["event_id"] and reclaimed["lease_token"] != left["lease_token"]
    assert reclaimed["attempts"] == 2
    assert not store.acknowledge_outbox(left["event_id"],left["lease_token"])
    assert store.acknowledge_outbox(reclaimed["event_id"],reclaimed["lease_token"])
    assert store.acknowledge_outbox(right["event_id"],right["lease_token"])
    assert not store.acknowledge_outbox(right["event_id"],right["lease_token"])


def test_durable_job_new_connection_checkpoint_terminal_and_pinned_metadata(store):
    key = uuid4().hex
    work = store.create_job(key,"synthetic-file",{"file_sha256":"f"*64})
    assert store.create_job(key,"synthetic-file",{"file_sha256":"f"*64}) == work
    with pytest.raises(IdempotencyConflict):
        store.create_job(key,"synthetic-file",{"file_sha256":"e"*64})
    apply(store,work,[record(work["source_id"],key)],cursor={"offset":132,"seen":1,"sha256":"f"*64})
    new_process_equivalent = CanonicalStore(store.dsn,store.schema)
    current = new_process_equivalent.get_job(work["id"])
    assert current["status"] == "processing" and current["cursor"]["offset"] == 132
    with pytest.raises(CheckpointConflict):
        new_process_equivalent.finish_job(work["id"],0)
    completed = new_process_equivalent.finish_job(work["id"],1)
    assert completed["status"] == "completed" and completed["records_processed"] == 1
    assert new_process_equivalent.finish_job(work["id"],1) == completed
    with pytest.raises(CheckpointConflict):
        apply(store,work,[record(work["source_id"],"extra")])


def test_mapper_contract_reconstructible_atoms_and_pending_unknowns(store):
    from bigbase.source_adapters import ExactDecimal, map_record
    import json
    raw = json.loads('{"CPF":"11144477735","NOME":"Pessoa Sintética","odd":{"0":[false,null,0,{},[]]},"precise":1.234567890123456789e-10,"unsafe\\u0000":"x\\ud800"}',parse_float=ExactDecimal)
    mapped = map_record("pessoas",uuid4().hex,raw)
    work = job(store,"pessoas")
    receipt = apply(store,work,[mapped],cursor={"seen":1})
    history = observations(store,receipt["entity_ids"][0])
    assert len(history) == len(mapped["facts"])
    by_path = {decode(r["source_path_json"]):r for r in history}
    for fact in mapped["facts"]:
        row = by_path[fact["source_path"]]
        assert row["input_json"] == fact["input_json"]
        assert row["normalized_json"] == fact["normalized_json"]
        assert decode(row["metadata_json"])["item_attributes"] == fact["item_attributes"]
    assert any(row["status"] == "pending" for row in history)
    assert not any(row["dimension"].startswith("flag:") for row in history)


def test_source_record_identity_does_not_cross_person_company(store):
    work = job(store)
    apply(store,work,[record(work["source_id"])])
    with pytest.raises(IdentityConflict):
        apply(store,work,[record(work["source_id"],entity_type="company")])


def test_literal_type_mismatch_rejects_false_as_zero(store):
    work = job(store)
    with pytest.raises(CanonicalError,match="differs"):
        apply(store,work,[record(work["source_id"],facts=[atom(value=False,input_json="0")])])
    assert store.get_job(work["id"])["checkpoint"] == 0


@pytest.mark.parametrize("value,literal",[
    ({"a":1},'{"a":true}'),
    ({"nested":[False,0]},'{"nested":[0,false]}'),
    ([{"value":False}], '[{"value":0}]'),
    ({"a":1},'{"a":0,"a":1}'),
])
def test_normalized_literal_checks_nested_types_and_duplicate_keys(store,value,literal):
    work = job(store)
    prepared = record(work["source_id"],facts=[atom(value="original",normalized_value=value,normalized_json=literal)])
    with pytest.raises(CanonicalError):
        apply(store,work,[prepared])
    assert store.get_job(work["id"])["checkpoint"] == 0


def test_input_type_must_match_source_value(store):
    work = job(store)
    prepared = record(work["source_id"],facts=[atom(value=0,input_type="boolean")])
    with pytest.raises(CanonicalError,match="input_type"):
        apply(store,work,[prepared])


def test_new_normalizer_version_creates_new_history_without_adapter_change(store,monkeypatch):
    from bigbase import source_adapters
    raw = {"NOME":"Pessoa sintética"}
    external = uuid4().hex
    first = source_adapters.map_record("pessoas",external,raw)
    work = job(store,"pessoas")
    receipt = apply(store,work,[first])
    monkeypatch.setattr(source_adapters,"NORMALIZER_VERSION",source_adapters.NORMALIZER_VERSION+".synthetic-next")
    second = source_adapters.map_record("pessoas",external,raw)
    assert first["adapter_version"] == second["adapter_version"]
    result = apply(store,work,[second])
    assert result["operations_created"] == 1 and result["entity_ids"] == receipt["entity_ids"]
    assert len(observations(store,receipt["entity_ids"][0])) == 2


def test_source_cpf_replacement_does_not_claim_unseen_other_cpf(store):
    source_job = job(store)
    def cpf_record(value):
        result = record(source_job["source_id"])
        result["identity_candidate"] = {"country":"BR","type":"CPF","value":value}
        return result
    old_cpf,new_cpf = "synthetic-old-"+uuid4().hex,"synthetic-new-"+uuid4().hex
    first = apply(store,source_job,[cpf_record(old_cpf)])
    with pytest.raises(IdentityConflict,match="different unique Brazilian document"):
        apply(store,source_job,[cpf_record(new_cpf)])
    assert store.get_job(source_job["id"])["checkpoint"] == 1
    with store.connection() as c:
        assert c.execute("SELECT owner_id FROM identity_keys WHERE key_hash=%s",(digest(["document","BR","CPF",new_cpf]),)).fetchone() is None
    other = job(store)
    other_record = record(other["source_id"])
    other_record["identity_candidate"] = {"country":"BR","type":"CPF","value":new_cpf}
    second = apply(store,other,[other_record])
    assert first["entity_ids"] != second["entity_ids"]


def test_previously_undocumented_source_can_receive_its_first_cpf(store):
    work = job(store)
    first = apply(store,work,[record(work["source_id"])])
    later = record(work["source_id"])
    later["record_hash"] = "b"*64
    later["identity_candidate"] = {"country":"BR","type":"CPF","value":"synthetic-"+uuid4().hex}
    second = apply(store,work,[later])
    assert first["entity_ids"] == second["entity_ids"]


def test_pending_ambiguous_birth_date_does_not_replace_resolved_projection(store):
    from bigbase.source_adapters import map_record
    work = job(store,"pessoas")
    external = uuid4().hex
    first = map_record("pessoas",external,{"DT_NASCIMENTO":"2000-01-01"})
    receipt = apply(store,work,[first])
    second = map_record("pessoas",external,{"DT_NASCIMENTO":"03/04/2000"})
    apply(store,work,[second])
    entity = store.get_entity(receipt["entity_ids"][0])
    assert fields(entity)[("identity","birth_date")]["value"] == "2000-01-01"
    pending = next(row for row in observations(store,entity["id"]) if row["status"] == "pending")
    assert pending["applied"] is False and pending["pending_reason"] == "pending_against_resolved"
    assert decode(pending["input_json"]) == "03/04/2000"
    assert decode(pending["metadata_json"])["pending_reason"] == "ambiguous_date_format"
    assert entity["items"][0]["version"] == 2


def test_indexed_identity_lookup_and_cursor_consistency(store):
    work = job(store)
    document = uuid4().hex
    prepared = record(work["source_id"],document=document)
    with pytest.raises(CheckpointConflict):
        apply(store,work,[prepared],cursor={"seen":2})
    assert store.get_job(work["id"])["checkpoint"] == 0
    receipt = apply(store,work,[prepared],cursor={"seen":1})
    owner = receipt["entity_ids"][0]
    assert store.lookup_identity(source_id=work["source_id"],source_record_id="1") == owner
    assert store.lookup_identity(document_type="SYNTHETIC",country="TEST",value=document) == owner
    assert store.lookup_identity(document_type="OTHER",country="TEST",value=document) is None
    with pytest.raises(CanonicalError):
        store.lookup_identity(source_id=work["source_id"],source_record_id="1",document_type="CPF",country="BR",value=document)


def test_normalizing_value_does_not_transfer_confirmation_to_new_value(store):
    work = job(store)
    first = record(work["source_id"],facts=[atom(value="1187654321",normalized_value="+5511987654321",kind="phone",target="number",key="phone",flags={"is_whatsapp":{"value":True}})])
    receipt = apply(store,work,[first])
    owner = receipt["entity_ids"][0]
    assert store.get_entity(owner)["items"][0]["flags"] == []
    flag = next(row for row in observations(store,owner) if row["dimension"] == "flag:is_whatsapp")
    assert flag["applied"] is False and flag["pending_reason"] == "value_mismatch"
    second = record(work["source_id"],facts=[atom(value="1187654321",normalized_value="+5511987654321",kind="phone",target="number",key="phone",flags={"is_whatsapp":{"value":True,"confirmed_value_json":'"+5511987654321"'}})])
    apply(store,work,[second])
    current = store.get_entity(owner)["items"][0]["flags"][0]
    assert current["value"] is True and current["applicable"] is True

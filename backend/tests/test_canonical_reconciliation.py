"""Readback proof against real PG with synthetic inputs and injected read faults."""
from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal
import json
from uuid import uuid4

import pytest

from bigbase.canonical_reconciliation import CanonicalReconciler, reconcile_reader
from bigbase.canonical_store import CanonicalError, json_text
from bigbase.migration_cli import execute_jsonl
from bigbase.migration_transport import JsonlSource, Page
from bigbase.source_adapters import ExactDecimal, map_record
from test_canonical_store import store, job, apply, atom, record


class ReadCursor:
    def __init__(self,cursor,query,transform):
        self.cursor,self.query,self.transform = cursor,query,transform
    def __getattr__(self,name): return getattr(self.cursor,name)
    def __enter__(self): self.cursor.__enter__();return self
    def __exit__(self,*args): return self.cursor.__exit__(*args)
    def execute(self,query,params):
        self.query = query if isinstance(query,str) else query.as_string(self.cursor.connection)
        self.cursor.execute(query,params)
        return self
    def fetchone(self):
        row = self.cursor.fetchone()
        rows = self.transform(self.query,[] if row is None else [deepcopy(row)])
        return rows[0] if rows else None
    def fetchall(self): return self.transform(self.query,deepcopy(self.cursor.fetchall()))
    def __iter__(self): return iter(self.transform(self.query,deepcopy(list(self.cursor))))


class ReadConnection:
    def __init__(self,connection,transform,log): self.connection,self.transform,self.log = connection,transform,log
    def execute(self,query,params=None):
        text = query if isinstance(query,str) else query.as_string(self.connection)
        self.log.append(text)
        return ReadCursor(self.connection.execute(query,params),text,self.transform)
    def cursor(self,*args,**kwargs): return ReadCursor(self.connection.cursor(*args,**kwargs),"",self.transform)


class ReadFaultStore:
    """Change rows returned to the checker; never modify SQL data or triggers."""
    def __init__(self,store,transform): self.store,self.transform,self.log = store,transform,[]
    def __getattr__(self,name): return getattr(self.store,name)
    @contextmanager
    def connection(self):
        with self.store.connection() as c: yield ReadConnection(c,self.transform,self.log)


def mapped_fixture(store):
    external = uuid4().hex
    raw = json.loads('{"NOME":"Pessoa Sintética Privada","unknown":{"0":[false,null,0,{},[]]},"precise":1.234567890123456789e-10,"unsafe\\u0000":"value\\ud800"}',parse_float=ExactDecimal)
    mapped = map_record("pessoas",external,raw,source_version="fixture-1")
    work = job(store,"pessoas")
    apply(store,work,[mapped])
    return work,mapped


def test_readback_matches_actual_atoms_and_is_read_only(store):
    work,mapped = mapped_fixture(store)
    before = store.get_job(work["id"])
    wrapper = ReadFaultStore(store,lambda _,rows:rows)
    proof = CanonicalReconciler(wrapper).check_record(mapped,expected_actor_id="test-actor")
    assert proof["passed"] and proof["divergences"] == 0
    assert proof["observations_checked"] == len(mapped["facts"])
    assert proof["containers_checked"] == len(mapped["containers"])
    assert store.get_job(work["id"]) == before
    assert any("READ ONLY" in query for query in wrapper.log)
    assert not any(query.lstrip().upper().startswith(("INSERT","UPDATE","DELETE","CREATE","ALTER")) for query in wrapper.log)
    assert "Pessoa Sintética" not in json_text(proof) and mapped["source_record_id"] not in json_text(proof)


@pytest.mark.parametrize("change,category",[
    ("missing","missing_observation"),("extra","extra_observation"),
    ("input_json","observation_input_json_mismatch"),("input_type","observation_input_type_mismatch"),
    ("normalized_json","observation_normalized_json_mismatch"),("metadata_json","observation_metadata_json_mismatch"),
    ("source_path_json","observation_source_path_json_mismatch"),("actor_id_json","observation_actor_id_json_mismatch"),
    ("status","observation_status_mismatch"),("applied","observation_applied_mismatch"),
    ("operation_sequence","observation_operation_sequence_mismatch"),
])
def test_same_declared_hash_does_not_hide_missing_extra_or_changed_atoms(store,change,category):
    _,mapped = mapped_fixture(store)
    def corrupt(query,rows):
        if "SELECT * FROM observations WHERE operation_id" not in query or not rows: return rows
        if change == "missing": return rows[1:]
        if change == "extra":
            extra = deepcopy(rows[0]);extra["observation_id"] = uuid4()
            return rows+[extra]
        replacements = {"input_json":'"different source value"',"input_type":"integer","normalized_json":"0.123456789012345678902",
            "metadata_json":'{}',"source_path_json":'"/different"',"actor_id_json":'"another actor"',
            "status":"changed","applied":not rows[0]["applied"],"operation_sequence":999}
        rows[0][change] = replacements[change]
        return rows
    proof = CanonicalReconciler(ReadFaultStore(store,corrupt)).check_record(mapped)
    assert not proof["passed"] and proof["discrepancy_counts"][category] >= 1
    assert not any(key.startswith("operation_prepared_hash") for key in proof["discrepancy_counts"])


@pytest.mark.parametrize("change",["missing","extra","type","length"])
def test_container_shape_and_empty_collections_are_checked(store,change):
    _,mapped = mapped_fixture(store)
    def corrupt(query,rows):
        if "SELECT * FROM source_containers WHERE operation_id" not in query or not rows: return rows
        if change == "missing": return rows[:-1]
        if change == "extra":
            extra = deepcopy(rows[0]);extra["path_hash"] = "f"*64
            return rows+[extra]
        if change == "type": rows[0]["container_type"] = "array" if rows[0]["container_type"] == "object" else "object"
        if change == "length": rows[0]["length"] += 1
        return rows
    result = CanonicalReconciler(ReadFaultStore(store,corrupt)).check_record(mapped)
    assert not result["passed"]
    assert any("container" in key for key in result["discrepancy_counts"])


def test_current_projection_corruption_is_detected_separately(store):
    _,mapped = mapped_fixture(store)
    def corrupt(query,rows):
        if "SELECT * FROM field_state WHERE owner_id" in query and rows: rows[0]["value_json"] = '"wrong-cache-value"'
        return rows
    result = CanonicalReconciler(ReadFaultStore(store,corrupt)).check_record(mapped)
    assert result["discrepancy_counts"]["projection_mismatch"] > 0


def test_multiple_sources_and_later_legitimate_versions_do_not_confuse_old_operation(store):
    cpf = "11144477735"
    one = map_record("pessoas",uuid4().hex,{"CPF":cpf,"NOME":"Nome anterior"},source_version="v1")
    two = map_record("pessoas_serasa",uuid4().hex,{"CPF":cpf,"NOME":"Nome recente"},source_version="v1")
    first,second = job(store,"pessoas"),job(store,"pessoas_serasa")
    apply(store,first,[one]);apply(store,second,[two])
    later = map_record("pessoas",one["source_record_id"],{"CPF":cpf,"NOME":"Outra atualização"},source_version="v2")
    apply(store,first,[later])
    reconciler = CanonicalReconciler(store)
    assert all(reconciler.check_record(mapped)["passed"] for mapped in (one,two,later))


def test_flag_binding_previous_values_and_original_actor_are_verified(store):
    work = job(store)
    first = record(work["source_id"],facts=[atom(value="old-phone",kind="phone",target="number",key="phone",flags={"valid":{"value":False},"is_whatsapp":{"value":True}})])
    second = record(work["source_id"],facts=[atom(value="new-phone",kind="phone",target="number",key="phone")])
    apply(store,work,[first,second])
    reconciler = CanonicalReconciler(store)
    assert reconciler.check_record(first)["passed"]
    assert reconciler.check_record(second)["passed"]
    assert not reconciler.check_record(first,expected_actor_id="a later replay actor")["passed"]


def source_file(tmp_path,count=10):
    path = tmp_path / "synthetic.jsonl"
    rows = [{"source_id":"pessoas","external_id":"synthetic-"+uuid4().hex,
             "record":{"NOME":f"Pessoa Sintética {i}","private_value":Decimal(f"0.{i}1234567890123456789"),"list":[False,None,0,{},[]]}}
            for i in range(count)]
    path.write_text("\n".join(json_text(row) for row in rows)+("\n" if rows else ""))
    return path


def test_ten_record_reader_pipeline_finishes_only_with_durable_full_readback_proof(store,tmp_path):
    path = source_file(tmp_path)
    with JsonlSource(path,page_size=3) as reader:
        result = execute_jsonl(reader,store,job_key=uuid4().hex,source_id="pessoas",actor_id="initial-operator",expected_records=10,synthetic=True)
    assert result["state"] == "completed" and result["verification"]["state"] == "verified"
    assert result["verification"]["records_checked"] == 10 and result["verification"]["divergences"] == 0
    persisted = store.get_job(result["job_id"])
    assert persisted["verification"] == result["verification"]
    with JsonlSource(path,page_size=4) as reader:
        progress = []
        proof = reconcile_reader(reader,store,expected_records=10,expected_actor_id=None,progress=lambda checked,errors:progress.append((checked,errors)))
    assert proof["complete"] and proof["passed"] and progress == [(i,0) for i in range(1,11)]
    assert "Pessoa Sintética" not in json_text(proof)


def test_partial_limit_cancel_and_wrong_count_never_claim_complete(store,tmp_path):
    path = source_file(tmp_path,3)
    with JsonlSource(path,page_size=2) as reader:
        execute_jsonl(reader,store,job_key=uuid4().hex,source_id="pessoas",actor_id="actor",expected_records=3,synthetic=True)
    with JsonlSource(path,page_size=2) as reader:
        limited = reconcile_reader(reader,store,expected_records=3,max_records=1)
    assert limited["state"] == "partial" and not limited["complete"] and limited["records_checked"] == 1
    with JsonlSource(path,page_size=2) as reader:
        cancelled = reconcile_reader(reader,store,expected_records=3,cancelled=lambda:True)
    assert cancelled["reason"] == "cancelled" and not cancelled["passed"]
    with JsonlSource(path,page_size=2) as reader:
        wrong = reconcile_reader(reader,store,expected_records=4)
    assert wrong["state"] == "failed" and not wrong["complete"]


def test_missing_operation_and_reader_without_eof_are_not_verified(store,tmp_path):
    path = source_file(tmp_path,1)
    with JsonlSource(path) as reader:
        absent = reconcile_reader(reader,store,expected_records=1)
    assert absent["discrepancy_counts"]["missing_operation"] == 1 and not absent["complete"]
    class Incomplete:
        identity = {"kind":"synthetic-incomplete"}
        def pages(self,checkpoint=None):
            yield Page([],{"seen":0},False)
    partial = reconcile_reader(Incomplete(),store,expected_records=0)
    assert partial["state"] == "partial" and not partial["complete"]


def test_completion_proof_cannot_be_missing_or_rebound(store,tmp_path):
    path = source_file(tmp_path,1)
    with JsonlSource(path) as reader:
        result = execute_jsonl(reader,store,job_key=uuid4().hex,source_id="pessoas",actor_id="actor",expected_records=1,synthetic=True)
    existing = store.get_job(result["job_id"])
    metadata = existing["metadata"]
    work = store.create_job(uuid4().hex,"pessoas",metadata)
    with JsonlSource(path) as reader:
        for page in reader.pages():
            if page.records:
                mapped = [map_record(row["source_id"],row["external_id"],row["record"]) for row in page.records]
                apply(store,work,mapped,cursor=page.checkpoint)
    with pytest.raises(CanonicalError): store.finish_job(work["id"],1)
    wrong = deepcopy(result["verification"]);wrong["destination_deployment_id"] = str(uuid4())
    with pytest.raises(CanonicalError): store.finish_job(work["id"],1,verification=wrong)
    completed = store.finish_job(work["id"],1,verification=result["verification"])
    assert completed["status"] == "completed" and completed["verification"]["complete"] is True

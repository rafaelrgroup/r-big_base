"""Read-only, bounded comparison of source atoms against actual PostgreSQL rows.

Operational results contain counts, stable error categories and aggregate hashes;
source values, paths, external IDs, SQL, DSNs and credentials are never reported.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
from uuid import UUID, uuid4

from psycopg import sql

from .canonical_store import (
    CanonicalError, _date, _precedence, decode, digest, identifier, json_text,
    prepare_canonical_record,
)
from .source_adapters import ADAPTER_VERSION, MAX_LEAVES, NORMALIZER_VERSION, map_record


PROOF_VERSION = "canonical-readback-2026-09-08.1"
MAX_PREPARED_BYTES = 32 * 1024 * 1024
MAX_DESTINATION_BYTES = 64 * 1024 * 1024
MAX_OBSERVATIONS = 100000
MAX_CONTAINERS = 20001


class ReconciliationError(ValueError):
    """A stable, non-personal code only."""


def _safe_tree(value):
    if isinstance(value, UUID): return str(value)
    if isinstance(value, datetime): return value.isoformat()
    if isinstance(value, dict): return {key:_safe_tree(child) for key,child in value.items()}
    if isinstance(value, list): return [_safe_tree(child) for child in value]
    return value


def _rows(c, query, params, *, maximum, budget, evidence):
    result, size = [],0
    with c.cursor(name="reconcile_"+uuid4().hex) as cursor:
        cursor.itersize = 1
        cursor.execute(query,params)
        for row in cursor:
            encoded = json_text(_safe_tree(row)).encode("ascii")
            size += len(encoded)
            if len(result) >= maximum or size > budget:
                raise ReconciliationError("DESTINATION_RECORD_LIMIT")
            evidence.update(len(encoded).to_bytes(8,"big"))
            evidence.update(encoded)
            result.append(row)
    return result


def _states(c,owner,keys,cut):
    result = {}
    keys = sorted(keys,key=lambda key:(str(key[0]),key[1],key[2]))
    for offset in range(0,len(keys),256):
        batch = keys[offset:offset+256]
        params = [value for key in batch for value in key] + [owner,cut]
        query = sql.SQL("""WITH requested(item_id,path_hash,dimension) AS (VALUES {})
            SELECT k.item_id,k.path_hash,k.dimension,h.* FROM requested k LEFT JOIN LATERAL (
              SELECT o.observation_id,o.normalized_json AS value_json,o.metadata_json,o.status,o.effective_at,
                o.received_at,o.source_id_json,o.binding_hash FROM observations o WHERE o.owner_id=%s
                AND o.item_id=k.item_id AND o.target_path_hash=k.path_hash AND o.dimension=k.dimension
                AND o.applied AND o.entity_version<=%s ORDER BY o.entity_version DESC,o.operation_sequence DESC LIMIT 1
            ) h ON true""").format(sql.SQL(",").join(sql.SQL("(%s::uuid,%s::text,%s::text)") for _ in batch))
        for row in c.execute(query,params).fetchall():
            key = row["item_id"],row["path_hash"],row["dimension"]
            result[key] = row if row["observation_id"] is not None else None
    return result


def _items_before(c,owner,item_ids,cut):
    result = {}
    item_ids = sorted(item_ids,key=str)
    for offset in range(0,len(item_ids),256):
        batch = item_ids[offset:offset+256]
        query = sql.SQL("""WITH requested(item_id) AS (VALUES {}) SELECT k.item_id,h.item_version
            FROM requested k LEFT JOIN LATERAL (SELECT o.item_version FROM observations o WHERE o.owner_id=%s
              AND o.item_id=k.item_id AND o.entity_version<=%s ORDER BY o.entity_version DESC,o.operation_sequence DESC LIMIT 1) h ON true""").format(
                sql.SQL(",").join(sql.SQL("(%s::uuid)") for _ in batch))
        for row in c.execute(query,[*batch,owner,cut]).fetchall():
            result[row["item_id"]] = row["item_version"] or 0
    return result


def _expected_observations(prepared,operation,states,item_versions):
    expected, sequence = [],0
    owner,op_id,received = operation["owner_id"],operation["operation_id"],operation["received_at"]
    for fact in prepared["facts"]:
        raw = fact["raw"]
        item = identifier("item",[str(owner),fact["kind"],raw["item_key"]])
        path_hash = digest(raw["target_path"])
        entries = [(None,None)] + list(raw.get("flags",{}).items())
        for flag_name,flag in entries:
            sequence += 1
            dimension = "flag:"+flag_name if flag is not None else "value"
            event = identifier("observation",[str(op_id),fact["id"],dimension])
            previous = states.get((item,path_hash,dimension))
            source_updated = _date(flag.get("source_updated_at")) if flag is not None else fact["source_updated_at"]
            observed = _date(flag.get("observed_at")) if flag is not None else fact["observed_at"]
            effective = source_updated or observed
            applied,reason = _precedence(previous,effective,received,event)
            if previous and fact["status"] == "pending" and previous["status"] != "pending":
                applied,reason = False,"pending_against_resolved"
            normalized = json_text(flag["value"]) if flag is not None else fact["normalized_json"]
            binding = None
            if flag is not None:
                binding = digest(decode(flag.get("confirmed_value_json",fact["input_json"])))
                current_value = states.get((item,path_hash,"value"))
                if not current_value or digest(decode(current_value["value_json"])) != binding:
                    applied,reason = False,"value_mismatch"
            row = {"owner_id":owner,"observation_id":event,"item_id":item,"operation_id":op_id,
                   "entity_version":operation["entity_version"],"operation_sequence":sequence,"item_version":item_versions[item]+1,
                   "source_fact_id_json":json_text(fact["id"]),"source_path_json":json_text(raw["source_path"]),
                   "target_path_hash":path_hash,"target_path_json":json_text(raw["target_path"]),"dimension":dimension,
                   "input_json":normalized if flag is not None else fact["input_json"],
                   "input_type":("null" if flag["value"] is None else "boolean") if flag is not None else fact["input_type"],
                   "input_encoding":"canonical_value" if flag is not None else fact["input_encoding"],
                   "normalized_json":normalized,"metadata_json":json_text(flag) if flag is not None else fact["metadata_json"],
                   "source_id_json":json_text(prepared["source"]),"source_id_hash":digest(prepared["source"]),
                   "source_updated_at":source_updated,"observed_at":observed,"effective_at":effective,"received_at":received,
                   "actor_id_json":operation["actor_id_json"],"status":fact["status"],"applied":applied,"pending_reason":reason,
                   "previous_observation_id":previous["observation_id"] if previous else None,
                   "previous_value_json":previous["value_json"] if previous else None,"binding_hash":binding}
            expected.append(row)
            if applied:
                states[item,path_hash,dimension] = {**row,"value_json":normalized}
    return expected


class CanonicalReconciler:
    def __init__(self,store):
        self.store = store

    def check_record(self,mapped_record,*,expected_actor_id=None):
        if expected_actor_id is not None and (not isinstance(expected_actor_id,str) or not expected_actor_id):
            raise ReconciliationError("INVALID_EXPECTED_ACTOR")
        if not isinstance(mapped_record,dict) or not isinstance(mapped_record.get("facts"),list) or not 1 <= len(mapped_record["facts"]) <= MAX_LEAVES:
            raise ReconciliationError("SOURCE_ATOM_LIMIT")
        if len(json_text(mapped_record)) > MAX_PREPARED_BYTES:
            raise ReconciliationError("PREPARED_RECORD_LIMIT")
        prepared = prepare_canonical_record(mapped_record)
        expected_count = sum(1+len(fact["raw"].get("flags",{})) for fact in prepared["facts"])
        if expected_count > MAX_OBSERVATIONS or len(prepared["containers"]) > MAX_CONTAINERS:
            raise ReconciliationError("PREPARED_RECORD_LIMIT")
        errors,evidence = Counter(),hashlib.sha256()
        evidence.update(json_text({"operation_id":str(prepared["operation_id"]),"prepared_hash":prepared["prepared_hash"]}).encode("ascii"))
        actual_count,container_count = 0,0
        with self.store.connection() as c:
            c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            self.store._deployment(c)
            operation = c.execute("SELECT p.*,e.entity_type,e.version AS current_entity_version FROM operations p JOIN entities e ON e.owner_id=p.owner_id WHERE p.operation_id=%s",(prepared["operation_id"],)).fetchone()
            if operation is None:
                errors["missing_operation"] += 1
            else:
                expected_op = {"source_id_json":json_text(prepared["source"]),"source_record_id_json":json_text(prepared["external"]),
                    "source_version_json":json_text(prepared["source_version"]),"record_hash":prepared["record_hash"],
                    "adapter_version_json":json_text(prepared["adapter"]),"normalizer_version_json":json_text(prepared["normalizer_version"]),
                    "prepared_hash":prepared["prepared_hash"],"entity_type":prepared["entity_type"]}
                if expected_actor_id is not None:
                    expected_op["actor_id_json"] = json_text(expected_actor_id)
                for key,value in expected_op.items():
                    if operation[key] != value: errors["operation_"+key+"_mismatch"] += 1
                owner = operation["owner_id"]
                if not isinstance(decode(operation["actor_id_json"]),str) or not decode(operation["actor_id_json"]):
                    errors["operation_actor_invalid"] += 1
                previous_op = c.execute("SELECT entity_version FROM operations WHERE owner_id=%s AND entity_version<%s ORDER BY entity_version DESC LIMIT 1",(owner,operation["entity_version"])).fetchone()
                if operation["entity_version"] != (previous_op["entity_version"] if previous_op else 0)+1:
                    errors["operation_version_gap"] += 1
                latest_op = c.execute("SELECT entity_version FROM operations WHERE owner_id=%s ORDER BY entity_version DESC LIMIT 1",(owner,)).fetchone()
                if latest_op["entity_version"] != operation["current_entity_version"]:
                    errors["entity_version_mismatch"] += 1
                for key,kind,identity_json in prepared["identities"]:
                    actual_identity = c.execute("SELECT owner_id,kind,identity_json FROM identity_keys WHERE key_hash=%s",(key,)).fetchone()
                    if actual_identity != {"owner_id":owner,"kind":kind,"identity_json":identity_json}:
                        errors["identity_registry_mismatch"] += 1
                item_expected,keys = {},set()
                for fact in prepared["facts"]:
                    raw = fact["raw"]
                    item = identifier("item",[str(owner),fact["kind"],raw["item_key"]])
                    item_expected[item] = {"kind":fact["kind"],"item_key_hash":digest(raw["item_key"]),"item_key_json":json_text(raw["item_key"])}
                    path = digest(raw["target_path"])
                    keys.add((item,path,"value"))
                    keys.update((item,path,"flag:"+flag) for flag in raw.get("flags",{}))
                previous = _states(c,owner,keys,operation["entity_version"]-1)
                item_before = _items_before(c,owner,item_expected,operation["entity_version"]-1)
                expected = _expected_observations(prepared,operation,previous,item_before)
                actual = _rows(c,"SELECT * FROM observations WHERE operation_id=%s ORDER BY owner_id,operation_sequence,observation_id",
                    (prepared["operation_id"],),maximum=MAX_OBSERVATIONS,budget=MAX_DESTINATION_BYTES,evidence=evidence)
                actual_count = len(actual)
                by_id = {}
                for row in actual:
                    if row["observation_id"] in by_id: errors["duplicate_observation"] += 1
                    by_id[row["observation_id"]] = row
                for expected_row in expected:
                    actual_row = by_id.pop(expected_row["observation_id"],None)
                    if actual_row is None:
                        errors["missing_observation"] += 1
                        continue
                    for column,value in expected_row.items():
                        if actual_row[column] != value: errors["observation_"+column+"_mismatch"] += 1
                errors["extra_observation"] += len(by_id)
                containers = _rows(c,"SELECT * FROM source_containers WHERE operation_id=%s ORDER BY path_hash",(prepared["operation_id"],),
                    maximum=MAX_CONTAINERS,budget=MAX_DESTINATION_BYTES,evidence=evidence)
                container_count = len(containers)
                containers_by_path = {row["path_hash"]:row for row in containers}
                if len(containers_by_path) != len(containers): errors["duplicate_container"] += 1
                for container in prepared["containers"]:
                    found = containers_by_path.pop(digest(container["source_path"]),None)
                    if found is None:
                        errors["missing_container"] += 1
                        continue
                    expected_container = {"operation_id":prepared["operation_id"],"path_hash":digest(container["source_path"]),
                        "path_json":json_text(container["source_path"]),"container_type":container["type"],"length":container["length"],
                        "metadata_json":json_text({k:v for k,v in container.items() if k not in {"source_path","type","length"}})}
                    if found != expected_container: errors["container_mismatch"] += 1
                errors["extra_container"] += len(containers_by_path)
                # Later legitimate operations do not invalidate an older source
                # readback. Compare the current cache to latest applied history.
                current_states = _states(c,owner,keys,operation["current_entity_version"])
                current_item_versions = _items_before(c,owner,item_expected,operation["current_entity_version"])
                for item_id,expected_item in item_expected.items():
                    item = c.execute("SELECT * FROM items WHERE owner_id=%s AND item_id=%s",(owner,item_id)).fetchone()
                    if item is None or any(item[column] != value for column,value in expected_item.items()):
                        errors["item_identity_mismatch"] += 1
                    elif item["version"] != current_item_versions[item_id] or item["created_version"] > operation["entity_version"]:
                        errors["item_version_mismatch"] += 1
                for key,state in current_states.items():
                    current = c.execute("SELECT * FROM field_state WHERE owner_id=%s AND item_id=%s AND target_path_hash=%s AND dimension=%s",(owner,*key)).fetchone()
                    if state is None:
                        if current is not None: errors["unexpected_projection"] += 1
                        continue
                    compared = ("observation_id","value_json","metadata_json","status","effective_at","received_at","source_id_json","binding_hash")
                    if current is None or any(current[column] != state[column] for column in compared):
                        errors["projection_mismatch"] += 1
        errors = {key:value for key,value in sorted(errors.items()) if value}
        return {"passed":not errors,"divergences":sum(errors.values()),"discrepancy_counts":errors,
            "atoms_expected":len(prepared["facts"]),"observations_expected":expected_count,"observations_checked":actual_count,
            "containers_expected":len(prepared["containers"]),"containers_checked":container_count,
            "evidence_sha256":evidence.hexdigest()}


def reconcile_reader(reader,store,*,expected_records,expected_actor_id=None,max_records=None,
                     cancelled=lambda:False,progress=None):
    if type(expected_records) is not int or expected_records < 0:
        raise ReconciliationError("INVALID_EXPECTED_RECORD_COUNT")
    if max_records is not None and (type(max_records) is not int or max_records < 0):
        raise ReconciliationError("INVALID_RECONCILIATION_LIMIT")
    started = datetime.now(timezone.utc).isoformat()
    errors,counts,evidence = Counter(),Counter(),hashlib.sha256()
    reason,exhausted = None,False
    identity_hash = digest(reader.identity)
    info = store.deployment_info()
    reconciler = CanonicalReconciler(store)
    try:
        if max_records == 0:
            reason = "record_limit"
        else:
            for page in reader.pages(checkpoint=None):
                if exhausted: raise ReconciliationError("SOURCE_PAGE_AFTER_EOF")
                if cancelled():
                    reason = "cancelled"
                    break
                if not isinstance(page.records,list) or len(page.records) > 1000:
                    raise ReconciliationError("SOURCE_PAGE_LIMIT")
                if page.checkpoint.get("seen") != counts["records_checked"]+len(page.records):
                    raise ReconciliationError("SOURCE_PAGE_COUNT_MISMATCH")
                for row in page.records:
                    if cancelled():
                        reason = "cancelled"
                        break
                    if max_records is not None and counts["records_checked"] >= max_records:
                        reason = "record_limit"
                        break
                    mapped = map_record(row["source_id"],row["external_id"],row["record"],source_version=row.get("source_version"))
                    if mapped.get("coverage",{}).get("passed") is not True:
                        raise ReconciliationError("SOURCE_ATOM_COVERAGE_FAILED")
                    result = reconciler.check_record(mapped,expected_actor_id=expected_actor_id)
                    counts["records_checked"] += 1
                    counts["records_matched"] += int(result["passed"])
                    for name in ("atoms_expected","observations_expected","observations_checked","containers_expected","containers_checked"):
                        counts[name] += result[name]
                    errors.update(result["discrepancy_counts"])
                    evidence.update(bytes.fromhex(result["evidence_sha256"]))
                    if progress is not None: progress(counts["records_checked"],sum(errors.values()))
                if reason: break
                exhausted = page.complete is True
    except Exception as exc:
        # Do not serialize exceptions from SQL, source values, parsing or HTTP.
        code = str(exc) if isinstance(exc,ReconciliationError) else "RECONCILIATION_READ_ERROR"
        errors[code] += 1
        reason = "read_error"
    if digest(reader.identity) != identity_hash:
        errors["source_identity_changed"] += 1
    if exhausted and counts["records_checked"] != expected_records:
        errors["source_record_count_mismatch"] += 1
    coverage_complete = exhausted and counts["records_checked"] == expected_records and reason is None
    divergences = sum(errors.values())
    passed = coverage_complete and not divergences
    return {"proof_version":PROOF_VERSION,"state":"verified" if passed else "failed" if divergences else "partial",
        "complete":passed,"passed":passed,"coverage_complete":coverage_complete,"reason":reason,
        "records_checked":counts["records_checked"],"records_matched":counts["records_matched"],"expected_records":expected_records,
        "divergences":divergences,"discrepancy_counts":dict(sorted(errors.items())),
        **{name:counts[name] for name in ("atoms_expected","observations_expected","observations_checked","containers_expected","containers_checked")},
        "evidence_sha256":evidence.hexdigest(),"source_identity_sha256":identity_hash,
        "destination_deployment_id":info["deployment_id"],"adapter_version":ADAPTER_VERSION,"normalizer_version":NORMALIZER_VERSION,
        "actor_scope":"explicit_expected_actor" if expected_actor_id is not None else "internal_consistency",
        "snapshot_scope":"per_operation_repeatable_read","started_at":started,"finished_at":datetime.now(timezone.utc).isoformat()}

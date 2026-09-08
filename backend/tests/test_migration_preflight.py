"""Only synthetic metadata: no database, filesystem fixture or real records."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json

import pytest

from bigbase import migration_preflight as preflight


NOW = datetime(2026, 9, 8, 20, 0, tzinfo=timezone.utc)
DEPLOYMENT = "baad4110-2200-4200-8200-100000000001"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(preflight, "_utcnow", lambda: NOW)


@pytest.fixture
def inputs():
    mappings = {name: {"mappings": {"properties": {"synthetic_field": {"type": "keyword"}}}}
                for name in ("pessoas", "pessoas_serasa")}
    counts = {"pessoas": 3_000_000, "pessoas_serasa": 2_000_000, ".security-7": 0}
    manifest = {"snapshot": "synthetic-metadata-backup", "requested_at": "2026-09-08T10:00:00Z",
                "cluster": {"cluster_uuid": "synthetic-origin"}, "mappings": mappings,
                "indices_before": [{"index": name, "uuid": "original-" + name, "docs.count": str(count)}
                                   for name, count in counts.items()]}
    snapshot = {"snapshot": manifest["snapshot"], "uuid": "synthetic-snapshot-id", "state": "SUCCESS",
                "shards": {"total": 3, "successful": 3, "failed": 0}, "failures": [],
                "indices": list(counts), "end_time": "2026-09-08T11:00:00Z"}
    restore = {"status": "SUCCESS", "phase": "completed", "snapshot": manifest["snapshot"],
               "snapshot_uuid": snapshot["uuid"], "source_cluster_uuid": "synthetic-origin",
               "restore_cluster_uuid": "synthetic-isolated-restore", "restore_version": "9.5.3",
               "repository_readonly": True, "global_state_restored": True, "feature_states": ["security"],
               "started_at": "2026-09-08T12:00:00Z", "completed_at": "2026-09-08T13:00:00Z",
               "configuration_archives": [{"name": "synthetic-config.tar.gz", "sha256": "a" * 64,
                    "checksum_verified": True, "archive_read_verified": True, "required_paths_verified": True}],
               "indices": [{"source_index": name, "restored_index": "restored_" + name, "count": count,
                    "recovery_done": True, "query_test_passed": True, "failed_shards": 0,
                    "mapping_sha256": digest(mappings[name]["mappings"]) if name in mappings else "b" * 64}
                   for name, count in counts.items()]}
    destination = {"deployment_id": DEPLOYMENT, "expected_deployment_id": DEPLOYMENT,
                   "environment": "staging", "schema": "canonical", "database": "synthetic_metadata_only",
                   "schema_version": 1, "server_version_num": 180006, "dedicated_for_migration": True,
                   "created_at": "2026-09-08T14:00:00Z", "observed_at": "2026-09-08T19:00:00Z",
                   "capacity": {"volumes": [{"id": "data", "free_bytes": 100_000_000},
                                             {"id": "replica", "free_bytes": 100_000_000},
                                             {"id": "backup", "free_bytes": 100_000_000}]}}
    source = {"mode": "full", "snapshot": snapshot, "cluster_uuid": "synthetic-origin",
              "expected_cluster_uuid": "synthetic-origin", "observed_at": "2026-09-08T19:00:00Z",
              "indices": [{"source_id": name, "index": name, "index_uuid": "original-" + name,
                           "expected_index_uuid": "original-" + name, "count": count,
                           "mapping_sha256": digest(mappings[name]["mappings"]), "read_only": True}
                          for name, count in counts.items() if name in mappings]}
    pilot = {"status": "SUCCESS", "snapshot_uuid": snapshot["uuid"], "deployment_id": DEPLOYMENT,
             "source_ids": ["pessoas", "pessoas_serasa"], "measured_records": 1_000_000,
             "projected_records": 5_000_000, "representative": True, "reconciled": True,
             "lossless_coverage": True, "failed_records": 0, "unpreserved_fields": 0,
             "measured_at": "2026-09-08T18:00:00Z",
             "metrics": {"unique_entities": 900_000, "source_intersection_records": 100_000,
                         "items": 8_000_000, "observations": 10_000_000, "nested_documents": 8_000_000,
                         "elapsed_seconds": 1000, "search_rebuild_seconds": 500},
             "components": [{"kind": kind, "volume_id": kind if kind in {"replica", "backup"} else "data",
                             "measured_bytes": 1_000, "copies": 1} for kind in sorted(preflight.CAPACITY_COMPONENTS)]}
    return {"backup_manifest": manifest, "restore_report": restore, "destination_info": destination,
            "source_info": source, "pilot": pilot}


def assess(inputs):
    return preflight.assess_migration(**inputs)


def test_complete_metadata_allows_full_only_not_cutover(inputs):
    report = assess(inputs)
    assert report["ready"] is True
    assert report["ready_for_full_import"] is True
    assert report["phase"] == "full"
    assert report["production_cutover_approved"] is False
    assert report["development_allowed"] is True
    assert report["blockers"] == []
    assert all(item["status"] == "passed" for item in report["checks"])
    assert sum(item["base_bytes"] for item in report["capacity"]) == 30_000
    assert sum(item["required_free_bytes"] for item in report["capacity"]) == 45_000


@pytest.mark.parametrize("missing,code", [
    ("backup_manifest", "BACKUP_MANIFEST_COMPLETE"), ("restore_report", "RESTORE_SUCCESS"),
    ("destination_info", "DESTINATION_DEFINED"), ("pilot", "PILOT_SUCCESS"),
    ("source_info", "SOURCE_READONLY"),
])
def test_missing_proof_blocks_migration_not_development(inputs, missing, code):
    inputs[missing] = None
    report = assess(inputs)
    assert report["ready"] is False
    assert report["development_allowed"] is True
    assert code in report["blockers"]


def mutate(inputs, path, value):
    target = inputs
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value


@pytest.mark.parametrize("path,value,code", [
    (("restore_report", "snapshot_uuid"), "different", "SNAPSHOT_IDENTITY_MATCH"),
    (("restore_report", "source_cluster_uuid"), "different", "RESTORE_CLUSTER_IDENTITY"),
    (("restore_report", "restore_cluster_uuid"), "synthetic-origin", "RESTORE_CLUSTER_IDENTITY"),
    (("restore_report", "restore_version"), "9.5.4", "RESTORE_VERSION_VERIFIED"),
    (("restore_report", "repository_readonly"), "true", "RESTORE_REPOSITORY_READONLY"),
    (("restore_report", "indices", 0, "count"), 2_999_999, "RESTORE_COUNTS_MATCH"),
    (("restore_report", "indices", 2, "count"), False, "RESTORE_COUNTS_MATCH"),
    (("restore_report", "indices", 0, "failed_shards"), 1, "RESTORE_READ_VERIFIED"),
    (("restore_report", "indices", 0, "mapping_sha256"), "a" * 64, "RESTORE_MAPPINGS_MATCH"),
    (("restore_report", "configuration_archives", 0, "checksum_verified"), 1, "RESTORE_ARCHIVES_VERIFIED"),
    (("restore_report", "configuration_archives"), [], "RESTORE_ARCHIVES_VERIFIED"),
    (("restore_report", "global_state_restored"), False, "RESTORE_GLOBAL_SECURITY_VERIFIED"),
    (("restore_report", "feature_states"), "security", "RESTORE_GLOBAL_SECURITY_VERIFIED"),
    (("source_info", "snapshot", "state"), "PARTIAL", "SNAPSHOT_SUCCESS"),
    (("source_info", "snapshot", "failures"), [{"reason": "redacted"}], "SNAPSHOT_SUCCESS"),
    (("source_info", "snapshot", "shards", "failed"), False, "SNAPSHOT_SUCCESS"),
    (("source_info", "snapshot", "indices"), ["pessoas"], "SNAPSHOT_INDICES_COMPLETE"),
    (("destination_info", "environment"), "synthetic", "DESTINATION_REAL_ENVIRONMENT"),
    (("destination_info", "test_fixture"), True, "DESTINATION_REAL_ENVIRONMENT"),
    (("destination_info", "synthetic_only"), True, "DESTINATION_REAL_ENVIRONMENT"),
    (("destination_info", "dedicated_for_migration"), "yes", "DESTINATION_REAL_ENVIRONMENT"),
    (("destination_info", "expected_deployment_id"), "baad4110-2200-4200-8200-100000000002", "DESTINATION_IDENTITY_PINNED"),
    (("destination_info", "server_version_num"), 170010, "DESTINATION_POSTGRESQL_18"),
    (("destination_info", "schema_version"), True, "DESTINATION_POSTGRESQL_18"),
    (("source_info", "expected_cluster_uuid"), "different", "SOURCE_CLUSTER_PINNED"),
    (("source_info", "indices", 0, "expected_index_uuid"), "different", "SOURCE_INDICES_PINNED"),
    (("source_info", "indices", 0, "count"), 3_000_001, "SOURCE_COUNTS_MATCH"),
    (("source_info", "indices", 0, "read_only"), False, "SOURCE_READONLY"),
    (("source_info", "indices", 0, "read_only"), "true", "SOURCE_READONLY"),
    (("pilot", "measured_records"), 999_999, "PILOT_REPRESENTATIVE_VOLUME"),
    (("pilot", "projected_records"), 1_000_000, "PILOT_REPRESENTATIVE_VOLUME"),
    (("pilot", "representative"), 1, "PILOT_REPRESENTATIVE_VOLUME"),
    (("pilot", "failed_records"), 1, "PILOT_LOSSLESS_RECONCILIATION"),
    (("pilot", "unpreserved_fields"), 1, "PILOT_LOSSLESS_RECONCILIATION"),
    (("pilot", "lossless_coverage"), None, "PILOT_LOSSLESS_RECONCILIATION"),
    (("pilot", "metrics"), {}, "PILOT_METRICS_MEASURED"),
    (("pilot", "components"), [], "PILOT_CAPACITY_COMPONENTS_MEASURED"),
    (("pilot", "components", 0, "measured_bytes"), 1.5, "PILOT_CAPACITY_COMPONENTS_MEASURED"),
    (("pilot", "components", 0, "copies"), False, "PILOT_CAPACITY_COMPONENTS_MEASURED"),
    (("destination_info", "capacity", "volumes", 0, "free_bytes"), 29_999, "CAPACITY_WITH_50_PERCENT_MARGIN"),
])
def test_evidence_mismatch_or_tampered_type_blocks(inputs, path, value, code):
    mutate(inputs, path, value)
    report = assess(inputs)
    assert report["ready"] is False
    assert code in report["blockers"]


@pytest.mark.parametrize("field,value", [
    ("completed_at", "2026-09-08T21:00:00Z"), ("completed_at", "2026-09-08T13:00:00"),
    ("completed_at", "2026-09-08T10:00:00Z"), ("started_at", "2026-09-08T14:00:00Z"),
    ("started_at", False),
])
def test_restore_chronology_requires_timezone_and_not_future(inputs, field, value):
    inputs["restore_report"][field] = value
    assert "RESTORE_DATES_VALID" in assess(inputs)["blockers"]


def test_supplied_snapshot_date_representations_must_agree(inputs):
    snapshot = inputs["source_info"]["snapshot"]
    snapshot["end_time_in_millis"] = 1
    assert "RESTORE_DATES_VALID" in assess(inputs)["blockers"]
    snapshot["end_time_in_millis"] = int(datetime(2026, 9, 8, 11, tzinfo=timezone.utc).timestamp() * 1000)
    assert assess(inputs)["ready"] is True
    del snapshot["end_time"]
    assert assess(inputs)["ready"] is True


@pytest.mark.parametrize("field", ["backup_manifest", "restore_report", "destination_info", "pilot", "source_info"])
@pytest.mark.parametrize("bad", [[], [None], "secret://never-echo", False, 10, 1.5])
def test_invalid_top_level_objects_never_throw_or_echo(inputs, field, bad):
    inputs[field] = bad
    report = assess(inputs)
    assert report["ready"] is False
    assert "secret://never-echo" not in json.dumps(report)


def test_no_mutation_or_arbitrary_metadata_in_report(inputs):
    inputs["destination_info"]["dsn"] = "postgres://SECRET"
    inputs["pilot"]["note"] = {"personal_record": "NEVER_ECHO"}
    before = deepcopy(inputs)
    result = assess(inputs)
    assert inputs == before
    assert "SECRET" not in json.dumps(result)
    assert "NEVER_ECHO" not in json.dumps(result)


def test_pit_does_not_replace_write_block(inputs):
    inputs["source_info"]["pit"] = {"opened": True, "consistent": True}
    inputs["source_info"]["indices"][0]["read_only"] = False
    assert "SOURCE_READONLY" in assess(inputs)["blockers"]


def test_restored_source_requires_verified_cluster_and_snapshot(inputs):
    source = inputs["source_info"]
    source["cluster_uuid"] = source["expected_cluster_uuid"] = inputs["restore_report"]["restore_cluster_uuid"]
    source["restored_from_snapshot_uuid"] = source["snapshot"]["uuid"]
    for item in source["indices"]:
        item["index"] = "restored_" + item["source_id"]
        item["index_uuid"] = item["expected_index_uuid"] = "new-" + item["source_id"]
    assert assess(inputs)["ready"] is True
    source["restored_from_snapshot_uuid"] = "different"
    assert "SOURCE_BACKUP_ASSOCIATION" in assess(inputs)["blockers"]


def test_duplicate_or_internal_source_cannot_expand_selection(inputs):
    inputs["source_info"]["indices"].append(deepcopy(inputs["source_info"]["indices"][0]))
    assert "SOURCE_INDICES_PINNED" in assess(inputs)["blockers"]
    inputs["source_info"]["indices"][-1]["source_id"] = ".security-7"
    assert "SOURCE_INDICES_PINNED" in assess(inputs)["blockers"]


def test_pending_preserved_fields_do_not_block_migration(inputs):
    inputs["pilot"]["unmapped_fields"] = 500
    inputs["pilot"]["pending_fields"] = 500
    assert assess(inputs)["ready"] is True


def test_snapshot_size_is_not_capacity_measurement(inputs):
    inputs["source_info"]["snapshot"]["size_in_bytes"] = 230_696_946_889
    inputs["pilot"].pop("components")
    assert "PILOT_CAPACITY_COMPONENTS_MEASURED" in assess(inputs)["blockers"]


def test_separate_volumes_and_replicas_are_summed_not_reused(inputs):
    inputs["pilot"]["components"][-1]["copies"] = 2
    report = assess(inputs)
    assert report["ready"] is True
    assert sum(row["base_bytes"] for row in report["capacity"]) == 35_000
    inputs["destination_info"]["capacity"]["volumes"][1]["free_bytes"] = 7_499
    assert "CAPACITY_WITH_50_PERCENT_MARGIN" in assess(inputs)["blockers"]


def pilot_plan(inputs):
    inputs["source_info"]["mode"] = "pilot"
    inputs["pilot"] = {"status": "PLANNED", "record_limit": 10_000, "max_batch_records": 100,
                       "max_duration_seconds": 3600, "volume_budgets": [{"volume_id": "data", "max_bytes": 10_001}]}


def test_first_bounded_pilot_does_not_need_a_previous_pilot(inputs):
    pilot_plan(inputs)
    report = assess(inputs)
    assert report["ready"] is True
    assert report["phase"] == "pilot"
    assert report["ready_for_full_import"] is False
    assert report["capacity"][0]["required_free_bytes"] == 15_002


@pytest.mark.parametrize("field,value,code", [
    ("record_limit", 1_000_001, "PILOT_EXECUTION_BOUNDED"),
    ("record_limit", False, "PILOT_EXECUTION_BOUNDED"),
    ("max_duration_seconds", None, "PILOT_EXECUTION_BOUNDED"),
    ("max_batch_records", 1001, "PILOT_EXECUTION_BOUNDED"),
    ("volume_budgets", [], "PILOT_CAPACITY_BUDGET_DEFINED"),
])
def test_pilot_scope_cannot_authorize_unbounded_import(inputs, field, value, code):
    pilot_plan(inputs)
    inputs["pilot"][field] = value
    assert code in assess(inputs)["blockers"]


def test_real_flag_cannot_override_synthetic_environment(inputs):
    pilot_plan(inputs)
    inputs["destination_info"]["environment"] = "synthetic"
    inputs["source_info"]["real"] = True
    assert "DESTINATION_REAL_ENVIRONMENT" in assess(inputs)["blockers"]


@pytest.mark.parametrize("bad", [[None], {"secret": "NEVER_ECHO"}])
def test_nested_type_tampering_is_a_report_not_an_exception(inputs, bad):
    def leaves(value, prefix=()):
        if type(value) is dict:
            for key, child in value.items():
                yield from leaves(child, prefix + (key,))
        elif type(value) is list:
            for index, child in enumerate(value):
                yield from leaves(child, prefix + (index,))
        else:
            yield prefix
    for path in leaves(inputs):
        changed = deepcopy(inputs)
        mutate(changed, path, bad)
        result = assess(changed)
        assert type(result["ready"]) is bool
        assert "NEVER_ECHO" not in json.dumps(result)

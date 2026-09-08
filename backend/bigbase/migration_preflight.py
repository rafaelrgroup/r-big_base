"""Read-only assessment of metadata for a bounded pilot or a full migration.

This module has no filesystem, database or network access. The caller collects
the evidence from the pinned deployments and enforces the accepted scope. A
successful assessment is not a traffic cutover or production load certificate.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from uuid import UUID


MIN_FULL_PILOT_RECORDS = 1_000_000
MAX_PILOT_RECORDS = 1_000_000
CAPACITY_COMPONENTS = frozenset({"postgres", "search", "wal", "temporary", "backup", "replica"})
BUSINESS_SOURCES = frozenset({"pessoas", "pessoas_serasa"})


def _utcnow():
    return datetime.now(timezone.utc)


def _obj(value):
    return value if type(value) is dict else {}


def _list(value):
    return value if type(value) is list else []


def _text(value):
    return type(value) is str and bool(value) and len(value) <= 512


def _integer(value, minimum=0):
    return type(value) is int and minimum <= value <= 2**63 - 1


def _count(value):
    # Elasticsearch's CAT response supplies canonical decimal strings. No other
    # coercion is accepted: bool, 1.0, signs and whitespace are not counts.
    if _integer(value):
        return value
    if type(value) is str and re.fullmatch(r"0|[1-9][0-9]{0,18}", value):
        parsed = int(value)
        return parsed if _integer(parsed) else None
    return None


def _sha(value):
    return type(value) is str and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _uuid(value):
    if type(value) is not str:
        return False
    try:
        return str(UUID(value)) == value.lower() and UUID(value).int != 0
    except (ValueError, AttributeError):
        return False


def _date(value):
    if type(value) is not str:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _digest(value):
    try:
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                        ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    except (ValueError, TypeError, RecursionError, UnicodeError):
        return None


def _string_set(value):
    if type(value) is not list or not value or not all(_text(item) for item in value):
        return None
    unique = set(value)
    return unique if len(unique) == len(value) else None


def assess_migration(backup_manifest, restore_report, destination_info, pilot=None, source_info=None):
    """Return safe operational codes; never mutate inputs or copy arbitrary data.

    ``source_info.mode`` defaults to ``full``. Explicit ``pilot`` assesses only
    a bounded plan and never sets ``ready_for_full_import``. Datetimes must have
    a timezone and cannot be over five minutes ahead of the assessment clock.
    There is deliberately no invented expiry date for a valid restored backup.
    """
    now = _utcnow()
    latest = now + timedelta(minutes=5)
    checks = []

    def check(code, passed):
        passed = passed is True
        checks.append({"code": code, "status": "passed" if passed else "blocked"})
        return passed

    manifest, restore = _obj(backup_manifest), _obj(restore_report)
    destination, source, measured = _obj(destination_info), _obj(source_info), _obj(pilot)
    mode = source.get("mode", "full")
    if not check("MIGRATION_MODE_VALID", type(mode) is str and mode in {"full", "pilot"}):
        mode = "invalid"

    cluster = _obj(manifest.get("cluster"))
    baseline = {}
    before = _list(manifest.get("indices_before"))
    valid_manifest = bool(before) and _text(manifest.get("snapshot")) and _text(cluster.get("cluster_uuid"))
    for raw in before:
        entry = _obj(raw)
        name, count = entry.get("index"), _count(entry.get("docs.count"))
        if not _text(name) or name in baseline or count is None or not _text(entry.get("uuid")):
            valid_manifest = False
            continue
        baseline[name] = {"count": count, "uuid": entry["uuid"]}
    mappings = _obj(manifest.get("mappings"))
    check("BACKUP_MANIFEST_COMPLETE", bool(valid_manifest) and BUSINESS_SOURCES.issubset(baseline)
          and all(type(_obj(mappings.get(name)).get("mappings")) is dict for name in BUSINESS_SOURCES))

    snapshot = _obj(source.get("snapshot"))
    shards = _obj(snapshot.get("shards"))
    snapshot_indices = _string_set(snapshot.get("indices"))
    check("SNAPSHOT_SUCCESS", snapshot.get("state") == "SUCCESS" and snapshot.get("failures") == []
          and _integer(shards.get("total"), 1) and _integer(shards.get("failed")) and shards.get("failed") == 0
          and _integer(shards.get("successful"), 1) and shards.get("successful") == shards.get("total"))
    check("SNAPSHOT_IDENTITY_MATCH", _text(snapshot.get("uuid"))
          and snapshot.get("snapshot") == manifest.get("snapshot") == restore.get("snapshot")
          and snapshot.get("uuid") == restore.get("snapshot_uuid"))
    check("SNAPSHOT_INDICES_COMPLETE", snapshot_indices is not None and snapshot_indices == set(baseline))
    check("RESTORE_SUCCESS", restore.get("status") == "SUCCESS" and restore.get("phase") == "completed")
    check("RESTORE_CLUSTER_IDENTITY", _text(restore.get("source_cluster_uuid"))
          and restore.get("source_cluster_uuid") == cluster.get("cluster_uuid")
          and _text(restore.get("restore_cluster_uuid"))
          and restore.get("restore_cluster_uuid") != restore.get("source_cluster_uuid"))
    check("RESTORE_VERSION_VERIFIED", restore.get("restore_version") == "9.5.3")
    check("RESTORE_REPOSITORY_READONLY", restore.get("repository_readonly") is True)
    features = _string_set(restore.get("feature_states"))
    check("RESTORE_GLOBAL_SECURITY_VERIFIED", restore.get("global_state_restored") is True
          and features is not None and "security" in features)

    archives = _list(restore.get("configuration_archives"))
    names = [_obj(item).get("name") for item in archives]
    check("RESTORE_ARCHIVES_VERIFIED", bool(archives) and all(_text(name) for name in names)
          and len(set(name for name in names if type(name) is str)) == len(names)
          and all(_sha(_obj(item).get("sha256")) and all(_obj(item).get(flag) is True for flag in
                  ("checksum_verified", "archive_read_verified", "required_paths_verified")) for item in archives))

    restored = {}
    index_proofs_valid = True
    for raw in _list(restore.get("indices")):
        entry = _obj(raw)
        name = entry.get("source_index")
        if not _text(name) or name in restored:
            index_proofs_valid = False
            continue
        restored[name] = entry
    check("RESTORE_INDICES_COMPLETE", index_proofs_valid and bool(restored) and set(restored) == set(baseline))
    check("RESTORE_COUNTS_MATCH", bool(baseline) and all(
        _integer(_obj(restored.get(name)).get("count")) and restored[name]["count"] == item["count"]
        for name, item in baseline.items()))
    check("RESTORE_READ_VERIFIED", bool(restored) and all(
        item.get("recovery_done") is True and item.get("query_test_passed") is True
        and _integer(item.get("failed_shards")) and item.get("failed_shards") == 0
        and _text(item.get("restored_index")) for item in restored.values()))
    check("RESTORE_MAPPINGS_MATCH", bool(restored) and all(
        _sha(item.get("mapping_sha256")) and (name not in mappings or (
            type(_obj(mappings[name]).get("mappings")) is dict
            and item["mapping_sha256"] == _digest(mappings[name]["mappings"])))
        for name, item in restored.items()))

    ended = _date(snapshot.get("end_time"))
    millis = snapshot.get("end_time_in_millis")
    if ended is None and _integer(millis):
        try:
            ended = datetime.fromtimestamp(millis / 1000, tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            pass
    # Two supplied timestamp representations must describe the same instant.
    times_consistent = (millis is None or (_integer(millis) and ended is not None
                                         and abs(ended.timestamp() * 1000 - millis) < 1))
    requested = _date(manifest.get("requested_at"))
    started, completed = _date(restore.get("started_at")), _date(restore.get("completed_at"))
    check("RESTORE_DATES_VALID", times_consistent and all(value is not None for value in
          (requested, ended, started, completed)) and requested <= ended <= started <= completed <= latest)

    check("DESTINATION_DEFINED", type(destination_info) is dict and bool(destination))
    check("DESTINATION_IDENTITY_PINNED", _uuid(destination.get("deployment_id"))
          and destination.get("deployment_id") == destination.get("expected_deployment_id")
          and _text(destination.get("schema")) and _text(destination.get("database")))
    check("DESTINATION_REAL_ENVIRONMENT", type(destination.get("environment")) is str
          and destination.get("environment") in {"staging", "production"}
          and destination.get("synthetic_only", False) is False
          and destination.get("test_fixture", False) is False
          and destination.get("dedicated_for_migration") is True)
    check("DESTINATION_POSTGRESQL_18", _integer(destination.get("server_version_num"))
          and 180000 <= destination.get("server_version_num", 0) < 190000
          and type(destination.get("schema_version")) is int and destination.get("schema_version") == 1)
    observed = _date(destination.get("observed_at"))
    created = _date(destination.get("created_at"))
    check("DESTINATION_DATES_VALID", observed is not None and created is not None
          and created <= observed <= latest)

    check("SOURCE_CLUSTER_PINNED", _text(source.get("cluster_uuid"))
          and source.get("cluster_uuid") == source.get("expected_cluster_uuid"))
    original = source.get("cluster_uuid") == cluster.get("cluster_uuid") and _text(source.get("cluster_uuid"))
    isolated = (source.get("cluster_uuid") == restore.get("restore_cluster_uuid")
                and _text(source.get("cluster_uuid"))
                and source.get("restored_from_snapshot_uuid") == snapshot.get("uuid")
                and _text(snapshot.get("uuid")))
    check("SOURCE_BACKUP_ASSOCIATION", original or isolated)
    observed_source = _date(source.get("observed_at"))
    check("SOURCE_DATES_VALID", observed_source is not None and completed is not None
          and completed <= observed_source <= latest)
    selected, source_entries = {}, _list(source.get("indices"))
    identities_valid = bool(source_entries)
    for raw in source_entries:
        entry = _obj(raw)
        name = entry.get("source_id")
        if type(name) is not str or name not in BUSINESS_SOURCES or name in selected:
            identities_valid = False
            continue
        selected[name] = entry
        proof = _obj(restored.get(name))
        identities_valid = identities_valid and (
            _text(entry.get("index_uuid")) and entry.get("index_uuid") == entry.get("expected_index_uuid")
            and ((original and entry.get("index") == name and entry.get("index_uuid") == _obj(baseline.get(name)).get("uuid"))
                 or (isolated and entry.get("index") == proof.get("restored_index"))))
    check("SOURCE_INDICES_PINNED", bool(identities_valid))
    check("SOURCE_COUNTS_MATCH", bool(selected) and all(_integer(item.get("count"))
          and item["count"] == _obj(baseline.get(name)).get("count") for name, item in selected.items()))
    check("SOURCE_MAPPINGS_MATCH", bool(selected) and all(_sha(item.get("mapping_sha256"))
          and item["mapping_sha256"] == _obj(restored.get(name)).get("mapping_sha256") for name, item in selected.items()))
    check("SOURCE_READONLY", bool(selected) and all(item.get("read_only") is True for item in selected.values()))

    volumes = {}
    volume_entries = _list(_obj(destination.get("capacity")).get("volumes"))
    volumes_valid = bool(volume_entries)
    for raw in volume_entries:
        volume = _obj(raw)
        identifier, free = volume.get("id"), volume.get("free_bytes")
        if not _text(identifier) or identifier in volumes or not _integer(free):
            volumes_valid = False
            continue
        volumes[identifier] = free
    check("DESTINATION_CAPACITY_OBSERVED", bool(volumes_valid))
    capacity_totals = {}
    numeric_expected = [item.get("count") for item in selected.values()]
    projected = sum(numeric_expected) if numeric_expected and all(_integer(n) for n in numeric_expected) else None

    if mode == "pilot":
        limits_ok = measured.get("status") == "PLANNED" and _integer(measured.get("record_limit"), 1)
        limits_ok = limits_ok and measured["record_limit"] <= MAX_PILOT_RECORDS
        limits_ok = limits_ok and _integer(measured.get("max_batch_records"), 1) and measured["max_batch_records"] <= 1000
        limits_ok = limits_ok and _integer(measured.get("max_duration_seconds"), 1)
        check("PILOT_EXECUTION_BOUNDED", bool(limits_ok))
        budgets, seen = _list(measured.get("volume_budgets")), set()
        budgets_ok = bool(budgets)
        for raw in budgets:
            budget = _obj(raw)
            volume, maximum = budget.get("volume_id"), budget.get("max_bytes")
            if not _text(volume) or volume in seen or volume not in volumes or not _integer(maximum, 1):
                budgets_ok = False
                continue
            seen.add(volume)
            capacity_totals[volume] = maximum
        check("PILOT_CAPACITY_BUDGET_DEFINED", bool(budgets_ok))
    elif mode == "full":
        check("PILOT_SUCCESS", measured.get("status") == "SUCCESS")
        check("PILOT_SOURCE_DESTINATION_MATCH", _text(snapshot.get("uuid"))
              and measured.get("snapshot_uuid") == snapshot.get("uuid")
              and _uuid(destination.get("deployment_id")) and measured.get("deployment_id") == destination.get("deployment_id")
              and _string_set(measured.get("source_ids")) == set(selected) and bool(selected))
        records = measured.get("measured_records")
        full_volume = _integer(records, MIN_FULL_PILOT_RECORDS) and _integer(projected, 1)
        full_volume = full_volume and records <= projected and _integer(measured.get("projected_records"), 1)
        full_volume = full_volume and measured.get("projected_records") == projected
        check("PILOT_REPRESENTATIVE_VOLUME", bool(full_volume) and measured.get("representative") is True)
        check("PILOT_LOSSLESS_RECONCILIATION", measured.get("reconciled") is True and measured.get("lossless_coverage") is True
              and _integer(measured.get("failed_records")) and measured.get("failed_records") == 0
              and _integer(measured.get("unpreserved_fields")) and measured.get("unpreserved_fields") == 0)
        measured_at = _date(measured.get("measured_at"))
        check("PILOT_DATE_VALID", measured_at is not None and completed is not None
              and completed <= measured_at <= latest and observed is not None and measured_at <= observed)
        metrics = _obj(measured.get("metrics"))
        check("PILOT_METRICS_MEASURED", all(_integer(metrics.get(key), minimum) for key, minimum in (
            ("unique_entities", 1), ("source_intersection_records", 0), ("items", 1), ("observations", 1),
            ("nested_documents", 0), ("elapsed_seconds", 1), ("search_rebuild_seconds", 0)))
              and _integer(records, 1) and metrics.get("unique_entities", records + 1) <= records
              and metrics.get("source_intersection_records", records + 1) <= records)
        components, seen, kinds = _list(measured.get("components")), set(), set()
        components_ok = bool(components) and bool(full_volume)
        for raw in components:
            part = _obj(raw)
            kind, volume = part.get("kind"), part.get("volume_id")
            size, copies = part.get("measured_bytes"), part.get("copies")
            if (type(kind) is not str or kind not in CAPACITY_COMPONENTS or not _text(volume)
                    or volume not in volumes or (kind, volume) in seen or not _integer(size) or not _integer(copies, 1)):
                components_ok = False
                continue
            seen.add((kind, volume))
            kinds.add(kind)
            if kind in {"postgres", "search", "wal", "backup", "replica"} and size == 0:
                components_ok = False
            if full_volume:
                extrapolated = (size * projected + records - 1) // records
                capacity_totals[volume] = capacity_totals.get(volume, 0) + extrapolated * copies
        check("PILOT_CAPACITY_COMPONENTS_MEASURED", bool(components_ok) and kinds == CAPACITY_COMPONENTS)

    capacity = []
    sufficient = bool(capacity_totals) and bool(volumes_valid)
    # No path, host, DSN, field value or arbitrary metadata is included here.
    for ordinal, (volume, base) in enumerate(sorted(capacity_totals.items()), 1):
        required = (base * 3 + 1) // 2
        available = volumes[volume]
        sufficient = sufficient and available >= required
        capacity.append({"volume_number": ordinal, "base_bytes": base, "margin_percent": 50,
                         "required_free_bytes": required, "observed_free_bytes": available,
                         "sufficient": available >= required})
    check("CAPACITY_WITH_50_PERCENT_MARGIN", bool(sufficient))
    blockers = [item["code"] for item in checks if item["status"] == "blocked"]
    ready = not blockers
    return {"ready": ready, "phase": mode, "ready_for_full_import": ready and mode == "full",
            "development_allowed": True, "production_cutover_approved": False,
            "checked_at": now.isoformat(), "blockers": blockers, "checks": checks, "capacity": capacity}

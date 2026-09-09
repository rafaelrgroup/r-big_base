"""Synthetic availability receipts; no live service or real migration proofs."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from bigbase.canonical_search import PROJECTION_VERSION, ProjectionError, build_query, custom_selector
from bigbase.canonical_search_availability import (
    AVAILABILITY_VERSION, custom_filter_availability, select_custom_filter,
)
from bigbase.canonical_store import digest
from test_canonical_custom_search import DEPLOYMENT, metadata

NOW = datetime(2026, 9, 9, 7, tzinfo=timezone.utc)
TARGET = {"deployment_id": DEPLOYMENT, "cluster_uuid": "synthetic-cluster",
          "index_uuid": "synthetic-index", "projection_version": PROJECTION_VERSION}
CUT = "a" * 64


def proof(meta):
    return {"contract": AVAILABILITY_VERSION, "selector": custom_selector(meta),
            "target": deepcopy(TARGET), "coverage_cut_sha256": CUT,
            "checked_at": NOW.isoformat(), "expires_at": (NOW + timedelta(seconds=120)).isoformat(),
            "proof_sha256": "b" * 64, "reconciled": True,
            "counts": {"expected": 10, "indexed": 10, "omitted": 0, "failed": 0}}


def availability(meta=None, **kwargs):
    return custom_filter_availability(meta or metadata(), **{
        "target": TARGET, "coverage_cut_sha256": CUT, "now": NOW, **kwargs})


def select(meta, **kwargs):
    return select_custom_filter(meta, **{"target": TARGET, "coverage_cut_sha256": CUT,
                                        "now": NOW, "receipt": proof(meta), **kwargs})


def test_default_pending_does_not_trust_mutable_catalog_search_state():
    meta = metadata()
    meta["canonical_search_state"] = "ready"
    result = availability(meta)
    assert result["state"] == "pending" and not result["selectable"]
    with pytest.raises(ProjectionError, match="^PROOF_REQUIRED$"):
        select(meta, value="x", receipt=None)


@pytest.mark.parametrize("kind,value", [("boolean", False), ("boolean", None), ("integer", 0),
    ("decimal", "9007199254740993.0123456789"), ("text", "literal")])
def test_selection_preserves_literals_sources_and_correlated_flags(kind, value):
    meta = metadata(kind)
    before = deepcopy(meta)
    result = select(meta, value=value, flags={"valid": False}, source_id="synthetic", include_invalid=True)
    assert result["value"] == value and type(result["value"]) is type(value)
    assert result["flags"] == {"valid": False} and result["source_id"] == "synthetic"
    assert build_query(result, include_invalid=True)
    assert meta == before


@pytest.mark.parametrize("key", ["deployment_id", "cluster_uuid", "index_uuid", "projection_version"])
def test_old_target_proof_cannot_enable_new_index(key):
    receipt = proof(metadata()); receipt["target"][key] = "old"
    assert availability(receipt=receipt)["reason"] == "AVAILABILITY_PROOF_CONTEXT_MISMATCH"


@pytest.mark.parametrize("key,value", [("version", 2), ("sha256", "c"*64),
    ("type", "url"), ("field_id", "other"), ("deployment_id", None)])
def test_other_definition_never_becomes_selectable(key, value):
    receipt = proof(metadata()); receipt["selector"][key] = value
    assert not availability(receipt=receipt)["selectable"]


def test_requested_cut_must_match_and_counts_alone_do_not_prove_reconciliation():
    receipt = proof(metadata())
    assert not availability(receipt=receipt, coverage_cut_sha256="c"*64)["selectable"]
    receipt["reconciled"] = False
    assert availability(receipt=receipt)["reason"] == "INCOMPLETE_SEARCH_COVERAGE"


@pytest.mark.parametrize("key,value", [("indexed", 9), ("omitted", 1), ("failed", 1),
    ("expected", True), ("indexed", -1), ("failed", "0")])
def test_partial_or_malformed_coverage_cannot_enable_filter(key, value):
    receipt = proof(metadata()); receipt["counts"][key] = value
    assert not availability(receipt=receipt)["selectable"]


@pytest.mark.parametrize("key,value", [("checked_at", "2026-09-09T07:00:00"),
    ("checked_at", "2026-09-09T07:00:01+00:00"), ("expires_at", "2026-09-09T07:02:01+00:00"),
    ("expires_at", "invalid"), ("expires_at", NOW.isoformat()), ("proof_sha256", None),
    ("reconciled", 1), ("extra", "ignored?")])
def test_bad_proofs_are_sanitized_and_not_selectable(key, value):
    receipt = proof(metadata()); receipt[key] = value
    result = availability(receipt=receipt)
    assert not result["selectable"] and result["reason"] == "INVALID_AVAILABILITY_PROOF"
    assert "ignored?" not in str(result)


def test_expiry_rechecked_even_when_ui_previously_saw_ready():
    meta = metadata(); receipt = proof(meta)
    assert availability(meta, receipt=receipt)["selectable"]
    later = NOW + timedelta(seconds=120)
    assert availability(meta, receipt=receipt, now=later)["state"] == "stale"
    with pytest.raises(ProjectionError, match="^AVAILABILITY_PROOF_EXPIRED$"):
        select(meta, value="x", receipt=receipt, now=later)


def test_historical_inactive_definition_remains_separate_from_new_version():
    meta = metadata()
    meta["field_definition"].update(active=False, name="Historical")
    meta["field_definition_sha256"] = digest(meta["field_definition"])
    receipt = proof(meta)
    assert availability(meta, receipt=receipt)["selectable"]
    assert not availability(metadata(version=2), receipt=receipt)["selectable"]


def test_reference_unbound_and_local_definitions_cannot_be_marked_ready():
    meta = metadata("reference")
    assert availability(meta, receipt=proof(meta))["state"] == "unsupported"
    assert custom_filter_availability({}, target=TARGET, coverage_cut_sha256=CUT, now=NOW)["reason"] == "UNBOUND_CUSTOM_FIELD"
    meta = metadata(); meta["catalog_deployment_id"] = "760baef1-d347-4e9c-a4c6-d4d52b992d0f"
    assert availability(meta, receipt=proof(meta))["reason"] == "CATALOG_DESTINATION_MISMATCH"
    meta = metadata()
    meta["custom_field"]["contract"] = "canonical-custom-field-2026-09-09.1"
    meta.pop("catalog_deployment_id")
    assert availability(meta, receipt=proof(meta))["reason"] == "CATALOG_DESTINATION_MISMATCH"


def test_operators_advertised_match_compiler_and_no_sorting_claim():
    date = metadata("date")
    assert select(date, value={"gte": "2024-01-01"}, op="range")
    number = metadata("integer")
    assert availability(number, receipt=proof(number))["operators"] == ["eq"]
    assert not availability(number, receipt=proof(number))["sortable"]
    with pytest.raises(ProjectionError, match="^UNSUPPORTED_CUSTOM_OPERATOR$"):
        select(number, value={"gte": 1}, op="range")
    with pytest.raises(ProjectionError, match="^SHORT_PREFIX_REQUIRES_SELECTIVE_FILTER$"):
        select(metadata(), value="a", op="prefix")


def test_empty_reconciled_cut_is_valid_without_fabricating_values_or_mutating_receipt():
    meta = metadata(); receipt = proof(meta)
    receipt["counts"].update(expected=0, indexed=0)
    before = deepcopy(receipt)
    result = availability(meta, receipt=receipt)
    assert result["selectable"]
    result["selector"]["version"] = 9
    result["target"]["index_uuid"] = "changed"
    assert receipt == before and TARGET["index_uuid"] == "synthetic-index"


def test_boolean_definition_version_cannot_impersonate_integer_version():
    receipt = proof(metadata()); receipt["selector"]["version"] = True
    assert not availability(receipt=receipt)["selectable"]


@pytest.mark.parametrize("changes", [{"target": None}, {"target": {}},
    {"target": {**TARGET, "projection_version": "old"}}, {"coverage_cut_sha256": None},
    {"coverage_cut_sha256": "not-a-hash"}, {"now": datetime(2026, 9, 9)}])
def test_invalid_expected_context_never_defaults_to_a_destination(changes):
    with pytest.raises(ProjectionError, match="^INVALID_SEARCH_AVAILABILITY_(CONTEXT|CLOCK)$"):
        availability(receipt=proof(metadata()), **changes)


@pytest.mark.parametrize("receipt", [[], "private-text", {}, {"state": "ready"}])
def test_invalid_receipt_has_no_sensitive_error_body(receipt):
    result = availability(receipt=receipt)
    assert result["reason"] == "INVALID_AVAILABILITY_PROOF"
    assert "private-text" not in str(result)


def test_proof_timestamps_with_offset_are_compared_as_instants():
    receipt = proof(metadata())
    receipt.update(checked_at="2026-09-09T04:00:00-03:00", expires_at="2026-09-09T04:02:00-03:00")
    assert availability(receipt=receipt)["expires_at"] == "2026-09-09T07:02:00+00:00"

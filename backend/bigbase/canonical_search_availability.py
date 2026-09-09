"""Pure, fail-closed contract for definition-bound filter selection.

Receipts must come from a trusted reconciliation service, never from a request
body. This module checks their binding and freshness; it neither authenticates
an artifact nor proves that Elasticsearch contains its reported contents.
No routes, writers, destinations or background tasks are enabled here.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import re

from .canonical_search import (
    PROJECTION_VERSION, ProjectionError, build_query, custom_selector, _custom_selector_key,
)


AVAILABILITY_VERSION = "canonical-search-availability-2026-09-09.1"
MAX_PROOF_AGE_SECONDS = 120
OPERATORS = {
    "text": ("eq", "ieq", "prefix", "contains", "match"),
    "enum": ("eq", "ieq", "prefix", "contains", "match"),
    "url": ("eq", "ieq", "prefix", "contains", "match"),
    "integer": ("eq",), "decimal": ("eq",), "boolean": ("eq",),
    "date": ("eq", "range"), "reference": (),
}


def _date(value):
    if not isinstance(value, str):
        raise ValueError()
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError()
    return parsed.astimezone(timezone.utc)


def _sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _target(value):
    return (isinstance(value, dict)
            and set(value) == {"deployment_id", "cluster_uuid", "index_uuid", "projection_version"}
            and all(isinstance(v, str) and 0 < len(v) <= 160 for v in value.values())
            and value["projection_version"] == PROJECTION_VERSION)


def custom_filter_availability(metadata, *, target, coverage_cut_sha256, receipt=None, now=None):
    """Describe one historical definition at an explicitly requested coverage cut.

    The caller supplies authorized metadata and trusted expected target/cut on
    every evaluation. Counts refer to values of this selector, not entities.
    A ready result is scoped to the cut and expires; it is not global readiness.
    Inactivation/renaming does not remove historical query capability.
    """
    selector = custom_selector(metadata)
    if not _target(target) or not _sha(coverage_cut_sha256):
        raise ProjectionError("INVALID_SEARCH_AVAILABILITY_CONTEXT")
    now = datetime.now(timezone.utc) if now is None else now
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ProjectionError("INVALID_SEARCH_AVAILABILITY_CLOCK")
    now = now.astimezone(timezone.utc)
    result = {
        "contract": AVAILABILITY_VERSION, "selector": deepcopy(selector),
        "target": deepcopy(target), "coverage_cut_sha256": coverage_cut_sha256,
        "state": "pending", "reason": "PROOF_REQUIRED", "selectable": False,
        "operators": list(OPERATORS[selector["type"]]) if selector else [],
        "sortable": False, "proof_sha256": None, "expires_at": None,
    }

    def unavailable(state, reason):
        return {**result, "state": state, "reason": reason}

    if selector is None:
        return unavailable("unsupported", "UNBOUND_CUSTOM_FIELD")
    if not result["operators"]:
        return unavailable("unsupported", "UNSUPPORTED_CUSTOM_REFERENCE_QUERY")
    if selector["deployment_id"] != target["deployment_id"]:
        return unavailable("blocked", "CATALOG_DESTINATION_MISMATCH")
    if receipt is None:
        return result
    keys = {"contract", "selector", "target", "coverage_cut_sha256", "checked_at",
            "expires_at", "proof_sha256", "reconciled", "counts"}
    if not isinstance(receipt, dict) or set(receipt) != keys:
        return unavailable("blocked", "INVALID_AVAILABILITY_PROOF")
    try:
        _custom_selector_key(receipt["selector"])
    except ProjectionError:
        return unavailable("blocked", "INVALID_AVAILABILITY_PROOF")
    if (receipt["contract"] != AVAILABILITY_VERSION or receipt["selector"] != selector
            or receipt["target"] != target or receipt["coverage_cut_sha256"] != coverage_cut_sha256):
        return unavailable("blocked", "AVAILABILITY_PROOF_CONTEXT_MISMATCH")
    try:
        checked, expires = _date(receipt["checked_at"]), _date(receipt["expires_at"])
    except (ValueError, TypeError, OverflowError):
        return unavailable("blocked", "INVALID_AVAILABILITY_PROOF")
    if (not checked < expires <= checked + timedelta(seconds=MAX_PROOF_AGE_SECONDS)
            or checked > now or not _sha(receipt["proof_sha256"])):
        return unavailable("blocked", "INVALID_AVAILABILITY_PROOF")
    if now >= expires:
        return unavailable("stale", "AVAILABILITY_PROOF_EXPIRED")
    counts = receipt["counts"]
    if (not isinstance(counts, dict) or set(counts) != {"expected", "indexed", "omitted", "failed"}
            or any(type(v) is not int or not 0 <= v <= 2**63 - 1 for v in counts.values())
            or type(receipt["reconciled"]) is not bool):
        return unavailable("blocked", "INVALID_AVAILABILITY_PROOF")
    if (receipt["reconciled"] is not True or counts["expected"] != counts["indexed"]
            or counts["omitted"] != 0 or counts["failed"] != 0):
        return unavailable("pending", "INCOMPLETE_SEARCH_COVERAGE")
    return {**result, "state": "ready", "reason": None, "selectable": True,
            "proof_sha256": receipt["proof_sha256"], "expires_at": expires.isoformat()}


def select_custom_filter(metadata, *, value, op="eq", target, coverage_cut_sha256,
                         receipt=None, now=None, flags=None, source_id=None,
                         include_invalid=False, include_pending=False):
    """Build a single filter after reevaluating proof, never trusting UI state.

    Preserve null/false/zero, source and correlated flags. The compiler still
    enforces text limits and operator/value types. Multiple versions require
    separate explicit selections in an `any` group.
    """
    availability = custom_filter_availability(metadata, target=target,
        coverage_cut_sha256=coverage_cut_sha256, receipt=receipt, now=now)
    if not availability["selectable"]:
        raise ProjectionError(availability["reason"])
    if not isinstance(op, str) or op not in availability["operators"]:
        raise ProjectionError("UNSUPPORTED_CUSTOM_OPERATOR")
    criterion = {"kind": "custom", "field": "value", "custom": availability["selector"],
                 "op": op, "value": deepcopy(value)}
    if flags is not None:
        criterion["flags"] = deepcopy(flags)
    if source_id is not None:
        criterion["source_id"] = source_id
    build_query(criterion, include_invalid=include_invalid, include_pending=include_pending)
    return criterion

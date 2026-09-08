"""Rebuildable Elasticsearch 9 projection; no I/O or default destination on import.

PostgreSQL owns the complete record. This module deliberately indexes a bounded,
explicit subset and never acknowledges an outbox event before publication proof.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
import re
import unicodedata
from urllib.parse import quote, urlsplit
from uuid import UUID

import httpx

from .canonical_store import json_text


PROJECTION_VERSION = "canonical-search-2026-09-08.2"
MAX_VERSION = 2**63 - 1
MAX_TEXT_BYTES = 8192
MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_NESTED_OBJECTS = 9000
FLAGS = ("valid", "is_whatsapp", "ownership_confirmed", "deliverable", "residence_confirmed")
FIELDS = {
    "identity": {"name", "legal_name", "trade_name", "birth_date", "death_date", "sex", "nationality", "opening_date"},
    "document": {"number", "type", "country", "issuer", "state", "syntax_valid"},
    "phone": {"number", "type", "phone_type", "usage", "extension", "country", "national_number", "area_code", "syntax_valid"},
    "email": {"email", "domain", "syntax_valid"},
    "address": {"country", "state", "city", "city_code", "neighborhood", "street", "street_type", "street_title", "number", "complement", "postal_code", "usage", "classification"},
    "username": {"platform", "username", "url", "external_id"},
    "relationship": {"target_id", "target_name", "target_document.number", "target_document.type", "target_document.country", "type", "role", "from", "until", "direction", "participation"},
    "activity": {"code", "description", "role", "area", "primary"},
}
DATE_FIELDS = {"birth_date", "death_date", "opening_date", "from", "until"}
PENDING = {"pending", "ambiguous", "unclassified"}
OMISSIONS = ("unknown_items", "unknown_fields", "unsupported_values", "unsafe_text", "long_text", "unknown_flags", "metadata")


class ProjectionError(ValueError):
    """Stable operational code only: never include payloads or HTTP error bodies."""


def _uuid(value):
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        raise ProjectionError("INVALID_ENTITY_ID") from None


def _version(value):
    if type(value) is not int or not 1 <= value <= MAX_VERSION:
        raise ProjectionError("INVALID_RECORD_VERSION")
    return value


def _bytes(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")


def _hash(value):
    return hashlib.sha256(json_text(value).encode("ascii")).hexdigest()


def fold(value):
    return "".join(c for c in unicodedata.normalize("NFKD", value).casefold() if not unicodedata.combining(c))


def _safe_text(value):
    if "\x00" in value or any(0xD800 <= ord(c) <= 0xDFFF for c in value):
        return "unsafe_text"
    if len(value.encode("utf-8")) > MAX_TEXT_BYTES:
        return "long_text"
    return None


def _numeric_key(value):
    # Avoid Decimal.normalize(), whose ambient context can round large numbers.
    value = Decimal(value) if type(value) is int else value
    if not value.is_finite():
        raise ProjectionError("NON_FINITE_NUMBER")
    sign, digits, exponent = value.as_tuple()
    digits = "".join(str(digit) for digit in digits)
    if not digits.strip("0"):
        return "0e0"
    trimmed = digits.rstrip("0")
    return ("-" if sign else "") + trimmed + "e" + str(exponent + len(digits) - len(trimmed))


def _scalar(value):
    if value is None:
        return {"value_type": "null"}, None
    if type(value) is bool:
        return {"value_type": "boolean", "value_boolean": value}, None
    if type(value) is int or isinstance(value, Decimal):
        key = _numeric_key(value)
        if len(key) > MAX_TEXT_BYTES:
            return None, "long_text"
        return {"value_type": "integer" if type(value) is int else "decimal", "number_exact": key}, None
    if isinstance(value, str):
        reason = _safe_text(value)
        folded = fold(value) if reason is None else ""
        reason = reason or _safe_text(folded)
        if reason:
            return None, reason
        return {"value_type": "text", "value_exact": value, "value_folded": folded, "value_text": value, "value_contains": folded}, None
    return None, "unsupported_values"


def _date(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProjectionError("INVALID_PROJECTION_DATE")
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            date.fromisoformat(value)
        else:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError()
    except ValueError:
        raise ProjectionError("INVALID_PROJECTION_DATE") from None
    return value


def _flag(flag, resolved):
    if flag is None:
        return {"value": None, "state": "unknown", "applicable": False, "expires_at": None}
    value = flag.get("value")
    if value is not None and type(value) is not bool:
        raise ProjectionError("INVALID_FLAG_VALUE")
    applicable = flag.get("applicable") is True and resolved and flag.get("status") not in PENDING
    value = value if applicable else None
    return {"value": value, "state": "unknown" if value is None else "true" if value else "false",
            "applicable": applicable, "expires_at": _date(flag.get("metadata", {}).get("expires_at")),
            "source_key": _hash(flag.get("source_id")), "observation_id": str(flag.get("observation_id", "")),
            "effective_at": _date(flag.get("effective_at")), "received_at": _date(flag.get("received_at"))}


def build_projection(entity: dict) -> dict:
    """Deterministic subset of CanonicalStore.get_entity(), with explicit omissions.

    Unknown metadata and unsafe/oversized values remain in PostgreSQL. A document
    exceeding the bounded nested/body limits fails as a whole, leaving its event.
    """
    if not isinstance(entity, dict) or entity.get("entity_type") not in {"person", "company"}:
        raise ProjectionError("INVALID_ENTITY")
    output = {"id": _uuid(entity.get("id")), "entity_type": entity["entity_type"],
              "record_version": _version(entity.get("version")), "projection_version": PROJECTION_VERSION,
              "items": [], "omitted": {key: 0 for key in OMISSIONS}}
    if not isinstance(entity.get("items"), list):
        raise ProjectionError("INVALID_ITEMS")
    seen_items = set()
    nested_count = 0
    for item in sorted(entity["items"], key=lambda item: str(item.get("id"))):
        item_id = _uuid(item.get("id"))
        if item_id in seen_items:
            raise ProjectionError("DUPLICATE_ITEM")
        seen_items.add(item_id)
        kind = item.get("kind")
        if kind not in FIELDS:
            output["omitted"]["unknown_items"] += 1
            output["omitted"]["unknown_fields"] += len(item.get("fields", []))
            continue
        indexed = {"id": item_id, "kind": kind, "fields": []}
        by_path = {}
        for flag in item.get("flags", []):
            if flag.get("name") not in FLAGS:
                output["omitted"]["unknown_flags"] += 1
                continue
            identity = (flag.get("path"), flag["name"])
            if identity in by_path:
                raise ProjectionError("DUPLICATE_FLAG_STATE")
            by_path[identity] = flag
        explicit_paths = {field["path"] for field in item.get("fields", [])}
        emitted = set()
        for field in sorted(item.get("fields", []), key=lambda row: (row["path"], str(row.get("observation_id")))):
            path = field["path"]
            candidates = [(path, field.get("value"), False)]
            if path == "target_document" and kind == "relationship" and isinstance(field.get("value"), dict):
                candidates = [("target_document." + key, value, True) for key, value in sorted(field["value"].items())]
            attrs = field.get("metadata", {}).get("item_attributes", {})
            if isinstance(attrs, dict):
                candidates.extend((key, value, True) for key, value in sorted(attrs.items()) if key in FIELDS[kind] and key not in explicit_paths)
            for key, value, derived in candidates:
                if key not in FIELDS[kind]:
                    output["omitted"]["unknown_fields"] += 1
                    continue
                scalar, reason = _scalar(value)
                if reason:
                    output["omitted"][reason] += 1
                    continue
                identity = (key, str(field.get("observation_id")))
                if identity in emitted:
                    raise ProjectionError("DUPLICATE_FIELD_STATE")
                emitted.add(identity)
                metadata = field.get("metadata", {})
                status = field.get("status", "unknown")
                if not isinstance(status, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", status):
                    raise ProjectionError("INVALID_FIELD_STATUS")
                resolved = status not in PENDING and metadata.get("classification_state") not in PENDING
                # A flag is bound to the parent's complete value. Derivation of
                # a document component/type does not confirm that new value.
                flags = {name: _flag(None if derived else by_path.get((path, name)), resolved) for name in FLAGS}
                info = {"source_id": field.get("source_id"), "source_path": metadata.get("source_path"),
                        "adapter_version": metadata.get("adapter_version"), "normalizer_version": metadata.get("normalizer_version"),
                        "field_id": metadata.get("field_id"), "field_definition_version": metadata.get("field_definition_version"),
                        "derived_from": path if derived else None}
                metadata_json = json_text(info)
                if len(metadata_json) > MAX_TEXT_BYTES:
                    output["omitted"]["metadata"] += 1
                    metadata_json = "null"
                current = {"key": key, **scalar, "status": status, "resolved": resolved,
                           # This records evidence, not wall-clock applicability:
                           # queries additionally enforce the flag expiry date.
                           "confirmation_recorded": resolved and flags["valid"]["value"] is True,
                           "source_key": _hash(field.get("source_id")), "observation_id": str(field.get("observation_id", "")),
                           "effective_at": _date(field.get("effective_at")), "received_at": _date(field.get("received_at")),
                           "metadata_json": metadata_json, "flags": flags,
                           "parent_valid": _flag(by_path.get((path, "valid")) if derived else None, resolved)}
                if key in DATE_FIELDS and isinstance(value, str) and resolved:
                    try:
                        current["value_date"] = _date(value)
                    except ProjectionError:
                        current["resolved"] = current["confirmation_recorded"] = False
                        current["flags"] = {name: _flag(None if derived else by_path.get((path, name)), False) for name in FLAGS}
                indexed["fields"].append(current)
        indexed["fields"].sort(key=lambda row: (row["key"], row["observation_id"]))
        nested_count += 1 + len(indexed["fields"])
        if nested_count > MAX_NESTED_OBJECTS:
            raise ProjectionError("PROJECTION_NESTED_LIMIT")
        output["items"].append(indexed)
    output["projection_hash"] = hashlib.sha256(_bytes(output)).hexdigest()
    if len(_bytes(output)) > MAX_DOCUMENT_BYTES:
        raise ProjectionError("PROJECTION_BYTE_LIMIT")
    return output


def index_definition() -> dict:
    """Return a versioned create-index body; the caller provisions it separately."""
    keyword = lambda: {"type": "keyword"}
    timestamp = lambda: {"type": "date", "format": "strict_date_optional_time"}
    flag_properties = {"value": {"type": "boolean"}, "state": keyword(), "applicable": {"type": "boolean"},
                       "expires_at": timestamp(), "effective_at": timestamp(), "received_at": timestamp(),
                       "source_key": keyword(), "observation_id": keyword()}
    fields = {"key": keyword(), "value_type": keyword(), "value_exact": keyword(), "value_folded": keyword(),
              "value_text": {"type": "text", "analyzer": "bigbase_text"}, "value_contains": {"type": "wildcard"}, "value_boolean": {"type": "boolean"},
              "number_exact": keyword(), "value_date": timestamp(), "status": keyword(),
              "resolved": {"type": "boolean"}, "confirmation_recorded": {"type": "boolean"}, "source_key": keyword(),
              "observation_id": keyword(), "effective_at": timestamp(), "received_at": timestamp(),
              "metadata_json": {"type": "text", "index": False},
              "parent_valid": {"dynamic": "strict", "properties": flag_properties},
              "flags": {"dynamic": "strict", "properties": {name: {"dynamic": "strict", "properties": flag_properties} for name in FLAGS}}}
    return {"settings": {"analysis": {"analyzer": {"bigbase_text": {"type": "custom", "tokenizer": "standard", "filter": ["lowercase", "asciifolding"]}}},
                         "index.mapping.total_fields.limit": 256, "index.mapping.nested_objects.limit": MAX_NESTED_OBJECTS},
            "mappings": {"dynamic": "strict", "_meta": {"projection_version": PROJECTION_VERSION}, "properties": {
                "id": keyword(), "entity_type": keyword(), "record_version": {"type": "long"},
                "projection_version": keyword(), "projection_hash": keyword(),
                "omitted": {"dynamic": "strict", "properties": {name: {"type": "integer"} for name in OMISSIONS}},
                "items": {"type": "nested", "dynamic": "strict", "properties": {"id": keyword(), "kind": keyword(),
                    "fields": {"type": "nested", "dynamic": "strict", "properties": fields}}}}}}


def _live_flag(name, expected, *, path=None):
    path = path or "items.fields.flags." + name
    live = {"bool": {"should": [{"bool": {"must_not": [{"exists": {"field": path + ".expires_at"}}]}},
                                  {"range": {path + ".expires_at": {"gt": "now"}}}], "minimum_should_match": 1}}
    if expected is None:
        return {"bool": {"must_not": [{"bool": {"filter": [
            {"terms": {path + ".state": ["true", "false"]}}, {"term": {path + ".applicable": True}}, live]}}]}}
    return {"bool": {"filter": [{"term": {path + ".state": "true" if expected else "false"}},
                                  {"term": {path + ".applicable": True}}, live]}}


def build_query(criteria: dict, *, entity_type: str | None = None, include_pending: bool = False,
                include_invalid: bool = False) -> dict:
    """Compile bounded AND/OR and same-item filters; returns IDs/versions only.

    Equality is typed (bool is never a number); Decimal/int numerical equality is
    exact. Numeric ranges are intentionally unsupported until a typed catalogue.
    """
    if type(include_pending) is not bool or type(include_invalid) is not bool or entity_type not in {None, "person", "company"}:
        raise ProjectionError("INVALID_QUERY_OPTIONS")
    count = 0

    def selective(node, depth=0):
        if depth > 8 or not isinstance(node, dict):
            return False
        if "all" in node and isinstance(node["all"], list):
            return len(node["all"]) <= 100 and any(selective(child, depth + 1) for child in node["all"])
        if "any" in node and isinstance(node["any"], list):
            return 1 <= len(node["any"]) <= 100 and all(selective(child, depth + 1) for child in node["any"])
        if "same_item" in node and isinstance(node["same_item"], dict):
            body = node["same_item"]
            return isinstance(body.get("conditions"), list) and len(body["conditions"]) <= 50 and any(selective({**child, "kind": body.get("kind")}, depth + 1) for child in body["conditions"] if isinstance(child, dict))
        kind, key, value, op = node.get("kind"), node.get("field"), node.get("value"), node.get("op", "eq")
        if op == "range" and key in DATE_FIELDS and isinstance(value, dict):
            return bool(set(value) & {"gt", "gte"}) and bool(set(value) & {"lt", "lte"})
        return (op in {"eq", "ieq"} and isinstance(value, str) and len(value.strip()) >= 3
                and (kind, key) in {("document", "number"), ("phone", "number"), ("email", "email"),
                                    ("username", "username"), ("address", "postal_code")})

    def field_query(kind, node, guarded):
        if not isinstance(node, dict) or set(node) - {"field", "op", "value", "flags", "status", "source_id"} or "field" not in node:
            raise ProjectionError("INVALID_FIELD_QUERY")
        key, op = node["field"], node.get("op", "eq")
        if key not in FIELDS[kind] or op not in {"eq", "ieq", "prefix", "contains", "match", "range"} or "value" not in node:
            raise ProjectionError("UNSUPPORTED_FIELD_QUERY")
        value = node["value"]
        clauses = [{"term": {"items.fields.key": key}}]
        if not include_pending:
            clauses.append({"term": {"items.fields.resolved": True}})
        if not include_invalid:
            clauses.append({"bool": {"must_not": [_live_flag("valid", False)]}})
            clauses.append({"bool": {"must_not": [_live_flag("valid", False, path="items.fields.parent_valid")]}})
        if op == "range":
            if key not in DATE_FIELDS or not isinstance(value, dict) or not value or set(value) - {"gt", "gte", "lt", "lte"}:
                raise ProjectionError("UNSUPPORTED_RANGE")
            if any(bound is None for bound in value.values()):
                raise ProjectionError("INVALID_PROJECTION_DATE")
            clauses.append({"range": {"items.fields.value_date": {k: _date(v) for k, v in value.items()}}})
        else:
            scalar, reason = _scalar(value)
            if reason:
                raise ProjectionError("UNSEARCHABLE_VALUE")
            if op != "eq" and not isinstance(value, str):
                raise ProjectionError("TEXT_OPERATOR_REQUIRES_TEXT")
            if op == "eq":
                if value is None:
                    clauses.append({"term": {"items.fields.value_type": "null"}})
                elif type(value) is bool:
                    clauses.append({"term": {"items.fields.value_boolean": value}})
                elif type(value) is int or isinstance(value, Decimal):
                    clauses.append({"term": {"items.fields.number_exact": scalar["number_exact"]}})
                else:
                    clauses.append({"term": {"items.fields.value_exact": value}})
            elif op == "match":
                if not value.strip():
                    raise ProjectionError("EMPTY_TEXT_QUERY")
                clauses.append({"match": {"items.fields.value_text": {"query": value, "operator": "and"}}})
            elif op == "ieq":
                clauses.append({"term": {"items.fields.value_folded": fold(value)}})
            else:
                if not value:
                    raise ProjectionError("EMPTY_TEXT_QUERY")
                text = fold(value)
                if not text.strip():
                    raise ProjectionError("EMPTY_TEXT_QUERY")
                if op == "prefix":
                    if len(text.strip()) < 2 and not guarded:
                        raise ProjectionError("SHORT_PREFIX_REQUIRES_SELECTIVE_FILTER")
                    clauses.append({"prefix": {"items.fields.value_folded": text}})
                else:
                    if len(text.strip()) < 3:
                        raise ProjectionError("CONTAINS_REQUIRES_THREE_CHARACTERS")
                    text = text.replace("\\", "\\\\").replace("*", "\\*").replace("?", "\\?")
                    clauses.append({"wildcard": {"items.fields.value_contains": {"value": "*" + text + "*"}}})
        flags = node.get("flags", {})
        if not isinstance(flags, dict) or set(flags) - set(FLAGS) or any(v is not None and type(v) is not bool for v in flags.values()):
            raise ProjectionError("INVALID_FLAG_QUERY")
        clauses.extend(_live_flag(name, value) for name, value in flags.items())
        if "status" in node:
            if not isinstance(node["status"], str) or _safe_text(node["status"]):
                raise ProjectionError("INVALID_STATUS_QUERY")
            clauses.append({"term": {"items.fields.status": node["status"]}})
        if "source_id" in node:
            clauses.append({"term": {"items.fields.source_key": _hash(node["source_id"])}})
        return {"nested": {"path": "items.fields", "score_mode": "none", "query": {"bool": {"filter": clauses}}}}

    def compile_node(node, depth=0, guarded=False):
        nonlocal count
        count += 1
        if depth > 8 or count > 100 or not isinstance(node, dict):
            raise ProjectionError("QUERY_COMPLEXITY_LIMIT")
        for operator in ("all", "any"):
            if operator in node:
                if set(node) != {operator} or not isinstance(node[operator], list) or not 1 <= len(node[operator]) <= 100:
                    raise ProjectionError("INVALID_QUERY_GROUP")
                child_guard = guarded or (operator == "all" and any(selective(child) for child in node[operator]))
                children = [compile_node(child, depth + 1, child_guard) for child in node[operator]]
                return {"bool": {"filter": children}} if operator == "all" else {"bool": {"should": children, "minimum_should_match": 1}}
        if "same_item" in node:
            if set(node) != {"same_item"} or not isinstance(node["same_item"], dict):
                raise ProjectionError("INVALID_SAME_ITEM_QUERY")
            body = node["same_item"]
            if set(body) != {"kind", "conditions"} or not isinstance(body["conditions"], list) or not 1 <= len(body["conditions"]) <= 50:
                raise ProjectionError("INVALID_SAME_ITEM_QUERY")
            kind, conditions = body["kind"], body["conditions"]
            count += len(conditions)
            guarded = guarded or selective(node)
        else:
            kind = node.get("kind")
            conditions = [{key: value for key, value in node.items() if key != "kind"}]
        if kind not in FIELDS or count > 100:
            raise ProjectionError("UNSUPPORTED_ITEM_KIND")
        clauses = [{"term": {"items.kind": kind}}, *(field_query(kind, field, guarded) for field in conditions)]
        return {"nested": {"path": "items", "score_mode": "none", "query": {"bool": {"filter": clauses}}}}

    query = compile_node(criteria)
    if entity_type:
        query = {"bool": {"filter": [{"term": {"entity_type": entity_type}}, query]}}
    return {"query": query, "_source": False, "fields": ["id", "record_version"], "sort": [{"id": "asc"}]}


class ElasticProjection:
    """Explicit client/URL/write alias. Construction has no network effects."""
    def __init__(self, client: httpx.Client, *, base_url: str, alias: str,
                 expected_cluster_uuid: str, expected_index_uuid: str,
                 max_response_bytes: int = MAX_RESPONSE_BYTES):
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            raise ProjectionError("INVALID_SEARCH_DESTINATION")
        if parsed.port == 9200:
            raise ProjectionError("LEGACY_SEARCH_PORT_FORBIDDEN")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ProjectionError("REMOTE_SEARCH_REQUIRES_HTTPS")
        if not isinstance(alias, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,150}", alias):
            raise ProjectionError("INVALID_SEARCH_ALIAS")
        if any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", value)
               for value in (expected_cluster_uuid, expected_index_uuid)):
            raise ProjectionError("SEARCH_DESTINATION_IDENTITIES_REQUIRED")
        if type(max_response_bytes) is not int or not 1024 <= max_response_bytes <= MAX_RESPONSE_BYTES:
            raise ProjectionError("INVALID_SEARCH_RESPONSE_LIMIT")
        self.client, self.base_url, self.alias = client, base_url.rstrip("/"), alias
        self.expected_cluster_uuid, self.expected_index_uuid = expected_cluster_uuid, expected_index_uuid
        self.max_response_bytes = max_response_bytes
        self.index = None

    def _request(self, method, path, **kwargs):
        try:
            headers = {**kwargs.pop("headers", {}), "Accept-Encoding": "identity"}
            with self.client.stream(method, self.base_url + path, timeout=30, follow_redirects=False, headers=headers, **kwargs) as response:
                # Never let an unbounded compressed response expand before the
                # byte guard. The private ES endpoint supports identity encoding.
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise ProjectionError("SEARCH_UNSUPPORTED_RESPONSE_ENCODING")
                content = bytearray()
                for chunk in response.iter_bytes(chunk_size=65536):
                    if len(content) + len(chunk) > self.max_response_bytes:
                        raise ProjectionError("SEARCH_RESPONSE_TOO_LARGE")
                    content.extend(chunk)
                return httpx.Response(response.status_code, content=bytes(content))
        except httpx.HTTPError:
            raise ProjectionError("SEARCH_TRANSPORT_ERROR") from None

    @staticmethod
    def _json(response):
        try:
            result = response.json()
        except (ValueError, UnicodeError):
            raise ProjectionError("SEARCH_INVALID_RESPONSE") from None
        if not isinstance(result, dict):
            raise ProjectionError("SEARCH_INVALID_RESPONSE")
        return result

    def verify_target(self):
        root = self._request("GET", "/")
        if root.status_code != 200 or self._json(root).get("cluster_uuid") != self.expected_cluster_uuid:
            raise ProjectionError("SEARCH_CLUSTER_IDENTITY_CHANGED")
        settings = self._request("GET", f"/{self.alias}/_settings?flat_settings=true")
        if settings.status_code != 200:
            raise ProjectionError("SEARCH_TARGET_UNAVAILABLE")
        pinned = self._json(settings)
        if len(pinned) != 1:
            raise ProjectionError("SEARCH_ALIAS_REQUIRES_ONE_INDEX")
        pinned_index, pinned_body = next(iter(pinned.items()))
        if pinned_body.get("settings", {}).get("index.uuid") != self.expected_index_uuid:
            raise ProjectionError("SEARCH_INDEX_IDENTITY_CHANGED")
        response = self._request("GET", f"/{self.alias}/_mapping")
        if response.status_code != 200:
            raise ProjectionError("SEARCH_TARGET_UNAVAILABLE")
        result = self._json(response)
        if len(result) != 1:
            raise ProjectionError("SEARCH_ALIAS_REQUIRES_ONE_INDEX")
        index, body = next(iter(result.items()))
        if index != pinned_index:
            raise ProjectionError("SEARCH_INDEX_IDENTITY_CHANGED")
        if body.get("mappings", {}).get("_meta", {}).get("projection_version") != PROJECTION_VERSION:
            raise ProjectionError("SEARCH_MAPPING_VERSION_MISMATCH")
        self.index = index

    def publish(self, document: dict) -> str:
        owner, version = _uuid(document.get("id")), _version(document.get("record_version"))
        if document.get("projection_version") != PROJECTION_VERSION:
            raise ProjectionError("PROJECTION_VERSION_MISMATCH")
        unhashed = {key: value for key, value in document.items() if key != "projection_hash"}
        if document.get("projection_hash") != hashlib.sha256(_bytes(unhashed)).hexdigest():
            raise ProjectionError("PROJECTION_HASH_MISMATCH")
        # Recheck for each publication; a reused worker must not trust a host or
        # alias replaced since an earlier event was acknowledged.
        self.verify_target()
        path = f"/{self.alias}/_doc/{quote(owner, safe='')}"
        response = self._request("PUT", path, params={"version": version, "version_type": "external_gte", "require_alias": "true", "wait_for_active_shards": "all"},
                                 content=_bytes(document), headers={"Content-Type": "application/json"})
        if response.status_code == 409:
            # A conflict alone is not evidence of a newer durable projection.
            checked = self._request("GET", path)
            if checked.status_code != 200:
                raise ProjectionError("SEARCH_CONFLICT_UNVERIFIED")
            current = self._json(checked)
            source = current.get("_source", {})
            if current.get("_id") != owner or current.get("_index") != self.index or current.get("found") is not True or source.get("id") != owner or source.get("projection_version") != PROJECTION_VERSION:
                raise ProjectionError("SEARCH_CONFLICT_UNVERIFIED")
            stored_version = _version(current.get("_version"))
            if type(source.get("record_version")) is not int or source["record_version"] != stored_version:
                raise ProjectionError("SEARCH_CONFLICT_UNVERIFIED")
            current_hash = hashlib.sha256(_bytes({key: value for key, value in source.items() if key != "projection_hash"})).hexdigest()
            if source.get("projection_hash") != current_hash or not isinstance(source.get("items"), list):
                raise ProjectionError("SEARCH_CONFLICT_UNVERIFIED")
            if stored_version > version:
                return "obsolete"
            if stored_version == version and source.get("projection_hash") == document["projection_hash"]:
                return "already_indexed"
            raise ProjectionError("SEARCH_VERSION_CONTENT_CONFLICT")
        if response.status_code not in {200, 201}:
            raise ProjectionError("SEARCH_INDEX_REJECTED")
        result = self._json(response)
        shards = result.get("_shards", {})
        if result.get("_id") != owner or result.get("_index") != self.index or type(result.get("_version")) is not int or result["_version"] != version or result.get("result") not in {"created", "updated"} or type(shards.get("failed")) is not int or shards["failed"] != 0 or type(shards.get("total")) is not int or shards["total"] < 1 or type(shards.get("successful")) is not int or shards["successful"] != shards["total"]:
            raise ProjectionError("SEARCH_ACK_UNVERIFIED")
        return "indexed"


class OutboxConsumer:
    def __init__(self, store, publisher: ElasticProjection):
        self.store, self.publisher = store, publisher

    def run_once(self, *, limit=100, lease_seconds=60):
        events = self.store.claim_outbox(limit=limit, lease_seconds=lease_seconds)
        report = {"claimed": len(events), "acknowledged": 0, "indexed": 0, "obsolete": 0, "lease_lost": 0, "failed": 0, "results": []}
        for event in events:
            result = {"event_id": str(event["event_id"])}
            try:
                entity = self.store.get_entity(event["owner_id"])
                if _uuid(entity.get("id")) != _uuid(event["owner_id"]):
                    raise ProjectionError("SOURCE_IDENTITY_MISMATCH")
                if _version(entity.get("version")) < _version(event["entity_version"]):
                    raise ProjectionError("SOURCE_VERSION_BEHIND_EVENT")
                published = self.publisher.publish(build_projection(entity))
                report["obsolete" if published == "obsolete" else "indexed"] += 1
                if self.store.acknowledge_outbox(event["event_id"], event["lease_token"]):
                    report["acknowledged"] += 1
                    result.update(status="acknowledged", publication=published)
                else:
                    report["lease_lost"] += 1
                    result.update(status="lease_lost", publication=published)
            except Exception as exc:
                report["failed"] += 1
                result.update(status="failed", code=str(exc) if isinstance(exc, ProjectionError) else "OUTBOX_PROCESSING_ERROR")
            report["results"].append(result)
        return report

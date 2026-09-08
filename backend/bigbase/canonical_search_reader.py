"""Read-only, bounded PIT search with authenticated encrypted continuation.

The calling API supplies freshly evaluated authorization on every request.
This module has no default destination, implicit key generation or background job.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import secrets
import time

from cryptography.fernet import Fernet, InvalidToken
import httpx

from .canonical_search import ElasticProjection, PROJECTION_VERSION, ProjectionError, build_query, _uuid, _version
from .canonical_store import json_text


CURSOR_PREFIX = "bs1_"
IDLE_TTL = 15 * 60
ABSOLUTE_TTL = 60 * 60
MAX_CURSOR_BYTES = 65536
MAX_PIT_BYTES = 16384
DEFAULT_SORT = [{"field": "id", "direction": "asc"}]


class SearchReadError(RuntimeError):
    """Safe code without query, credentials, source values or cursor contents."""


class InvalidSearchCursor(SearchReadError):
    pass


class SearchAuthorizationError(SearchReadError):
    pass


def _digest(value):
    return hashlib.sha256(json_text(value).encode("ascii")).hexdigest()


def _iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _pit(value):
    if not isinstance(value, str) or not value or not value.isascii() or len(value) > MAX_PIT_BYTES:
        raise SearchReadError("INVALID_SEARCH_PIT")
    return value


def _position(value):
    if not isinstance(value, list) or len(value) != 2 or type(value[1]) is not int or value[1] < 0:
        raise SearchReadError("INVALID_SEARCH_SORT_POSITION")
    try:
        owner = _uuid(value[0])
    except ProjectionError:
        raise SearchReadError("INVALID_SEARCH_SORT_POSITION") from None
    if value[0] != owner:
        raise SearchReadError("INVALID_SEARCH_SORT_POSITION")
    return owner, value[1]


def _total(value):
    if not isinstance(value, dict) or set(value) != {"value", "relation"} or type(value.get("value")) is not int or value["value"] < 0 or value.get("relation") not in {"eq", "gte"}:
        raise SearchReadError("INVALID_SEARCH_TOTAL")
    return value


def _complete_shards(value):
    return (isinstance(value, dict) and type(value.get("total")) is int and value["total"] >= 1
            and type(value.get("successful")) is int and value["successful"] == value["total"]
            and type(value.get("failed")) is int and value["failed"] == 0)


def _freeze_expiry(value, timestamp):
    """Freeze generated expiry ranges only; a person's literal name 'now' stays."""
    if isinstance(value, list):
        return [_freeze_expiry(child, timestamp) for child in value]
    if not isinstance(value, dict):
        return value
    output = {key: _freeze_expiry(child, timestamp) for key, child in value.items()}
    if "range" in output:
        for field, bounds in output["range"].items():
            if field.endswith(".expires_at"):
                output["range"][field] = {key: _iso(timestamp) if bound == "now" else bound for key, bound in bounds.items()}
    return output


class CanonicalSearchReader:
    def __init__(self, client: httpx.Client, *, base_url: str, alias: str, expected_cluster_uuid: str,
                 expected_index_uuid: str, cursor_key: bytes, max_response_bytes=2 * 1024 * 1024,
                 clock=time.time):
        if not isinstance(cursor_key, bytes):
            raise SearchReadError("PERSISTENT_CURSOR_KEY_REQUIRED")
        try:
            self.cipher = Fernet(cursor_key)
        except (ValueError, TypeError):
            raise SearchReadError("INVALID_CURSOR_KEY") from None
        self.target = ElasticProjection(client, base_url=base_url, alias=alias,
            expected_cluster_uuid=expected_cluster_uuid, expected_index_uuid=expected_index_uuid,
            max_response_bytes=max_response_bytes)
        self.destination = _digest({"url": self.target.base_url, "alias": alias,
                                   "cluster_uuid": expected_cluster_uuid, "index_uuid": expected_index_uuid,
                                   "projection_version": PROJECTION_VERSION})
        self.clock = clock

    @staticmethod
    def _authorization(principal_id, authorization):
        if not isinstance(principal_id, str) or not principal_id or len(principal_id) > 2048:
            raise SearchAuthorizationError("INVALID_SEARCH_PRINCIPAL")
        if not isinstance(authorization, dict) or authorization.get("active") is not True or authorization.get("can_search") is not True:
            raise SearchAuthorizationError("SEARCH_ACCESS_REVOKED")
        if not isinstance(authorization.get("revision"), str) or not authorization["revision"]:
            raise SearchAuthorizationError("SEARCH_AUTHORIZATION_REVISION_REQUIRED")
        if not isinstance(authorization.get("scopes"), list) or not authorization["scopes"] or any(not isinstance(scope, str) for scope in authorization["scopes"]):
            raise SearchAuthorizationError("SEARCH_SCOPES_REQUIRED")
        if len(json_text(authorization)) > 8192:
            raise SearchAuthorizationError("SEARCH_AUTHORIZATION_CONTEXT_TOO_LARGE")
        return _digest({"principal_id": principal_id, "authorization": authorization})

    def _decode(self, token, now, auth, *, kind):
        if not isinstance(token, str) or not token.startswith(CURSOR_PREFIX) or len(token) > MAX_CURSOR_BYTES or not token.isascii():
            raise InvalidSearchCursor("INVALID_OR_EXPIRED_SEARCH_CURSOR")
        try:
            raw = self.cipher.decrypt_at_time(token[len(CURSOR_PREFIX):].encode("ascii"), ttl=ABSOLUTE_TTL, current_time=now)
            state = json.loads(raw)
            if (not isinstance(state, dict) or state.get("kind") != kind or state.get("destination") != self.destination
                    or not isinstance(state.get("authorization"), str) or not secrets.compare_digest(state["authorization"], auth)
                    or any(type(state.get(key)) is not int for key in ("started_at", "expires_at", "deadline_at", "seen"))
                    or state["started_at"] > now or now >= state["expires_at"] or now >= state["deadline_at"]
                    or not state["started_at"] < state["expires_at"] <= state["deadline_at"] <= state["started_at"] + ABSOLUTE_TTL
                    or state["seen"] < 0):
                raise ValueError()
            _pit(state.get("pit_id"))
            if state.get("position") is not None:
                _position(state["position"])
            if state.get("total") is not None:
                _total(state["total"])
            if kind == "search" and (state["seen"] == 0 or state.get("position") is None or state.get("total") is None):
                raise ValueError()
            return state
        except (InvalidToken, ValueError, TypeError, KeyError, SearchReadError):
            raise InvalidSearchCursor("INVALID_OR_EXPIRED_SEARCH_CURSOR") from None

    def _encode(self, state, now, kind):
        payload = json.dumps({**state, "kind": kind}, sort_keys=True, separators=(",", ":")).encode("ascii")
        token = CURSOR_PREFIX + self.cipher.encrypt_at_time(payload, current_time=now).decode("ascii")
        if len(token) > MAX_CURSOR_BYTES:
            raise SearchReadError("SEARCH_CURSOR_TOO_LARGE")
        return token

    def _request(self, method, path, body=None):
        try:
            response = self.target._request(method, path, **({"json": body} if body is not None else {}))
            if response.status_code == 404 and path.startswith("/_search"):
                raise InvalidSearchCursor("SEARCH_PIT_EXPIRED_RESTART_REQUIRED")
            if response.status_code not in {200, 201}:
                raise SearchReadError("SEARCH_READ_HTTP_ERROR")
            return self.target._json(response)
        except ProjectionError as exc:
            raise SearchReadError(str(exc)) from None

    def search(self, criteria: dict, *, principal_id: str, authorization: dict, page_size=50,
               entity_type=None, include_pending=False, include_invalid=False, sort=None, cursor=None):
        auth = self._authorization(principal_id, authorization)
        if type(page_size) is not int or not 1 <= page_size <= 100:
            raise SearchReadError("SEARCH_PAGE_SIZE_OUT_OF_RANGE")
        if sort is not None and sort != DEFAULT_SORT:
            raise SearchReadError("SEARCH_SORT_NOT_IMPLEMENTED")
        try:
            compiled = build_query(criteria, entity_type=entity_type, include_pending=include_pending, include_invalid=include_invalid)
        except ProjectionError as exc:
            raise SearchReadError(str(exc)) from None
        context = _digest({"criteria": criteria, "entity_type": entity_type, "include_pending": include_pending,
                           "include_invalid": include_invalid, "sort": DEFAULT_SORT, "page_size": page_size})
        now = int(self.clock())
        if cursor is None:
            state = {"destination": self.destination, "authorization": auth, "context": context,
                     "started_at": now, "expires_at": now + IDLE_TTL, "deadline_at": now + ABSOLUTE_TTL,
                     "seen": 0, "position": None, "total": None}
        else:
            state = self._decode(cursor, now, auth, kind="search")
            if not isinstance(state.get("context"), str) or not secrets.compare_digest(state["context"], context):
                raise InvalidSearchCursor("SEARCH_CURSOR_CONTEXT_CHANGED")
        try:
            self.target.verify_target()
        except ProjectionError as exc:
            raise SearchReadError(str(exc)) from None
        keep_alive = str(min(IDLE_TTL, state["deadline_at"] - now)) + "s"
        if cursor is None:
            opened = self._request("POST", f"/{self.target.alias}/_pit?keep_alive={keep_alive}&allow_partial_search_results=false")
            state["pit_id"] = _pit(opened.get("id"))
            if not _complete_shards(opened.get("_shards")):
                # There is no continuation to preserve if opening was partial.
                self._release_pit(state["pit_id"])
                raise SearchReadError("SEARCH_PIT_NOT_COMPLETE")
        compiled = _freeze_expiry(compiled, state["started_at"])
        compiled.update(size=page_size + 1, track_total_hits=10000,
                        sort=[{"id": "asc"}, {"_shard_doc": "asc"}],
                        pit={"id": state["pit_id"], "keep_alive": keep_alive})
        if state["position"] is not None:
            compiled["search_after"] = state["position"]
        # Any error keeps an existing PIT/cursor usable until its original TTL.
        # A first-page failure has no returned cursor, so release that new PIT.
        try:
            response = self._request("POST", "/_search?allow_partial_search_results=false", compiled)
            if "pit_id" in response:
                state["pit_id"] = _pit(response["pit_id"])
            if response.get("timed_out") is not False or response.get("terminated_early", False) is not False or not _complete_shards(response.get("_shards")):
                raise SearchReadError("INCOMPLETE_SEARCH_PAGE")
            hits_body = response.get("hits")
            if not isinstance(hits_body, dict):
                raise SearchReadError("INVALID_SEARCH_PAGE")
            total = _total(hits_body.get("total"))
            if state["total"] is not None and total != state["total"]:
                raise SearchReadError("SEARCH_SNAPSHOT_TOTAL_CHANGED")
            hits = hits_body.get("hits")
            if not isinstance(hits, list) or len(hits) > page_size + 1:
                raise SearchReadError("INVALID_SEARCH_PAGE")
            records, positions, seen_ids = [], [], set()
            previous = _position(state["position"]) if state["position"] is not None else None
            for hit in hits:
                if not isinstance(hit, dict) or hit.get("_index") != self.target.index:
                    raise SearchReadError("INVALID_SEARCH_HIT")
                position = _position(hit.get("sort"))
                fields = hit.get("fields")
                if not isinstance(fields, dict) or set(fields) != {"id", "record_version"} or fields["id"] != [position[0]] or hit.get("_id") != position[0] or not isinstance(fields["record_version"], list) or len(fields["record_version"]) != 1:
                    raise SearchReadError("INVALID_SEARCH_HIT")
                if position[0] in seen_ids or (previous is not None and (position <= previous or position[0] <= previous[0])):
                    raise SearchReadError("DUPLICATED_OR_UNORDERED_SEARCH_HIT")
                try:
                    version = _version(fields["record_version"][0])
                except ProjectionError:
                    raise SearchReadError("INVALID_SEARCH_HIT_VERSION") from None
                records.append({"id": position[0], "record_version": version})
                positions.append(list(position))
                previous = position
                seen_ids.add(position[0])
            more = len(records) > page_size
            if total["relation"] == "eq" and (state["seen"] + len(records) > total["value"] or (not more and state["seen"] + len(records) != total["value"])):
                raise SearchReadError("SEARCH_COUNT_RECONCILIATION_FAILED")
            if not more and state["seen"] + len(records) < total["value"]:
                raise SearchReadError("SEARCH_COUNT_RECONCILIATION_FAILED")
            records = records[:page_size]
            state.update(total=total, seen=state["seen"] + len(records),
                         position=positions[len(records)-1] if records else state["position"],
                         expires_at=min(now + IDLE_TTL, state["deadline_at"]))
            next_cursor = self._encode(state, now, "search") if more else None
            release_cursor = self._encode(state, now, "release")
            return {"items": records, "returned": len(records), "seen": state["seen"], "total": deepcopy(total),
                    "has_more": more, "next_cursor": next_cursor, "release_cursor": release_cursor,
                    "snapshot": {"started_at": _iso(state["started_at"]), "expires_at": _iso(state["expires_at"]),
                                 "deadline_at": _iso(state["deadline_at"]), "projection_version": PROJECTION_VERSION}}
        except Exception:
            if cursor is None:
                self._release_pit(state["pit_id"])
            raise

    def _release_pit(self, pit_id):
        try:
            response = self._request("DELETE", "/_pit", {"id": pit_id})
            return response.get("succeeded") is True
        except SearchReadError:
            return False

    def close(self, release_cursor, *, principal_id, authorization):
        auth = self._authorization(principal_id, authorization)
        state = self._decode(release_cursor, int(self.clock()), auth, kind="release")
        try:
            self.target.verify_target()
        except ProjectionError as exc:
            raise SearchReadError(str(exc)) from None
        return {"released": self._release_pit(state["pit_id"])}

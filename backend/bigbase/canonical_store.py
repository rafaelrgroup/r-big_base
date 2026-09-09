"""Explicit PostgreSQL repository for prepared field atoms, separate from SQLite.

No authentication, HTTP, Elasticsearch reads or document normalization live here.
The caller supplies lossless atoms and a validated identity candidate; every batch
commits observations, projections, outbox and the source cursor together.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import secrets
from typing import Any, Iterator
from uuid import UUID, uuid4, uuid5

import psycopg
from psycopg import sql
from psycopg.rows import dict_row


NAMESPACE = UUID("972eb69b-f7f3-5517-b733-09d2e6a5376e")
SCHEMA_VERSION = 1
MAX_BATCH_RECORDS = 1000
MAX_PAGE_SIZE = 200
MAX_PAGE_BYTES = 8 * 1024 * 1024
PAGE_ENVELOPE_BYTES = 1024
ITEM_COLLECTION_CURSOR_BYTES = 256
CURSOR_IDLE_TTL = timedelta(minutes=15)
CURSOR_MAX_TTL = timedelta(hours=1)
DDL_PATH = Path(__file__).resolve().parents[2] / "infra" / "sql" / "001_canonical.sql"


class CanonicalError(ValueError):
    """Safe repository contract error, with no source values in its message."""


class IdentityConflict(CanonicalError):
    pass


class VersionConflict(CanonicalError):
    pass


class CheckpointConflict(CanonicalError):
    pass


class IdempotencyConflict(CanonicalError):
    pass


class DeploymentMismatch(CanonicalError):
    pass


class InvalidCursor(CanonicalError):
    pass


class CanonicalRowTooLarge(CanonicalError):
    pass


def json_text(value: Any) -> str:
    """Lossless JSON value text; never silently accept a binary float.

    TEXT contains ASCII JSON escapes, so PostgreSQL's jsonb NUL/surrogate
    restrictions cannot discard an imported string. Exact numeric source lexemes
    supplied as input_json are retained separately, without reserialization.
    """
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise CanonicalError("Non-finite decimal is not a JSON value")
        literal = getattr(value, "json_lexeme", str(value))
        if not isinstance(literal, str) or not re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?", literal) or Decimal(literal) != value:
            raise CanonicalError("Invalid exact decimal lexeme")
        return literal
    if isinstance(value, float):
        raise CanonicalError("Binary floats are not lossless input; parse JSON with Decimal")
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=True)
    if isinstance(value, list):
        return "[" + ",".join(json_text(v) for v in value) + "]"
    if isinstance(value, dict) and all(isinstance(k, str) for k in value):
        return "{" + ",".join(json_text(k) + ":" + json_text(value[k]) for k in sorted(value)) + "}"
    raise CanonicalError("Unsupported value type in prepared input")


def decode(text: str) -> Any:
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise CanonicalError("Duplicate object key in JSON literal")
            result[key] = value
        return result
    try:
        return json.loads(text, parse_float=Decimal,
                          object_pairs_hook=unique_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(CanonicalError("Non-finite JSON")))
    except InvalidOperation:
        raise CanonicalError('Decimal exponent exceeds the exact parser range') from None


def digest(value: Any) -> str:
    return hashlib.sha256(json_text(value).encode("ascii")).hexdigest()


def identifier(kind: str, value: Any) -> UUID:
    return uuid5(NAMESPACE, kind + ":" + digest(value))


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise CanonicalError(f"{label} must be a nonempty string")
    return value


def _token(value: Any, label: str) -> str:
    value = _text(value, label)
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", value):
        raise CanonicalError(f"{label} must be an ASCII token")
    return value


def _date(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise CanonicalError("Invalid source date") from exc
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise CanonicalError("Source dates require an explicit timezone")
    return value.astimezone(timezone.utc)


def _literal(fact: dict, key: str) -> str:
    literal = fact.get(key + "_json")
    value = fact[key + "_value"]
    # Validate the supplied value too: a pre-rounded float cannot become trusted
    # merely by supplying an apparently exact JSON literal beside it.
    canonical = json_text(value)
    if literal is None:
        return canonical
    if not isinstance(literal, str) or not literal.isascii() or "\x00" in literal:
        raise CanonicalError("Field JSON literal must be ASCII with escaped Unicode")
    try:
        parsed = decode(literal)
    except (ValueError, TypeError) as exc:
        raise CanonicalError("Invalid field JSON literal") from exc
    if not _same_json_value(parsed, value):
        raise CanonicalError("Field JSON literal differs from its declared value")
    return literal


def _same_json_value(left: Any, right: Any) -> bool:
    """Compare JSON types recursively; Python bool/int equality is not suitable.

    JSON has one numeric type. Decimal/int representations of the same exact
    number can compare equal; the original input_type and literal are retained.
    """
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, Decimal)) and isinstance(right, (int, Decimal)):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, list):
        return len(left) == len(right) and all(_same_json_value(a,b) for a,b in zip(left,right))
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_same_json_value(left[key],right[key]) for key in left)
    return left == right


def _input_type(value: Any) -> str:
    if value is None: return "null"
    if type(value) is bool: return "boolean"
    if type(value) is int: return "integer"
    if isinstance(value, Decimal): return "decimal"
    if isinstance(value, str): return "text"
    if isinstance(value, dict) and not value: return "empty_object"
    if isinstance(value, list) and not value: return "empty_array"
    return "unsupported"


def _prepare(record: dict) -> dict:
    if not isinstance(record, dict):
        raise CanonicalError("A prepared record must be an object")
    source = _text(record.get("source_id"), "source_id")
    external = _text(record.get("source_record_id"), "source_record_id")
    adapter = _text(record.get("adapter_version"), "adapter_version")
    entity_type = record.get("entity_type", "person")
    if entity_type not in {"person", "company"}:
        raise CanonicalError("Unknown entity_type")
    record_hash = record.get("record_hash", "")
    if not isinstance(record_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", record_hash):
        raise CanonicalError("record_hash must be a lowercase SHA-256")
    identities = [("source", ["source", source, external])]
    candidate = record.get("identity_candidate")
    if candidate is not None:
        if not isinstance(candidate, dict) or not {"type", "country", "value"}.issubset(candidate) or set(candidate) - {"type", "country", "value", "evidence_paths"}:
            raise CanonicalError("Identity candidate requires type, country and value")
        document_type = _token(candidate["type"], "document type")
        country = _token(candidate["country"], "document country")
        number = _text(candidate["value"], "document value")
        # The adapter validates the country/document rule. The registry preserves
        # its result exactly and includes type/country in the unique key.
        identities.append(("document", ["document", country, document_type, number]))
    facts = record.get("facts")
    if not isinstance(facts, list) or not facts:
        raise CanonicalError("A prepared record requires at least one field atom")
    prepared_facts, seen, source_paths = [], set(), set()
    for fact in facts:
        if not isinstance(fact, dict):
            raise CanonicalError("Each atom must be an object")
        fact_id = _text(fact.get("id"), "fact id")
        if fact_id in seen:
            raise CanonicalError("Duplicate fact id in one record")
        seen.add(fact_id)
        for name in ("source_path", "target_path", "item_key"):
            if not isinstance(fact.get(name), str):
                raise CanonicalError(f"{name} must be a string")
        if fact["source_path"] in source_paths:
            raise CanonicalError("Duplicate source leaf path in one record")
        source_paths.add(fact["source_path"])
        kind = _token(fact.get("target_kind"), "target_kind")
        input_type = _token(fact.get("input_type"), "input_type")
        if "input_value" not in fact or "normalized_value" not in fact:
            raise CanonicalError("Atoms require explicit input_value and normalized_value, including null")
        if isinstance(fact["input_value"], (dict, list)) and fact["input_value"]:
            raise CanonicalError("Input atoms cannot contain a copied source subtree")
        if input_type != _input_type(fact["input_value"]):
            raise CanonicalError("input_type differs from the actual source atom type")
        flags = fact.get("flags", {})
        if not isinstance(flags, dict):
            raise CanonicalError("flags must be an object")
        for flag_name, flag in flags.items():
            _token(flag_name, "flag name")
            if not isinstance(flag, dict) or "value" not in flag or not (flag["value"] is None or type(flag["value"]) is bool):
                raise CanonicalError("Each flag requires an explicit boolean or null value")
            _date(flag.get("source_updated_at"))
            _date(flag.get("observed_at"))
        metadata = {k: v for k, v in fact.items() if k not in {
            "id", "source_path", "target_path", "target_kind", "item_key", "input_value", "input_json",
            "normalized_value", "normalized_json", "flags", "source_updated_at", "observed_at", "status",
        }}
        prepared_facts.append({
            "raw": fact, "id": fact_id, "kind": kind,
            "input_json": _literal(fact, "input"), "normalized_json": _literal(fact, "normalized"),
            "input_type": input_type, "input_encoding": _token(fact.get("input_encoding", "canonical_value"), "input_encoding"),
            "metadata_json": json_text(metadata),
            "status": _token(fact.get("status", "unknown"), "status"),
            # Dates are never promoted from a whole record to unrelated fields.
            "source_updated_at": _date(fact.get("source_updated_at")),
            "observed_at": _date(fact.get("observed_at")),
        })
    containers = record.get("containers", [])
    if not isinstance(containers, list):
        raise CanonicalError("containers must be an array")
    container_paths = set()
    for container in containers:
        if not isinstance(container, dict) or not isinstance(container.get("source_path"), str):
            raise CanonicalError("Invalid source container")
        if container.get("type") not in {"object", "array"} or type(container.get("length")) is not int or container["length"] < 0:
            raise CanonicalError("Invalid source container type or length")
        if container["source_path"] in container_paths:
            raise CanonicalError("Duplicate source container path")
        container_paths.add(container["source_path"])
    operation_key = [source, external, record.get("source_version"), adapter, record.get("normalizer_version"), record_hash]
    return {"source": source, "external": external, "adapter": adapter, "entity_type": entity_type,
            "record_hash": record_hash, "operation_id": identifier("operation", operation_key),
            "source_version": record.get("source_version"), "prepared_hash": digest(record),
            "normalizer_version": record.get("normalizer_version"),
            "identities": [(digest(key), kind, json_text(key)) for kind, key in identities],
            "facts": prepared_facts, "containers": containers}


def _precedence(previous: dict | None, effective: datetime | None, received: datetime, event_id: UUID) -> tuple[bool, str | None]:
    if effective is not None and effective > received + timedelta(minutes=5):
        return False, "future_date"
    if previous is None:
        return True, None
    old = previous["effective_at"]
    if old is not None and effective is None:
        return False, "undated_against_dated"
    floor = datetime.min.replace(tzinfo=timezone.utc)
    incoming = (effective or floor, received, str(event_id))
    existing = (old or floor, previous["received_at"], str(previous["observation_id"]))
    return (True, None) if incoming > existing else (False, "older_observation")


def prepare_canonical_record(record: dict) -> dict:
    """Pure shared storage contract for ingestion and independent readback.

    The returned internal structure may contain source atoms and datetimes;
    callers must not serialize it as a public operational report.
    """
    return _prepare(record)


class CanonicalStore:
    def __init__(self, dsn: str, schema: str = "bigbase_canonical"):
        if not dsn or not re.fullmatch(r"[a-z][a-z0-9_]{0,50}", schema):
            raise CanonicalError("Explicit DSN and a safe schema name are required")
        self.dsn, self.schema = dsn, schema

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=10) as connection:
            connection.execute(sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(sql.Identifier(self.schema)))
            connection.execute("SET LOCAL lock_timeout = '15s'")
            yield connection

    def initialize(self, environment: str = "synthetic") -> dict:
        if environment not in {"synthetic", "staging", "production"}:
            raise DeploymentMismatch("Unknown deployment environment")
        with self.connection() as c:
            version = c.execute("SELECT current_setting('server_version_num')::integer AS version").fetchone()["version"]
            if not 180000 <= version < 190000:
                raise DeploymentMismatch("This schema is validated for PostgreSQL 18 only")
            c.execute("SELECT pg_advisory_xact_lock(%s)", (self._lock_key(["schema", self.schema]),))
            c.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(self.schema)))
            exists = c.execute("SELECT to_regclass(%s) AS name", (f"{self.schema}.canonical_meta",)).fetchone()["name"]
            if exists:
                info = self._deployment(c)
                if info["environment"] != environment:
                    raise DeploymentMismatch("An existing deployment environment is immutable")
                return info
            occupied = c.execute("SELECT 1 FROM pg_catalog.pg_class r JOIN pg_catalog.pg_namespace n ON n.oid=r.relnamespace WHERE n.nspname=%s LIMIT 1", (self.schema,)).fetchone()
            if occupied:
                raise DeploymentMismatch("Refusing to initialize an unrecognized nonempty schema")
            ddl = DDL_PATH.read_text()
            c.execute(ddl)
            c.execute("INSERT INTO canonical_meta(deployment_id,environment,schema_version,schema_sha256) VALUES(%s,%s,%s,%s)",
                      (uuid4(), environment, SCHEMA_VERSION, hashlib.sha256(ddl.encode()).hexdigest()))
            return self._deployment(c)

    @staticmethod
    def _deployment(c) -> dict:
        row = c.execute("SELECT * FROM canonical_meta WHERE singleton").fetchone()
        if not row or row["schema_version"] != SCHEMA_VERSION:
            raise DeploymentMismatch("Unsupported or missing canonical deployment metadata")
        if row.get("schema_sha256") != hashlib.sha256(DDL_PATH.read_bytes()).hexdigest():
            raise DeploymentMismatch("Existing schema differs from the explicit DDL version; migration is required")
        connection_info = c.execute("SELECT current_schema() AS schema,current_database() AS database,current_user AS database_user,current_setting('server_version_num')::integer AS server_version_num,inet_server_addr()::text AS server_address,current_setting('port')::integer AS server_port").fetchone()
        return {**row, **connection_info, "deployment_id": str(row["deployment_id"]), "created_at": row["created_at"].isoformat()}

    def deployment_info(self) -> dict:
        with self.connection() as c:
            return self._deployment(c)

    @staticmethod
    def _lock_key(value: Any) -> int:
        return int.from_bytes(bytes.fromhex(digest(value))[:8], "big", signed=True)

    @staticmethod
    def _job(row: dict) -> dict:
        result = dict(row)
        result["id"] = str(result["id"])
        for key in ("job_key", "source_id", "metadata", "cursor", "error", "verification"):
            raw = result.pop(key + "_json")
            result[key] = decode(raw) if raw is not None else None
        for key in ("created_at", "updated_at"):
            result[key] = result[key].isoformat()
        return result

    def create_job(self, job_key: str, source_id: str, metadata: dict | None = None) -> dict:
        _text(job_key, "job_key")
        _text(source_id, "source_id")
        if metadata is not None and not isinstance(metadata, dict):
            raise CanonicalError("Job metadata must be an object")
        job_id = identifier("job", job_key)
        metadata_json = json_text(metadata or {})
        with self.connection() as c:
            self._deployment(c)
            c.execute("INSERT INTO migration_jobs(id,job_key_json,source_id_json,metadata_json,status,created_at,updated_at) VALUES(%s,%s,%s,%s,'pending',clock_timestamp(),clock_timestamp()) ON CONFLICT DO NOTHING",
                      (job_id, json_text(job_key), json_text(source_id), metadata_json))
            row = c.execute("SELECT * FROM migration_jobs WHERE id=%s FOR UPDATE", (job_id,)).fetchone()
            if row["job_key_json"] != json_text(job_key) or row["source_id_json"] != json_text(source_id) or row["metadata_json"] != metadata_json:
                raise IdempotencyConflict("Job key was already used with different pinned source metadata")
            return self._job(row)

    def get_job(self, job_id: str) -> dict:
        with self.connection() as c:
            self._deployment(c)
            row = c.execute("SELECT * FROM migration_jobs WHERE id=%s", (UUID(str(job_id)),)).fetchone()
            if not row:
                raise CanonicalError("Migration job does not exist")
            return self._job(row)

    def finish_job(self, job_id: str, expected_checkpoint: int, status: str = "completed", error: Any = None,
                   verification: dict | None = None) -> dict:
        if status not in {"completed", "failed", "cancelled"}:
            raise CanonicalError("Invalid terminal job status")
        if type(expected_checkpoint) is not int or expected_checkpoint < 0:
            raise CheckpointConflict("Expected checkpoint must be a nonnegative integer")
        with self.connection() as c:
            deployment = self._deployment(c)
            row = c.execute("SELECT * FROM migration_jobs WHERE id=%s FOR UPDATE", (UUID(str(job_id)),)).fetchone()
            if not row or row["checkpoint"] != expected_checkpoint:
                raise CheckpointConflict("Migration checkpoint changed")
            encoded_error = json_text(error) if error is not None else None
            if verification is not None and (not isinstance(verification,dict) or len(json_text(verification)) > 65536):
                raise CanonicalError("Verification must be an object of at most 64 KiB")
            proof = verification if verification is not None else decode(row["verification_json"]) if row["verification_json"] else None
            metadata = decode(row["metadata_json"])
            if status == "completed" and metadata.get("completion_contract") not in {None,"source_destination_reconciliation_v1"}:
                raise CanonicalError("Unsupported migration completion proof contract")
            if status == "completed" and metadata.get("completion_contract") == "source_destination_reconciliation_v1":
                if (not isinstance(proof,dict) or proof.get("proof_version") != "canonical-readback-2026-09-08.1"
                    or proof.get("state") != "verified" or proof.get("complete") is not True or proof.get("passed") is not True
                    or proof.get("coverage_complete") is not True or type(proof.get("divergences")) is not int or proof["divergences"] != 0
                    or type(proof.get("records_checked")) is not int or proof["records_checked"] != row["checkpoint"]
                    or type(proof.get("expected_records")) is not int or proof["expected_records"] != row["checkpoint"]
                    or proof["expected_records"] != metadata.get("expected_records") or row["records_processed"] != row["checkpoint"]
                    or type(proof.get("records_matched")) is not int or proof["records_matched"] != row["checkpoint"]
                    or proof.get("destination_deployment_id") != deployment["deployment_id"]
                    or proof.get("source_identity_sha256") != digest(metadata.get("source"))
                    or proof.get("adapter_version") != metadata.get("adapter_version") or proof.get("normalizer_version") != metadata.get("normalizer_version")
                    or not isinstance(proof.get("evidence_sha256"),str) or not re.fullmatch(r"[0-9a-f]{64}",proof["evidence_sha256"])):
                    raise CanonicalError("Completed migration requires a matching durable destination reconciliation proof")
            encoded_proof = json_text(proof) if proof is not None else None
            if row["status"] in {"completed", "failed", "cancelled"}:
                if row["status"] != status or row["error_json"] != encoded_error or row["verification_json"] != encoded_proof:
                    raise CheckpointConflict("Terminal job cannot be silently reopened")
                return self._job(row)
            row = c.execute("UPDATE migration_jobs SET status=%s,error_json=%s,verification_json=%s,updated_at=clock_timestamp() WHERE id=%s RETURNING *", (status, encoded_error, encoded_proof, UUID(str(job_id)))).fetchone()
            return self._job(row)

    def apply_batch(self, records: list[dict], *, job_id: str, expected_checkpoint: int,
                    next_checkpoint: int, actor_id: str, next_cursor: dict | None = None) -> dict:
        _text(actor_id, "actor_id")
        if not isinstance(records, list) or not 1 <= len(records) <= MAX_BATCH_RECORDS:
            raise CanonicalError("A batch requires between 1 and 1000 prepared records")
        if type(expected_checkpoint) is not int or type(next_checkpoint) is not int or expected_checkpoint < 0 or next_checkpoint != expected_checkpoint + len(records):
            raise CheckpointConflict("Checkpoint must advance by the exact record count")
        if next_cursor is not None and not isinstance(next_cursor, dict):
            raise CanonicalError("Source cursor must be an object or null")
        if next_cursor is not None and "seen" in next_cursor and (type(next_cursor["seen"]) is not int or next_cursor["seen"] != next_checkpoint):
            raise CheckpointConflict("Source cursor seen count differs from the committed checkpoint")
        prepared = [prepare_canonical_record(record) for record in records]
        batch_hash = digest({"records": [r["prepared_hash"] for r in prepared], "next_cursor": next_cursor})
        job_uuid = UUID(str(job_id))
        with self.connection() as c:
            self._deployment(c)
            job = c.execute("SELECT * FROM migration_jobs WHERE id=%s FOR UPDATE", (job_uuid,)).fetchone()
            if not job:
                raise CanonicalError("Migration job does not exist")
            prior = c.execute("SELECT * FROM migration_batches WHERE job_id=%s AND expected_checkpoint=%s", (job_uuid, expected_checkpoint)).fetchone()
            if prior:
                if prior["batch_hash"] != batch_hash or prior["next_checkpoint"] != next_checkpoint:
                    raise IdempotencyConflict("Checkpoint already committed a different batch or cursor")
                return {**decode(prior["receipt_json"]), "replayed": True}
            if job["checkpoint"] != expected_checkpoint or job["status"] not in {"pending", "processing"}:
                raise CheckpointConflict("Migration checkpoint or status changed")
            if any(json_text(record["source"]) != job["source_id_json"] for record in prepared):
                raise CanonicalError("Batch source differs from the job's pinned source")
            keys = sorted({key for record in prepared for key, _, _ in record["identities"]})
            # Identity locks precede all entity locks and are globally ordered.
            for key in keys:
                c.execute("SELECT pg_advisory_xact_lock(%s)", (self._lock_key([self.schema, "identity", key]),))
            owner_rows = c.execute("SELECT DISTINCT owner_id FROM identity_keys WHERE key_hash=ANY(%s) ORDER BY owner_id", (keys,)).fetchall()
            if owner_rows:
                c.execute("SELECT owner_id FROM entities WHERE owner_id=ANY(%s) ORDER BY owner_id FOR UPDATE", ([r["owner_id"] for r in owner_rows],)).fetchall()
            operations, observations, entities = 0, 0, []
            for record in prepared:
                owner, created, count = self._apply_record(c, record, actor_id)
                operations += int(created)
                observations += count
                entities.append(str(owner))
            receipt = {"job_id": str(job_uuid), "checkpoint": next_checkpoint,
                       "records_processed": len(records), "operations_created": operations,
                       "observations_created": observations, "entity_ids": entities, "replayed": False}
            c.execute("INSERT INTO migration_batches(job_id,expected_checkpoint,next_checkpoint,batch_hash,receipt_json,created_at) VALUES(%s,%s,%s,%s,%s,clock_timestamp())",
                      (job_uuid, expected_checkpoint, next_checkpoint, batch_hash, json_text(receipt)))
            c.execute("UPDATE migration_jobs SET checkpoint=%s,cursor_json=%s,records_processed=records_processed+%s,operations_created=operations_created+%s,observations_created=observations_created+%s,status='processing',updated_at=clock_timestamp() WHERE id=%s",
                      (next_checkpoint, json_text(next_cursor), len(records), operations, observations, job_uuid))
            return receipt

    def apply_enrichment(self, record: dict, *, actor_id: str, request_key: str,
                         expected_version: int, expected_owner: str | None = None, validate_record=None) -> dict:
        """One HTTP operation, including durable replay, in one PostgreSQL transaction.

        Identity locks follow the same order as migration batches. The receipt is
        reconstructed from immutable operations, never from the latest entity.
        No migration job or second copy of the entity is created.
        """
        _text(actor_id, 'actor_id')
        _text(request_key, 'request_key')
        if type(expected_version) is not int or not 0 <= expected_version < 2**63 - 1:
            raise CanonicalError('Invalid expected entity version')
        expected_owner = UUID(expected_owner) if expected_owner is not None else None
        prepared = prepare_canonical_record(record)
        prepared['operation_id'] = identifier('http-enrichment', [actor_id, request_key])
        prepared['prepared_hash'] = digest({'record': record, 'expected_version': expected_version,
                                           'expected_owner': str(expected_owner) if expected_owner else None})
        with self.connection() as c:
            self._deployment(c)
            c.execute('SELECT pg_advisory_xact_lock(%s)',
                      (self._lock_key([self.schema, 'http-enrichment', str(prepared['operation_id'])]),))
            prior = c.execute('SELECT * FROM operations WHERE operation_id=%s', (prepared['operation_id'],)).fetchone()
            if prior:
                if prior['prepared_hash'] != prepared['prepared_hash']:
                    raise IdempotencyConflict('Idempotency key already committed different content')
                count = c.execute('SELECT count(*) AS n FROM observations WHERE owner_id=%s AND operation_id=%s',
                                  (prior['owner_id'], prior['operation_id'])).fetchone()['n']
                return {'id': str(prior['owner_id']), 'operation_id': str(prior['operation_id']),
                        'record_version': prior['entity_version'], 'observations_created': count, 'replayed': True}
            if validate_record is not None:
                validated = prepare_canonical_record(validate_record(c))
                validated.update(operation_id=prepared['operation_id'], prepared_hash=prepared['prepared_hash'])
                prepared = validated
            keys = sorted(key for key, _, _ in prepared['identities'])
            for key in keys:
                c.execute('SELECT pg_advisory_xact_lock(%s)', (self._lock_key([self.schema, 'identity', key]),))
            owners = c.execute('SELECT DISTINCT owner_id FROM identity_keys WHERE key_hash=ANY(%s) ORDER BY owner_id', (keys,)).fetchall()
            if len(owners) > 1:
                raise IdentityConflict('Identities belong to distinct entities')
            owner = owners[0]['owner_id'] if owners else None
            if expected_owner is not None and owner != expected_owner:
                raise IdentityConflict('Expected entity does not match the supplied identity')
            current = c.execute('SELECT version FROM entities WHERE owner_id=%s FOR UPDATE', (owner,)).fetchone() if owner else None
            if (current['version'] if current else 0) != expected_version:
                raise VersionConflict('Entity version changed; reopen before writing')
            owner, created, count = self._apply_record(c, prepared, actor_id)
            version = c.execute('SELECT entity_version FROM operations WHERE operation_id=%s', (prepared['operation_id'],)).fetchone()['entity_version']
            return {'id': str(owner), 'operation_id': str(prepared['operation_id']),
                    'record_version': version, 'observations_created': count, 'replayed': not created}

    def apply_validation(self, body: dict, *, owner_id: str, item_id: str, entity_type: str,
                         actor_id: str, request_key: str, api_key_id: str | None = None) -> dict:
        """Append only flags, with the entity lock, durable receipt and outbox."""
        from .canonical_validation import prepare_validation, CONTRACT_VERSION
        prepared = prepare_validation(body)
        owner, item = UUID(owner_id), UUID(item_id)
        _text(actor_id, 'actor_id'); _text(request_key, 'request_key')
        operation = identifier('http-validation', [actor_id, request_key])
        fingerprint = digest({'body': body, 'owner': str(owner), 'item': str(item),
                              'entity_type': entity_type, 'api_key_id': api_key_id})
        with self.connection() as c:
            self._deployment(c)
            c.execute('SELECT pg_advisory_xact_lock(%s)',
                      (self._lock_key([self.schema, 'http-validation', str(operation)]),))
            prior = c.execute('SELECT * FROM operations WHERE operation_id=%s', (operation,)).fetchone()
            if prior:
                if prior['prepared_hash'] != fingerprint:
                    raise IdempotencyConflict('Idempotency key already committed different content')
                return {'id': str(owner), 'operation_id': str(operation), 'record_version': prior['entity_version'],
                        'observations_created': len(prepared['flags']), 'replayed': True}
            entity = c.execute('SELECT * FROM entities WHERE owner_id=%s FOR UPDATE', (owner,)).fetchone()
            if not entity or entity['entity_type'] != entity_type:
                raise CanonicalError('Entity/collection does not exist')
            if entity['version'] != prepared['expected_version']:
                raise VersionConflict('Entity version changed')
            target = c.execute('SELECT * FROM observations WHERE owner_id=%s AND observation_id=%s',
                               (owner, UUID(prepared['value_observation_id']))).fetchone()
            if (not target or target['item_id'] != item or target['dimension'] != 'value'
                    or target['target_path_json'] != json_text(prepared['field_path'])
                    or isinstance(decode(target['normalized_json']), (dict, list))):
                raise CanonicalError('Reference must identify a scalar value on the exact item and field')
            item_row = c.execute('SELECT version FROM items WHERE owner_id=%s AND item_id=%s', (owner, item)).fetchone()
            received = c.execute('SELECT clock_timestamp() AS received').fetchone()['received']
            version = entity['version'] + 1
            c.execute('INSERT INTO operations(operation_id,owner_id,entity_version,source_id_json,source_record_id_json,source_version_json,record_hash,adapter_version_json,normalizer_version_json,prepared_hash,actor_id_json,received_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                      (operation, owner, version, json_text(prepared['source_id']), json_text(str(target['observation_id'])),
                       'null', digest(body), json_text(CONTRACT_VERSION), json_text('preserved-no-inference'),
                       fingerprint, json_text(actor_id), received))
            for sequence, (name, flag) in enumerate(prepared['flags'].items(), 1):
                binding = decode(target['metadata_json'])
                schema = {k: binding[k] for k in ('custom_field', 'field_id', 'field_definition_version', 'field_definition', 'field_definition_sha256', 'classification_state', 'canonical_search_state', 'catalog_environment', 'value_policy') if k in binding}
                evidence = {**flag, **schema, 'confirmed_value_json': target['normalized_json'],
                            'value_observation_id': str(target['observation_id']), 'api_key_id': api_key_id}
                fact = {'raw': {'target_path': prepared['field_path'], 'source_path': '/flags/'+name},
                        'id': digest([str(item), prepared['field_path'], name]), 'status': 'unknown',
                        'input_json': target['normalized_json']}
                self._observe(c, owner, item, operation, prepared['source_id'], actor_id, received, fact, name, evidence,
                              entity_version=version, operation_sequence=sequence, item_version=item_row['version']+1)
            c.execute('UPDATE items SET version=version+1,updated_at=%s WHERE owner_id=%s AND item_id=%s', (received, owner, item))
            c.execute('UPDATE entities SET version=%s,updated_at=%s WHERE owner_id=%s', (version, received, owner))
            c.execute('INSERT INTO outbox(event_id,operation_id,owner_id,entity_version,created_at) VALUES(%s,%s,%s,%s,%s)',
                      (identifier('outbox', str(operation)), operation, owner, version, received))
            return {'id': str(owner), 'operation_id': str(operation), 'record_version': version,
                    'observations_created': len(prepared['flags']), 'replayed': False}

    def apply_scalar_patch(self, body: dict, *, owner_id: str, item_id: str, entity_type: str,
                         actor_id: str, request_key: str, api_key_id: str | None = None) -> dict:
        """Append a scalar to an existing field, without changing identity keys."""
        from .canonical_scalar_patch import prepare_scalar_patch, CONTRACT_VERSION
        prepared = prepare_scalar_patch(body)
        owner, item = UUID(owner_id), UUID(item_id)
        _text(actor_id, 'actor_id'); _text(request_key, 'request_key')
        operation = identifier('http-scalar-patch', [actor_id, request_key])
        fingerprint = digest({'body': body, 'owner': str(owner), 'item': str(item),
                              'entity_type': entity_type, 'api_key_id': api_key_id})
        with self.connection() as c:
            self._deployment(c)
            c.execute('SELECT pg_advisory_xact_lock(%s)',
                      (self._lock_key([self.schema, 'http-scalar-patch', str(operation)]),))
            prior = c.execute('SELECT * FROM operations WHERE operation_id=%s', (operation,)).fetchone()
            if prior:
                if prior['prepared_hash'] != fingerprint:
                    raise IdempotencyConflict('Idempotency key already committed different content')
                return {'id': str(owner), 'operation_id': str(operation), 'record_version': prior['entity_version'],
                        'observations_created': 1, 'replayed': True}
            entity = c.execute('SELECT * FROM entities WHERE owner_id=%s FOR UPDATE', (owner,)).fetchone()
            if not entity or entity['entity_type'] != entity_type:
                raise CanonicalError('Entity/collection does not exist')
            if entity['version'] != prepared['expected_version']:
                raise VersionConflict('Entity version changed')
            target = c.execute("SELECT * FROM field_state WHERE owner_id=%s AND item_id=%s AND target_path_hash=%s AND dimension='value'",
                               (owner, item, digest(prepared['field_path']))).fetchone()
            if (not target or target['target_path_json'] != json_text(prepared['field_path'])
                    or isinstance(decode(target['value_json']), (dict, list))):
                raise CanonicalError('Target must identify an existing scalar value on the exact item and field')
            item_row = c.execute('SELECT version,kind FROM items WHERE owner_id=%s AND item_id=%s', (owner, item)).fetchone()
            if not item_row or item_row['kind'] == 'relationship':
                raise CanonicalError('Relationships require their own contract')
            received = c.execute('SELECT clock_timestamp() AS received').fetchone()['received']
            version = entity['version'] + 1
            c.execute('INSERT INTO operations(operation_id,owner_id,entity_version,source_id_json,source_record_id_json,source_version_json,record_hash,adapter_version_json,normalizer_version_json,prepared_hash,actor_id_json,received_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                      (operation, owner, version, json_text(prepared['source_id']), json_text(str(target['observation_id'])),
                       'null', digest(body), json_text(CONTRACT_VERSION), json_text('preserved-no-inference'),
                       fingerprint, json_text(actor_id), received))
            literal = json_text(prepared['value'])
            metadata = {name: prepared[name] for name in ('reason',) if name in prepared}
            metadata.update(api_key_id=api_key_id, normalization='preserved-no-inference',
                            target_observation_id=str(target['observation_id']), identity_registry_updated=False)
            fact = {'raw': {'target_path': prepared['field_path'], 'source_path': '/value'},
                    'id': digest([str(item), prepared['field_path']]), 'status': 'unknown',
                    'input_json': literal, 'normalized_json': literal, 'input_type': _input_type(prepared['value']),
                    'input_encoding': 'canonical_value', 'metadata_json': json_text(metadata),
                    'source_updated_at': _date(prepared.get('source_updated_at')),
                    'observed_at': _date(prepared.get('observed_at'))}
            if item_row['kind'] == 'custom':
                from .canonical_fields import guard_custom_field
                guard_custom_field(c, owner, item, fact)
            if item_row['kind'] == 'username':
                from .canonical_username import guard_username_platform
                guard_username_platform(c, owner, item, fact)
            self._observe(c, owner, item, operation, prepared['source_id'], actor_id, received, fact,
                          entity_version=version, operation_sequence=1, item_version=item_row['version']+1)
            c.execute('UPDATE items SET version=version+1,updated_at=%s WHERE owner_id=%s AND item_id=%s', (received, owner, item))
            c.execute('UPDATE entities SET version=%s,updated_at=%s WHERE owner_id=%s', (version, received, owner))
            c.execute('INSERT INTO outbox(event_id,operation_id,owner_id,entity_version,created_at) VALUES(%s,%s,%s,%s,%s)',
                      (identifier('outbox', str(operation)), operation, owner, version, received))
            return {'id': str(owner), 'operation_id': str(operation), 'record_version': version,
                    'observations_created': 1, 'replayed': False}

    def _apply_record(self, c, record: dict, actor_id: str) -> tuple[UUID, bool, int]:
        owners = set()
        for key, _, identity_json in record["identities"]:
            row = c.execute("SELECT * FROM identity_keys WHERE key_hash=%s", (key,)).fetchone()
            if row:
                if row["identity_json"] != identity_json:
                    raise IdentityConflict("Identity hash collision requires manual investigation")
                owners.add(row["owner_id"])
        if len(owners) > 1:
            raise IdentityConflict("Prepared identities belong to distinct entities; explicit resolution is required")
        # Document identity has deterministic priority for a new entity.
        preferred = record["identities"][-1][0]
        owner = next(iter(owners)) if owners else identifier("entity", preferred)
        received = c.execute("SELECT clock_timestamp() AS received").fetchone()["received"]
        c.execute("INSERT INTO entities(owner_id,entity_type,created_at,updated_at) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING", (owner, record["entity_type"], received, received))
        entity = c.execute("SELECT * FROM entities WHERE owner_id=%s FOR UPDATE", (owner,)).fetchone()
        if entity["entity_type"] != record["entity_type"]:
            raise IdentityConflict("Identity is already associated with another entity type")
        entity_version = entity["version"] + 1
        for _, kind, identity_json in record["identities"]:
            incoming = decode(identity_json)
            if kind == "document" and incoming[1:3] in (["BR","CPF"],["BR","CNPJ"]):
                existing_documents = c.execute("SELECT identity_json FROM identity_keys WHERE owner_id=%s AND kind='document'", (owner,)).fetchall()
                for existing_document in existing_documents:
                    known = decode(existing_document["identity_json"])
                    if known[:3] == incoming[:3] and known[3] != incoming[3]:
                        raise IdentityConflict("Source alias already has a different unique Brazilian document; explicit resolution is required")
        for key, kind, identity_json in record["identities"]:
            c.execute("INSERT INTO identity_keys(key_hash,kind,identity_json,owner_id,created_at) VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", (key, kind, identity_json, owner, received))
        operation_id = record["operation_id"]
        existing = c.execute("SELECT * FROM operations WHERE operation_id=%s", (operation_id,)).fetchone()
        if existing:
            if existing["prepared_hash"] != record["prepared_hash"] or existing["owner_id"] != owner:
                raise IdempotencyConflict("Operation identity was reused with different prepared facts")
            return owner, False, 0
        c.execute("INSERT INTO operations(operation_id,owner_id,entity_version,source_id_json,source_record_id_json,source_version_json,record_hash,adapter_version_json,normalizer_version_json,prepared_hash,actor_id_json,received_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                  (operation_id, owner, entity_version, json_text(record["source"]), json_text(record["external"]), json_text(record["source_version"]), record["record_hash"], json_text(record["adapter"]), json_text(record["normalizer_version"]), record["prepared_hash"], json_text(actor_id), received))
        count, touched_items = 0, set()
        for fact in record["facts"]:
            raw = fact["raw"]
            item_id = identifier("item", [str(owner), fact["kind"], raw["item_key"]])
            c.execute("INSERT INTO items(owner_id,item_id,kind,item_key_hash,item_key_json,created_version,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                      (owner, item_id, fact["kind"], digest(raw["item_key"]), json_text(raw["item_key"]), entity_version, received, received))
            item = c.execute("SELECT * FROM items WHERE owner_id=%s AND item_id=%s", (owner, item_id)).fetchone()
            if item is None or item["item_key_json"] != json_text(raw["item_key"]) or item["kind"] != fact["kind"]:
                raise IdentityConflict("Item hash collision requires manual investigation")
            if fact['kind'] == 'custom':
                from .canonical_fields import guard_custom_field
                guard_custom_field(c, owner, item_id, fact)
            if fact['kind'] == 'username':
                from .canonical_username import guard_username_platform
                guard_username_platform(c, owner, item_id, fact)
            self._observe(c, owner, item_id, operation_id, record["source"], actor_id, received, fact,
                          entity_version=entity_version, operation_sequence=count+1,item_version=item["version"]+1)
            count += 1
            for flag_name, flag in raw.get("flags", {}).items():
                self._observe(c, owner, item_id, operation_id, record["source"], actor_id, received, fact, flag_name, flag,
                              entity_version=entity_version, operation_sequence=count+1,item_version=item["version"]+1)
                count += 1
            touched_items.add(item_id)
        for item_id in sorted(touched_items):
            c.execute("UPDATE items SET version=version+1,updated_at=%s WHERE owner_id=%s AND item_id=%s", (received, owner, item_id))
        for container in record["containers"]:
            c.execute("INSERT INTO source_containers(operation_id,path_hash,path_json,container_type,length,metadata_json) VALUES(%s,%s,%s,%s,%s,%s)",
                      (operation_id, digest(container["source_path"]), json_text(container["source_path"]), container["type"], container["length"], json_text({k: v for k, v in container.items() if k not in {"source_path", "type", "length"}})))
        version = c.execute("UPDATE entities SET version=version+1,updated_at=%s WHERE owner_id=%s RETURNING version", (received, owner)).fetchone()["version"]
        c.execute("INSERT INTO outbox(event_id,operation_id,owner_id,entity_version,created_at) VALUES(%s,%s,%s,%s,%s)",
                  (identifier("outbox", str(operation_id)), operation_id, owner, version, received))
        return owner, True, count

    def _observe(self, c, owner: UUID, item_id: UUID, operation_id: UUID, source: str,
                 actor: str, received: datetime, fact: dict, flag_name: str | None = None, flag: dict | None = None,
                 *, entity_version: int, operation_sequence: int, item_version: int):
        raw = fact["raw"]
        dimension = "flag:" + flag_name if flag_name else "value"
        event_id = identifier("observation", [str(operation_id), fact["id"], dimension])
        path_hash, path_json = digest(raw["target_path"]), json_text(raw["target_path"])
        params = (owner, item_id, path_hash, dimension)
        previous = c.execute("SELECT * FROM field_state WHERE owner_id=%s AND item_id=%s AND target_path_hash=%s AND dimension=%s", params).fetchone()
        if previous and previous["target_path_json"] != path_json:
            raise IdentityConflict("Field path hash collision requires manual investigation")
        source_updated = _date(flag.get("source_updated_at")) if flag is not None else fact["source_updated_at"]
        observed = _date(flag.get("observed_at")) if flag is not None else fact["observed_at"]
        effective = source_updated or observed
        applied, reason = _precedence(previous, effective, received, event_id)
        if previous and fact["status"] == "pending" and previous["status"] != "pending":
            applied, reason = False, "pending_against_resolved"
        value_json = json_text(flag["value"]) if flag is not None else fact["normalized_json"]
        input_json = value_json if flag is not None else fact["input_json"]
        input_type = "null" if flag is not None and flag["value"] is None else "boolean" if flag is not None else fact["input_type"]
        metadata_json = json_text(flag) if flag is not None else fact["metadata_json"]
        binding_hash = None
        if flag is not None:
            # A transformation does not move evidence from the received value to
            # a different canonical value. A caller with explicit evidence for
            # the canonical value must supply that binding deliberately.
            binding_json = flag.get("confirmed_value_json", fact["input_json"])
            if not isinstance(binding_json, str):
                raise CanonicalError("Flag confirmed_value_json must be a JSON literal")
            # Binding compares decoded values canonically, while preserving the
            # exact source literal in the observation metadata.
            binding_hash = digest(decode(binding_json))
            value_state = c.execute("SELECT value_json FROM field_state WHERE owner_id=%s AND item_id=%s AND target_path_hash=%s AND dimension='value'", (owner, item_id, path_hash)).fetchone()
            if not value_state or digest(decode(value_state["value_json"])) != binding_hash:
                applied, reason = False, "value_mismatch"
        c.execute("""INSERT INTO observations(owner_id,observation_id,item_id,operation_id,source_fact_id_json,
                   source_path_json,target_path_hash,target_path_json,dimension,input_json,input_type,input_encoding,
                   normalized_json,metadata_json,source_id_json,source_updated_at,observed_at,effective_at,received_at,
                   actor_id_json,status,applied,pending_reason,previous_observation_id,previous_value_json,binding_hash,
                   entity_version,operation_sequence,source_id_hash,item_version)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                  (owner,event_id,item_id,operation_id,json_text(fact["id"]),json_text(raw["source_path"]),path_hash,path_json,
                   dimension,input_json,input_type,fact["input_encoding"] if flag is None else "canonical_value",
                   value_json,metadata_json,json_text(source),source_updated,observed,effective,received,json_text(actor),
                   fact["status"],applied,reason,previous["observation_id"] if previous else None,
                   previous["value_json"] if previous else None,binding_hash,entity_version,operation_sequence,digest(source),item_version))
        if applied:
            c.execute("""INSERT INTO field_state(owner_id,item_id,target_path_hash,target_path_json,dimension,
                       observation_id,value_json,metadata_json,status,effective_at,received_at,source_id_json,binding_hash)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(owner_id,item_id,target_path_hash,dimension) DO UPDATE SET
                       observation_id=EXCLUDED.observation_id,value_json=EXCLUDED.value_json,metadata_json=EXCLUDED.metadata_json,
                       status=EXCLUDED.status,effective_at=EXCLUDED.effective_at,received_at=EXCLUDED.received_at,
                       source_id_json=EXCLUDED.source_id_json,binding_hash=EXCLUDED.binding_hash""",
                      (owner,item_id,path_hash,path_json,dimension,event_id,value_json,metadata_json,fact["status"],
                       effective,received,json_text(source),binding_hash))

    def get_entity(self, owner_id: str) -> dict:
        """Small/synthetic hydration, not a streaming endpoint for large histories."""
        owner = UUID(str(owner_id))
        with self.connection() as c:
            c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            row = c.execute("SELECT * FROM entities WHERE owner_id=%s", (owner,)).fetchone()
            if not row:
                raise CanonicalError("Entity does not exist")
            items = c.execute("SELECT * FROM items WHERE owner_id=%s ORDER BY item_id", (owner,)).fetchall()
            fields = c.execute("SELECT * FROM field_state WHERE owner_id=%s ORDER BY item_id,target_path_hash,dimension", (owner,)).fetchall()
            by_item = {str(item["item_id"]): {"id": str(item["item_id"]), "kind": item["kind"], "item_key": decode(item["item_key_json"]),
                       "version": item["version"], "created_at": item["created_at"].isoformat(), "updated_at": item["updated_at"].isoformat(),
                       "fields": [], "flags": []} for item in items}
            values = {(field["item_id"],field["target_path_hash"]): digest(decode(field["value_json"])) for field in fields if field["dimension"] == "value"}
            for field in fields:
                current = {"path":decode(field["target_path_json"]),"value":decode(field["value_json"]),
                           "value_json":field["value_json"],"observation_id":str(field["observation_id"]),
                           "source_id":decode(field["source_id_json"]),"metadata":decode(field["metadata_json"]),
                           "status":field["status"],"effective_at":field["effective_at"].isoformat() if field["effective_at"] else None,
                           "received_at":field["received_at"].isoformat()}
                if field["dimension"] == "value":
                    by_item[str(field["item_id"])]["fields"].append(current)
                else:
                    applicable = field["binding_hash"] == values.get((field["item_id"],field["target_path_hash"]))
                    current.update(name=field["dimension"][5:], applicable=applicable,
                                   value=current["value"] if applicable else None)
                    by_item[str(field["item_id"])]["flags"].append(current)
            return {"id":str(owner),"entity_type":row["entity_type"],"version":row["version"],"items":list(by_item.values())}

    def entity_metadata(self, owner_id: str) -> dict:
        """Bounded metadata and initial collection cursors at one committed cut."""
        owner = self._page_arguments(owner_id, 1, "asc", {})
        with self.connection() as c:
            frontier = self._read_frontier(c, owner, "items", {"kind": None}, "asc", None)
            row = c.execute("SELECT entity_type FROM entities WHERE owner_id=%s", (owner,)).fetchone()
            result = {"id": str(owner), "entity_type": row["entity_type"], "version": frontier["cut_version"]}
            for kind in ("items", "fields", "history"):
                filters = {"kind": None} if kind == "items" else {"item_id": None, "field_path": None, "source_id": None, "dimension": None}
                result[kind] = {"included": False, "cursor": self._issue_cursor(c, owner, kind, filters, "asc", frontier, None)}
            return result

    def lookup_identity(self, *, source_id: str | None = None, source_record_id: str | None = None,
                        document_type: str | None = None, country: str | None = None, value: str | None = None) -> str | None:
        """Exact indexed lookup of an externally validated/normalized identity."""
        source_mode = source_id is not None or source_record_id is not None
        document_mode = document_type is not None or country is not None or value is not None
        if source_mode == document_mode:
            raise CanonicalError("Specify exactly one source identity or one document identity")
        if source_mode:
            key = ["source", _text(source_id,"source_id"), _text(source_record_id,"source_record_id")]
        else:
            key = ["document",_token(country,"country"),_token(document_type,"document_type"),_text(value,"value")]
        with self.connection() as c:
            row = c.execute("SELECT owner_id,identity_json FROM identity_keys WHERE key_hash=%s",(digest(key),)).fetchone()
            if row is None:
                return None
            if row["identity_json"] != json_text(key):
                raise IdentityConflict("Identity hash collision requires manual investigation")
            return str(row["owner_id"])

    @staticmethod
    def _page_arguments(owner_id, limit, order, filters):
        try:
            owner = UUID(str(owner_id))
        except (ValueError, TypeError) as exc:
            raise CanonicalError("Invalid entity UUID") from exc
        if type(limit) is not int or not 1 <= limit <= MAX_PAGE_SIZE:
            raise CanonicalError("Page limit must be between 1 and 200")
        if not isinstance(order,str) or order not in {"asc", "desc"}:
            raise CanonicalError("Page order must be asc or desc")
        for key, value in filters.items():
            if value is not None and not isinstance(value, str):
                raise CanonicalError("Read filters must be strings or null")
            if key == "item_id" and value is not None:
                try:
                    filters[key] = str(UUID(value))
                except ValueError as exc:
                    raise CanonicalError("Invalid item UUID") from exc
        if len(json_text(filters)) > 8192:
            raise CanonicalError("Read filter context exceeds 8 KiB")
        return owner

    def _read_frontier(self, c, owner, kind, filters, order, cursor):
        c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        self._deployment(c)
        now = c.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
        if cursor is None:
            entity = c.execute("SELECT version FROM entities WHERE owner_id=%s", (owner,)).fetchone()
            if entity is None:
                raise CanonicalError("Entity does not exist")
            return {"cut_version":entity["version"], "position":None, "started_at":now,
                    "expires_at":now+CURSOR_IDLE_TTL, "deadline_at":now+CURSOR_MAX_TTL}
        if not isinstance(cursor, str) or not re.fullmatch(r"cb1_[A-Za-z0-9_-]{43}", cursor):
            raise InvalidCursor("Invalid or expired canonical read cursor")
        token_hash = hashlib.sha256(cursor.encode("ascii")).hexdigest()
        row = c.execute("SELECT * FROM read_cursors WHERE token_hash=%s", (token_hash,)).fetchone()
        if (row is None or row["owner_id"] != owner or row["kind"] != kind or row["ordering"] != order
            or row["expires_at"] <= now or row["deadline_at"] <= now
            or not secrets.compare_digest(row["filter_hash"],digest(filters))
            or row["filters_json"] != json_text(filters)):
            raise InvalidCursor("Invalid or expired canonical read cursor")
        return {"cut_version":row["cut_version"],"position":decode(row["position_json"]),
                "started_at":row["started_at"],"expires_at":min(now+CURSOR_IDLE_TTL,row["deadline_at"]),
                "deadline_at":row["deadline_at"]}

    @staticmethod
    def _issue_cursor(c, owner, kind, filters, order, frontier, position):
        for _ in range(3):
            token = "cb1_" + secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
            created = c.execute("""INSERT INTO read_cursors(token_hash,owner_id,kind,filter_hash,filters_json,ordering,
                cut_version,position_json,started_at,expires_at,deadline_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING""", (token_hash,owner,kind,digest(filters),json_text(filters),order,
                frontier["cut_version"],json_text(position),frontier["started_at"],frontier["expires_at"],frontier["deadline_at"]))
            if created.rowcount == 1:
                return token
        raise CanonicalError("Unable to allocate a unique canonical read cursor")

    @staticmethod
    def _public_observation(row):
        result = {key:str(row[key]) if isinstance(row[key],UUID) else row[key] for key in (
            "owner_id","item_id","observation_id","operation_id","entity_version","operation_sequence","item_version",
            "dimension","input_type","input_encoding","input_json","normalized_json","status","applied",
            "pending_reason","previous_observation_id","previous_value_json","binding_hash")}
        for name,column in (("source_fact_id","source_fact_id_json"),("source_path","source_path_json"),
                            ("field_path","target_path_json"),("input_value","input_json"),("normalized_value","normalized_json"),
                            ("metadata","metadata_json"),("source_id","source_id_json"),("actor_id","actor_id_json")):
            result[name] = decode(row[column])
        for name in ("source_updated_at","observed_at","effective_at","received_at"):
            result[name] = row[name].isoformat() if row[name] else None
        result["previous_value"] = decode(row["previous_value_json"]) if row["previous_value_json"] is not None else None
        result["operation"] = {"source_record_id":decode(row["source_record_id_json"]),
            "source_version":decode(row["source_version_json"]),"record_hash":row["record_hash"],
            "adapter_version":decode(row["adapter_version_json"]),"normalizer_version":decode(row["normalizer_version_json"])}
        return result

    @staticmethod
    def _take_page(c, query, params, limit, encode, extra_row_bytes=0):
        # Reserve the fixed response envelope and separators; item summaries
        # also reserve their two fixed-size opaque collection tokens.
        raw_rows, public_rows, size = [], [], PAGE_ENVELOPE_BYTES
        # A server-side cursor avoids buffering 201 potentially large source
        # literals. At most a page and one extra row are held in this process.
        with c.cursor(name="read_"+uuid4().hex) as rows:
            rows.itersize = 1
            rows.execute(query,params)
            for row in rows:
                if len(public_rows) == limit:
                    return raw_rows,public_rows,True
                public = encode(row)
                row_size = len(json_text(public)) + 2 + extra_row_bytes
                if size + row_size > MAX_PAGE_BYTES:
                    if not public_rows:
                        raise CanonicalRowTooLarge("A canonical row exceeds the 8 MiB page budget; a streaming artifact is required")
                    return raw_rows,public_rows,True
                raw_rows.append(row)
                public_rows.append(public)
                size += row_size
        return raw_rows,public_rows,False

    def _page_result(self,c,owner,kind,filters,order,frontier,rows,has_more,position):
        token = self._issue_cursor(c,owner,kind,filters,order,frontier,position) if has_more else None
        return {"items":rows,"has_more":has_more,"next_cursor":token,
                "snapshot":{"entity_id":str(owner),"entity_version":frontier["cut_version"],
                            "started_at":frontier["started_at"].isoformat(),"expires_at":frontier["expires_at"].isoformat(),
                            "deadline_at":frontier["deadline_at"].isoformat()}}

    def page_history(self, owner_id: str, *, item_id: str | None = None, field_path: str | None = None,
                     source_id: str | None = None, dimension: str | None = None,
                     order: str = "asc", limit: int = 100, cursor: str | None = None) -> dict:
        """Immutable event history at a committed entity-version frontier."""
        filters = {"item_id":item_id,"field_path":field_path,"source_id":source_id,"dimension":dimension}
        owner = self._page_arguments(owner_id,limit,order,filters)
        with self.connection() as c:
            frontier = self._read_frontier(c,owner,"history",filters,order,cursor)
            clauses = [sql.SQL("o.owner_id=%s"),sql.SQL("o.entity_version<=%s")]
            params = [owner,frontier["cut_version"]]
            for key,column in (("item_id","item_id"),("dimension","dimension")):
                if filters[key] is not None:
                    clauses.append(sql.SQL("o.{}=%s").format(sql.Identifier(column)))
                    params.append(UUID(filters[key]) if key == "item_id" else filters[key])
            for key,column,value_column in (("field_path","target_path_hash","target_path_json"),("source_id","source_id_hash","source_id_json")):
                if filters[key] is not None:
                    clauses.append(sql.SQL("o.{}=%s AND o.{}=%s").format(sql.Identifier(column),sql.Identifier(value_column)))
                    params.extend([digest(filters[key]),json_text(filters[key])])
            if frontier["position"] is not None:
                clauses.append(sql.SQL("(o.entity_version,o.operation_sequence) {} (%s,%s)").format(sql.SQL(">" if order == "asc" else "<")))
                params.extend(frontier["position"])
            params.append(limit+1)
            query = sql.SQL("""SELECT o.*,p.source_record_id_json,p.source_version_json,p.record_hash,p.adapter_version_json,p.normalizer_version_json
                FROM observations o JOIN operations p ON p.operation_id=o.operation_id AND p.owner_id=o.owner_id
                WHERE {} ORDER BY o.entity_version {},o.operation_sequence {} LIMIT %s""").format(
                    sql.SQL(" AND ").join(clauses),sql.SQL(order),sql.SQL(order))
            raw,public,more = self._take_page(c,query,params,limit,self._public_observation)
            position = [raw[-1]["entity_version"],raw[-1]["operation_sequence"]] if raw else None
            return self._page_result(c,owner,"history",filters,order,frontier,public,more,position)

    def page_fields(self, owner_id: str, *, item_id: str | None = None, field_path: str | None = None,
                    source_id: str | None = None, dimension: str | None = None,
                    order: str = "asc", limit: int = 100, cursor: str | None = None) -> dict:
        """Project each field/flag from immutable observations at the cursor cut."""
        filters = {"item_id":item_id,"field_path":field_path,"source_id":source_id,"dimension":dimension}
        owner = self._page_arguments(owner_id,limit,order,filters)
        with self.connection() as c:
            frontier = self._read_frontier(c,owner,"fields",filters,order,cursor)
            clauses = [sql.SQL("k.owner_id=%s")]
            params = [frontier["cut_version"],frontier["cut_version"],owner]
            for key,column in (("item_id","item_id"),("dimension","dimension")):
                if filters[key] is not None:
                    clauses.append(sql.SQL("k.{}=%s").format(sql.Identifier(column)))
                    params.append(UUID(filters[key]) if key == "item_id" else filters[key])
            if filters["field_path"] is not None:
                clauses.append(sql.SQL("k.target_path_hash=%s AND h.target_path_json=%s"))
                params.extend([digest(filters["field_path"]),json_text(filters["field_path"])])
            if filters["source_id"] is not None:
                clauses.append(sql.SQL("h.source_id_hash=%s AND h.source_id_json=%s"))
                params.extend([digest(filters["source_id"]),json_text(filters["source_id"])])
            if frontier["position"] is not None:
                clauses.append(sql.SQL("(k.item_id,k.target_path_hash,k.dimension) {} (%s,%s,%s)").format(sql.SQL(">" if order == "asc" else "<")))
                position = frontier["position"]
                params.extend([UUID(position[0]),position[1],position[2]])
            params.append(limit+1)
            query = sql.SQL("""SELECT h.*,v.normalized_json AS binding_value_json,
                p.source_record_id_json,p.source_version_json,p.record_hash,p.adapter_version_json,p.normalizer_version_json
                FROM field_state k
                JOIN LATERAL (SELECT o.* FROM observations o WHERE o.owner_id=k.owner_id AND o.item_id=k.item_id
                  AND o.target_path_hash=k.target_path_hash AND o.dimension=k.dimension AND o.applied AND o.entity_version<=%s
                  ORDER BY o.entity_version DESC,o.operation_sequence DESC LIMIT 1) h ON true
                LEFT JOIN LATERAL (SELECT o.normalized_json FROM observations o WHERE h.dimension<>'value'
                  AND o.owner_id=k.owner_id AND o.item_id=k.item_id AND o.target_path_hash=k.target_path_hash
                  AND o.dimension='value' AND o.applied AND o.entity_version<=%s
                  ORDER BY o.entity_version DESC,o.operation_sequence DESC LIMIT 1) v ON true
                JOIN operations p ON p.operation_id=h.operation_id AND p.owner_id=h.owner_id
                WHERE {} ORDER BY k.item_id {},k.target_path_hash {},k.dimension {} LIMIT %s""").format(
                    sql.SQL(" AND ").join(clauses),sql.SQL(order),sql.SQL(order),sql.SQL(order))
            def public_field(row):
                result = self._public_observation(row)
                applicable = row["dimension"] == "value" or (row["binding_value_json"] is not None and row["binding_hash"] == digest(decode(row["binding_value_json"])))
                result.update(applicable=applicable,value=result["normalized_value"] if applicable else None,
                              value_json=row["normalized_json"] if applicable else "null")
                if row['dimension'].startswith('flag:'):
                    evaluated = datetime.now(timezone.utc)
                    metadata = result['metadata']
                    # Older adapters can preserve unknown evidence metadata.
                    # A malformed expiry must not hide the field or its history.
                    try:
                        expiry = _date(metadata.get('expires_at'))
                        stale = bool(expiry and expiry <= evaluated)
                    except CanonicalError:
                        stale = None
                    result.update(checked_at=metadata.get('checked_at'), expires_at=metadata.get('expires_at'),
                                  stale=stale, freshness_evaluated_at=evaluated.isoformat())
                return result
            raw,public,more = self._take_page(c,query,params,limit,public_field)
            position = [str(raw[-1]["item_id"]),raw[-1]["target_path_hash"],raw[-1]["dimension"]] if raw else None
            return self._page_result(c,owner,"fields",filters,order,frontier,public,more,position)

    def page_items(self, owner_id: str, *, kind: str | None = None, order: str = "asc",
                   limit: int = 100, cursor: str | None = None) -> dict:
        """Item summaries and bounded collection cursors sharing the same cut."""
        filters = {"kind":kind}
        owner = self._page_arguments(owner_id,limit,order,filters)
        with self.connection() as c:
            frontier = self._read_frontier(c,owner,"items",filters,order,cursor)
            clauses = [sql.SQL("i.owner_id=%s AND i.created_version<=%s")]
            params = [frontier["cut_version"],owner,frontier["cut_version"]]
            if kind is not None:
                clauses.append(sql.SQL("i.kind=%s"))
                params.append(kind)
            if frontier["position"] is not None:
                clauses.append(sql.SQL("i.item_id {} %s").format(sql.SQL(">" if order == "asc" else "<")))
                params.append(UUID(frontier["position"]))
            params.append(limit+1)
            query = sql.SQL("""SELECT i.*,h.version_at_cut,h.updated_at_cut FROM items i
                JOIN LATERAL (SELECT o.item_version AS version_at_cut,o.received_at AS updated_at_cut
                  FROM observations o WHERE o.owner_id=i.owner_id AND o.item_id=i.item_id AND o.entity_version<=%s
                  ORDER BY o.entity_version DESC,o.operation_sequence DESC LIMIT 1) h ON true
                WHERE {} ORDER BY i.item_id {} LIMIT %s""").format(sql.SQL(" AND ").join(clauses),sql.SQL(order))
            def public_item(row):
                return {"id":str(row["item_id"]),"owner_id":str(owner),"kind":row["kind"],"item_key":decode(row["item_key_json"]),
                        "version":row["version_at_cut"],"created_at":row["created_at"].isoformat(),
                        "updated_at":row["updated_at_cut"].isoformat() if row["updated_at_cut"] else row["created_at"].isoformat()}
            raw,public,more = self._take_page(c,query,params,limit,public_item,ITEM_COLLECTION_CURSOR_BYTES)
            for item in public:
                child_filters = {"item_id":item["id"],"field_path":None,"source_id":None,"dimension":None}
                item["fields"] = {"included":False,"cursor":self._issue_cursor(c,owner,"fields",child_filters,"asc",frontier,None)}
                item["history"] = {"included":False,"cursor":self._issue_cursor(c,owner,"history",child_filters,"asc",frontier,None)}
            position = str(raw[-1]["item_id"]) if raw else None
            return self._page_result(c,owner,"items",filters,order,frontier,public,more,position)

    def cleanup_read_cursors(self, limit: int = 1000) -> int:
        """Delete expired technical cursors only; canonical facts are unaffected."""
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise CanonicalError("Cursor cleanup limit must be between 1 and 10000")
        with self.connection() as c:
            return c.execute("""WITH expired AS (SELECT token_hash FROM read_cursors WHERE expires_at<=clock_timestamp()
                ORDER BY expires_at,token_hash LIMIT %s FOR UPDATE SKIP LOCKED)
                DELETE FROM read_cursors c USING expired e WHERE c.token_hash=e.token_hash""",(limit,)).rowcount

    def claim_outbox(self, limit: int = 100, lease_seconds: int = 60) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 1000 or type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise CanonicalError("Invalid outbox lease bounds")
        token = uuid4()
        with self.connection() as c:
            rows = c.execute("""WITH candidates AS (
                SELECT event_id FROM outbox WHERE published_at IS NULL AND
                (lease_until IS NULL OR lease_until < clock_timestamp())
                ORDER BY created_at,event_id LIMIT %s FOR UPDATE SKIP LOCKED)
                UPDATE outbox o SET lease_token=%s,lease_until=clock_timestamp() + %s * interval '1 second',
                attempts=attempts+1 FROM candidates x WHERE o.event_id=x.event_id RETURNING o.*""", (limit,token,lease_seconds)).fetchall()
            return [{k: str(v) if isinstance(v,UUID) else v.isoformat() if isinstance(v,datetime) else v for k,v in row.items()} for row in rows]

    def acknowledge_outbox(self, event_id: str, lease_token: str) -> bool:
        with self.connection() as c:
            row = c.execute("UPDATE outbox SET published_at=clock_timestamp(),lease_until=NULL WHERE event_id=%s AND lease_token=%s AND lease_until >= clock_timestamp() AND published_at IS NULL RETURNING event_id", (UUID(event_id),UUID(lease_token))).fetchone()
            return row is not None

"""Opt-in synthetic PostgreSQL field catalog; no runtime DDL or SQLite fallback."""
from contextlib import contextmanager
import hashlib
from pathlib import Path
import re
from uuid import uuid4

import psycopg
from fastapi import HTTPException, Query, Request
from pydantic import ValidationError

from .canonical_http import exact_response, failure, validate_synthetic_dsn
from .canonical_store import CanonicalError, CanonicalStore, decode, digest, identifier, json_text
from .catalogs import FieldDefinitionInput, create_definition, update_definition, snapshot

PG_FIELD_CONTRACT = 'canonical-postgresql-field-2026-09-09.1'
DDL = Path(__file__).resolve().parents[2] / 'infra/sql/002_synthetic_catalog.sql'


def verify_catalog(c):
    info = CanonicalStore._deployment(c)
    if (info['environment'] != 'synthetic' or info['database'] != 'bigbase_test'
            or info['server_address'] is not None or info['server_port'] != 18769):
        raise CanonicalError('Private synthetic catalog required')
    row = c.execute('SELECT * FROM field_catalog_meta WHERE singleton').fetchone()
    if not row or str(row['deployment_id']) != info['deployment_id'] or row['ddl_sha256'] != hashlib.sha256(DDL.read_bytes()).hexdigest():
        raise CanonicalError('Synthetic catalog extension mismatch')
    return info


def initialize_catalog(repository):
    """Fixture setup only, explicitly called after base-schema initialization."""
    validate_synthetic_dsn(repository.dsn)
    with repository.connection() as c:
        info = repository._deployment(c)
        if (info['environment'] != 'synthetic' or info['database'] != 'bigbase_test'
                or info['server_address'] is not None or info['server_port'] != 18769):
            raise CanonicalError('Private synthetic catalog required')
        c.execute('SELECT pg_advisory_xact_lock(%s)', (repository._lock_key([repository.schema, 'catalog-schema']),))
        exists = c.execute("SELECT to_regclass('field_catalog_meta') AS name").fetchone()['name']
        if not exists:
            c.execute(DDL.read_text())
            c.execute('INSERT INTO field_catalog_meta(deployment_id,ddl_sha256) VALUES(%s,%s)',
                      (info['deployment_id'], hashlib.sha256(DDL.read_bytes()).hexdigest()))
        verify_catalog(c)


def lock_fields(c, field_ids):
    schema = c.execute('SELECT current_schema() AS name').fetchone()['name']
    for field_id in sorted(set(field_ids)):
        # Exclusive per-field locks also cover an ID that is still unknown.
        c.execute('SELECT pg_advisory_xact_lock(%s)', (CanonicalStore._lock_key([schema, 'field-catalog', field_id]),))


def load_definitions(c, field_ids):
    verify_catalog(c)
    lock_fields(c, field_ids)
    rows = c.execute('SELECT field_id,definition_json FROM field_catalog WHERE field_id=ANY(%s)', (list(field_ids),)).fetchall()
    return {row['field_id']: decode(row['definition_json']) for row in rows}


class CatalogAdapter:
    """Reuse strict definition rules while keeping all writes on this connection."""
    def get(self, c, kind, key):
        if kind == 'field':
            row = c.execute('SELECT definition_json FROM field_catalog WHERE field_id=%s', (key,)).fetchone()
        else:
            field_id, version = key.rsplit(':', 1)
            row = c.execute('SELECT definition_json FROM field_catalog_versions WHERE field_id=%s AND version=%s', (field_id, int(version))).fetchone()
        return decode(row['definition_json']) if row else None

    def put(self, c, kind, value):
        if kind == 'field':
            saved = snapshot(value)
            c.execute('INSERT INTO field_catalog(field_id,version,definition_json) VALUES(%s,%s,%s) ON CONFLICT(field_id) DO UPDATE SET version=EXCLUDED.version,definition_json=EXCLUDED.definition_json',
                      (saved['id'], saved['version'], json_text(saved)))
        else:
            saved = value['definition']
            c.execute('INSERT INTO field_catalog_versions(field_id,version,definition_json,definition_sha256) VALUES(%s,%s,%s,%s)',
                      (value['field_id'], saved['version'], json_text(saved), digest(saved)))


def mutate_catalog(repository, *, actor_id, request_key, body, field_id=None, if_match=None):
    action = 'update' if field_id is not None else 'create'
    operation = identifier('canonical-field-catalog', [actor_id, request_key])
    signature = digest({'action': action, 'field_id': field_id, 'body': body, 'if_match': if_match})
    with repository.connection() as c:
        verify_catalog(c)
        c.execute('SELECT pg_advisory_xact_lock(%s)', (repository._lock_key([repository.schema, 'catalog-operation', str(operation)]),))
        prior = c.execute('SELECT request_sha256,receipt_json FROM field_catalog_operations WHERE operation_id=%s', (operation,)).fetchone()
        if prior:
            if prior['request_sha256'] != signature:
                failure(409, 'CATALOG_IDEMPOTENCY_CONFLICT', 'Chave idempotente reutilizada com conteúdo diferente.')
            return {**decode(prior['receipt_json']), 'replayed': True}
        if action == 'create':
            try:
                parsed = FieldDefinitionInput.model_validate(body)
            except ValidationError:
                failure(422, 'INVALID_CANONICAL_DEFINITION', 'Definição inválida; confira nome, tipo, opções e escopo.')
            field_id = parsed.id or str(uuid4())
            parsed.id = field_id
        elif not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}', field_id):
            failure(422, 'INVALID_CANONICAL_FIELD_ID', 'Identificador de campo inválido.')
        lock_fields(c, [field_id])
        adapter = CatalogAdapter()
        if action == 'create':
            value = create_definition(adapter, c, parsed, actor_id)
        else:
            value = update_definition(adapter, c, field_id, body, if_match, actor_id)
        definition = snapshot(value)
        receipt = {'operation_id': str(operation), 'definition': definition,
                   'definition_sha256': digest(definition), 'replayed': False,
                   'catalog_environment': 'postgresql_synthetic', 'production_connected': False}
        c.execute('INSERT INTO field_catalog_operations(operation_id,request_sha256,actor_json,action,field_id,version,receipt_json) VALUES(%s,%s,%s,%s,%s,%s,%s)',
                  (operation, signature, json_text(actor_id), action, field_id, definition['version'], json_text(receipt)))
        return receipt


def install_canonical_catalog(app, reads, *, store, security, auth):
    @contextmanager
    def available(request, mutate=False):
        with store.transaction() as c:
            user, key = auth(c, request, 'admin' if mutate else 'read')
            if mutate:
                security.require_recent_totp(c, request, user, key)
        if reads is None or (mutate and not reads.writes_enabled):
            failure(503, 'CANONICAL_CATALOG_DISABLED', 'Catálogo PostgreSQL sintético não configurado.')
        try:
            reads.verify()
            yield user
        except (psycopg.Error, CanonicalError, ValueError):
            failure(503, 'CANONICAL_CATALOG_UNAVAILABLE', 'Catálogo PostgreSQL sintético indisponível; repita com a mesma chave.')

    @app.get('/api/v1/canonical/fields')
    def fields(request: Request, after: str = Query(default='', max_length=160), limit: int = Query(default=50, ge=1, le=100)):
        with available(request):
            with reads.repository.connection() as c:
                verify_catalog(c)
                rows = c.execute('SELECT field_id,definition_json FROM field_catalog WHERE field_id>%s ORDER BY field_id LIMIT %s', (after, limit+1)).fetchall()
                return exact_response({'items': [decode(row['definition_json']) for row in rows[:limit]],
                    'next_after': rows[limit-1]['field_id'] if len(rows)>limit else None,
                    'catalog_environment': 'postgresql_synthetic', 'search_state': 'pending'})

    @app.get('/api/v1/canonical/fields/{field_id}/history')
    def history(field_id: str, request: Request, after: int = Query(default=0, ge=0, le=2**63-1), limit: int = Query(default=50, ge=1, le=100)):
        with available(request):
            with reads.repository.connection() as c:
                verify_catalog(c)
                rows = c.execute('SELECT version,definition_json,definition_sha256 FROM field_catalog_versions WHERE field_id=%s AND version>%s ORDER BY version LIMIT %s', (field_id, after, limit+1)).fetchall()
                return exact_response({'items': [{'definition': decode(row['definition_json']), 'definition_sha256': row['definition_sha256']} for row in rows[:limit]],
                    'next_after': rows[limit-1]['version'] if len(rows)>limit else None, 'catalog_environment': 'postgresql_synthetic'})

    def write(request, body, field_id=None):
        with available(request, True) as user:
            key = request.headers.get('Idempotency-Key', '')
            if not key.strip() or len(key) > 200:
                failure(422, 'IDEMPOTENCY_KEY_REQUIRED', 'Idempotency-Key obrigatório, até 200 caracteres.')
            return exact_response(mutate_catalog(reads.repository, actor_id=user['id'], request_key=key, body=body,
                field_id=field_id, if_match=request.headers.get('If-Match')))

    @app.post('/api/v1/canonical/fields')
    def create(body: dict, request: Request):
        return write(request, body)

    @app.patch('/api/v1/canonical/fields/{field_id}')
    def update(field_id: str, body: dict, request: Request):
        return write(request, body, field_id)

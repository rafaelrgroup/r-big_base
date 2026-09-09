"""Explicit synthetic PostgreSQL reads; authentication still uses local SQLite.

No environment DSN discovery, schema initialization or production fallback.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import contextmanager
from uuid import UUID

import psycopg
from psycopg.conninfo import conninfo_to_dict
from cryptography.fernet import InvalidToken
from fastapi import HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

from .canonical_store import CanonicalStore, CanonicalError, InvalidCursor, CanonicalRowTooLarge, digest, json_text
from .canonical_search_reader import CanonicalSearchReader, SearchReadError, InvalidSearchCursor
from .canonical_search import build_query, ProjectionError, FIELDS, FLAGS, PROJECTION_VERSION
from .security import hashed


CURSOR_CLEANUP_BATCH_SIZE = 1000
CURSOR_CLEANUP_IDLE_SECONDS = 60
CURSOR_CLEANUP_BACKLOG_SECONDS = 0.25
CURSOR_CLEANUP_RETRY_SECONDS = 60
logger = logging.getLogger(__name__)


def validate_synthetic_dsn(dsn):
    config = conninfo_to_dict(dsn)
    if (not config.get('host', '').startswith('/') or config.get('hostaddr') or config.get('service')
            or ',' in config['host'] or config.get('port') != '18769' or config.get('dbname') != 'bigbase_test'
            or os.environ.get('PGHOSTADDR') or os.environ.get('PGSERVICE')):
        raise ValueError('CANONICAL_HTTP_REQUIRES_PRIVATE_SYNTHETIC_FIXTURE')


class CanonicalReads:
    def __init__(self, repository: CanonicalStore, *, expected_deployment_id: str,
                 search_reader: CanonicalSearchReader | None = None, writes_enabled: bool = False,
                 search_availability_provider=None):
        # Reject network/default destinations BEFORE opening a connection.
        validate_synthetic_dsn(repository.dsn)
        self.repository = repository
        self.deployment_id = str(UUID(expected_deployment_id))
        self.search_reader = search_reader
        # Internal synthetic dependency only; never populated from HTTP input.
        if search_availability_provider is not None and not callable(search_availability_provider):
            raise ValueError('INVALID_SEARCH_AVAILABILITY_PROVIDER')
        self.search_availability_provider = search_availability_provider
        if type(writes_enabled) is not bool:
            raise ValueError('CANONICAL_WRITES_REQUIRE_EXPLICIT_BOOLEAN')
        self.writes_enabled = writes_enabled
        self.verify()

    def verify(self):
        validate_synthetic_dsn(self.repository.dsn)
        info = self.repository.deployment_info()
        if (info['deployment_id'] != self.deployment_id or info['environment'] != 'synthetic'
                or info['database'] != 'bigbase_test' or info['server_address'] is not None
                or info['server_port'] != 18769 or not 180000 <= info['server_version_num'] < 190000):
            raise ValueError('CANONICAL_HTTP_SYNTHETIC_DESTINATION_MISMATCH')

    def cleanup_read_cursors(self):
        # Revalidate immediately before each bounded cleanup; never follow a
        # deployment/configuration change merely because startup once passed.
        self.verify()
        removed = self.repository.cleanup_read_cursors(limit=CURSOR_CLEANUP_BATCH_SIZE)
        if type(removed) is not int or not 0 <= removed <= CURSOR_CLEANUP_BATCH_SIZE:
            raise ValueError('CANONICAL_CURSOR_CLEANUP_INVALID_COUNT')
        return removed


async def maintain_canonical_cursors(reads):
    """Drain expired technical cursors in bounded batches; preserve canonical data."""
    while True:
        try:
            removed = await run_in_threadpool(reads.cleanup_read_cursors)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Drivers may include DSNs or server details in exception messages.
            # Retry the next cycle without logging the exception or its values.
            logger.warning('Limpeza de cursores canônicos indisponível; nova tentativa será feita.')
            delay = CURSOR_CLEANUP_RETRY_SECONDS
        else:
            delay = CURSOR_CLEANUP_BACKLOG_SECONDS if removed == CURSOR_CLEANUP_BATCH_SIZE else CURSOR_CLEANUP_IDLE_SECONDS
        await asyncio.sleep(delay)


def failure(status, code, message):
    raise HTTPException(status, {'code': code, 'message': message})


def exact_response(value):
    # Avoid FastAPI's Decimal -> float encoder and preserve escaped NUL/surrogates.
    text = json_text(value)
    if len(text) > 10 * 1024 * 1024:
        failure(413, 'CANONICAL_RESPONSE_TOO_LARGE', 'Resposta excede 10 MiB; solicite uma página menor. Exportação canônica ainda pendente.')
    return Response(text, media_type='application/json')


def install_canonical_reads(app, reads, *, store, security, auth, audit):
    def context(request):
        with store.transaction() as c:
            user, key = auth(c, request, 'read')
            # No credentials or stored authentication hashes enter the context.
            credential = {'kind': 'key', 'id': key['public_id'], 'scopes': sorted(key['scopes']),
                          'sources': sorted(key['sources']), 'expires_at': key['expires_at']} if key else {
                          'kind': 'session', 'id': hashed(request.cookies.get('bigbase_session', ''))}
            revision = digest({'permissions': sorted(user['permissions']), 'role': user['role'], 'credential': credential})
            authorization = {'active': True, 'can_search': True, 'revision': revision,
                             'scopes': sorted(set(user['permissions']) & set(key['scopes'])) if key else sorted(user['permissions'])}
            return user, key, authorization

    @contextmanager
    def available(request):
        user, key, authorization = context(request)
        if reads is None:
            failure(503, 'CANONICAL_READS_DISABLED', 'Leitura canônica não configurada.')
        try:
            reads.verify()
            yield user, key, authorization
        except InvalidCursor:
            failure(422, 'INVALID_CANONICAL_CURSOR', 'Cursor inválido ou expirado; reabra a ficha.')
        except CanonicalRowTooLarge:
            failure(413, 'CANONICAL_ROW_TOO_LARGE', 'Registro exige entrega em arquivo; página não foi truncada.')
        except CanonicalError as exc:
            if str(exc) == 'Entity does not exist':
                failure(404, 'CANONICAL_ENTITY_NOT_FOUND', 'Cadastro canônico não encontrado.')
            failure(422, 'INVALID_CANONICAL_READ', 'Parâmetros de leitura canônica inválidos.')
        except InvalidSearchCursor:
            failure(422, 'INVALID_SEARCH_CURSOR', 'Cursor de pesquisa inválido ou expirado.')
        except SearchReadError as exc:
            code = str(exc)
            if code == 'SEARCH_SORT_NOT_IMPLEMENTED' or code.startswith(('UNSUPPORTED_', 'INVALID_FILTER', 'UNKNOWN_')):
                failure(422, 'UNSUPPORTED_CANONICAL_SEARCH', 'Critério ou ordenação ainda não suportado.')
            failure(503, 'CANONICAL_SEARCH_UNAVAILABLE', 'Pesquisa canônica temporariamente indisponível.')
        except (psycopg.Error, ValueError):
            failure(503, 'CANONICAL_READS_UNAVAILABLE', 'Destino canônico indisponível ou divergente.')

    def typed_collection(collection):
        if collection not in {'people', 'companies'}:
            failure(404, 'CANONICAL_COLLECTION_NOT_FOUND', 'Coleção não encontrada.')
        return 'person' if collection == 'people' else 'company'

    def owner_id(value):
        try:
            return str(UUID(value))
        except (TypeError, ValueError):
            failure(422, 'INVALID_ENTITY_ID', 'Identificador inválido.')

    def audit_read(user, action, target):
        with store.transaction() as c:
            audit(c, user, action, target)

    def binding(user, authorization, collection, owner, kind, filters, order):
        return digest({'principal': user['id'], 'authorization': authorization, 'deployment': reads.deployment_id,
                       'collection': collection, 'owner': owner, 'kind': kind, 'filters': filters, 'order': order})

    def wrap(token, bound):
        if token is None:
            return None
        return 'ch1_' + security.cipher.encrypt(json.dumps({'token': token, 'binding': bound}).encode()).decode()

    def unwrap(token, bound):
        if not isinstance(token, str) or not token.startswith('ch1_') or len(token) > 4096:
            failure(422, 'INVALID_CANONICAL_CURSOR', 'Cursor inválido ou expirado; reabra a ficha.')
        try:
            value = json.loads(security.cipher.decrypt(token[4:].encode('ascii'), ttl=3600))
            if value['binding'] != bound or not isinstance(value['token'], str):
                raise ValueError()
            return value['token']
        except (InvalidToken, ValueError, KeyError, TypeError):
            failure(422, 'INVALID_CANONICAL_CURSOR', 'Cursor inválido ou expirado; reabra a ficha.')

    def filters_for(kind, item_id=None):
        return {'kind': None} if kind == 'items' else {'item_id': item_id, 'field_path': None, 'source_id': None, 'dimension': None}

    def wrap_children(value, user, authorization, collection, owner, *, item_id=None):
        for kind in ('items', 'fields', 'history'):
            if kind in value:
                bound = binding(user, authorization, collection, owner, kind, filters_for(kind, item_id), 'asc')
                value[kind]['cursor'] = wrap(value[kind]['cursor'], bound)
        return value

    def metadata(collection, owner, user, authorization):
        data = reads.repository.entity_metadata(owner)
        if data['entity_type'] != typed_collection(collection):
            failure(404, 'CANONICAL_ENTITY_NOT_FOUND', 'Cadastro canônico não encontrado.')
        return wrap_children(data, user, authorization, collection, owner)

    def strict_body(body, allowed):
        if set(body) - set(allowed):
            failure(422, 'UNSUPPORTED_CANONICAL_PARAMETER', 'Parâmetro não suportado; nenhum critério foi ignorado.')

    @app.get('/api/v1/canonical/status')
    def status(request: Request):
        user, key, _ = context(request)
        scopes = set(user['permissions']) & set(key['scopes']) if key else set(user['permissions'])
        if reads is not None:
            try:
                reads.verify()
            except (psycopg.Error, ValueError, CanonicalError):
                failure(503, 'CANONICAL_READS_UNAVAILABLE', 'Destino canônico indisponível ou divergente.')
        return {'enabled': reads is not None, 'environment': getattr(reads, 'environment', 'synthetic') if reads else None,
                'writes_enabled': bool(reads and reads.writes_enabled),
                'can_enrich': 'enrich' in scopes, 'can_validate': 'validate' in scopes,
                'can_administer_catalog': 'admin' in scopes and key is None,
                'search_enabled': (reads.search_enabled() if hasattr(reads, 'search_enabled') else bool(reads and reads.search_reader)),
                'search_coverage': getattr(getattr(reads, 'search_resource', None), 'coverage_scope', None),
                'search_fields': {kind:sorted(fields) for kind,fields in FIELDS.items()},
                'search_flags': list(FLAGS), 'search_projection_version': PROJECTION_VERSION,
                'auth_storage': getattr(reads, 'auth_storage', 'sqlite-local-single-process'),
                'runtime': 'deployed' if getattr(reads, 'environment', None) in {'staging', 'production'} else 'development',
                'production_connected': getattr(reads, 'environment', None) == 'production'}

    @app.post('/api/v1/canonical/{collection}/lookup')
    def lookup(collection: str, body: dict, request: Request):
        with available(request) as (user, key, authorization):
            typed_collection(collection)
            strict_body(body, ('source_id', 'source_record_id', 'document_type', 'country', 'value'))
            if any(not isinstance(v, str) or not v or len(v) > 2048 for v in body.values()):
                failure(422, 'INVALID_IDENTITY', 'Informe uma identidade textual completa, até 2048 caracteres por componente.')
            if key and 'source_id' in body and body['source_id'] not in key['sources']:
                failure(403, 'SOURCE_NOT_ALLOWED', 'Origem não permitida para esta chave.')
            owner = reads.repository.lookup_identity(**body)
            if owner is None:
                failure(404, 'CANONICAL_ENTITY_NOT_FOUND', 'Cadastro canônico não encontrado.')
            result = metadata(collection, owner, user, authorization)
            audit_read(user, 'canonical_lookup', owner)
            return exact_response(result)

    @app.get('/api/v1/canonical/{collection}/{id}')
    def entity(collection: str, id: str, request: Request):
        with available(request) as (user, key, authorization):
            typed_collection(collection)
            owner = owner_id(id)
            result = metadata(collection, owner, user, authorization)
            audit_read(user, 'canonical_read', owner)
            return exact_response(result)

    @app.post('/api/v1/canonical/{collection}/search')
    def search(collection: str, body: dict, request: Request):
        with available(request) as (user, key, authorization):
            entity_type = typed_collection(collection)
            strict_body(body, ('filters', 'sort', 'page_size', 'include_pending', 'include_invalid', 'cursor'))
            if reads.search_reader is None:
                failure(503, 'CANONICAL_SEARCH_DISABLED', 'Pesquisa canônica não configurada.')
            size = body.get('page_size', 20)
            if type(size) is not int or not 1 <= size <= 20:
                failure(422, 'INVALID_SEARCH_PAGE_SIZE', 'Informe de 1 a 20 resultados por página.')
            if any(type(body.get(name, False)) is not bool for name in ('include_pending', 'include_invalid')):
                failure(422, 'INVALID_SEARCH_FLAG', 'Informe flags booleanas.')
            if body.get('sort') not in (None, [{'field': 'id', 'direction': 'asc'}]):
                failure(422, 'UNSUPPORTED_CANONICAL_SORT', 'A pesquisa canônica suporta somente ID crescente.')
            try:
                build_query(body.get('filters', {}), entity_type=entity_type,
                            include_pending=body.get('include_pending', False), include_invalid=body.get('include_invalid', False))
            except ProjectionError:
                failure(422, 'UNSUPPORTED_CANONICAL_SEARCH', 'Critério ainda não suportado.')
            result = reads.search_reader.search(body.get('filters', {}), principal_id=user['id'], authorization=authorization,
                entity_type=entity_type, page_size=size, sort=body.get('sort'), cursor=body.get('cursor'),
                include_pending=body.get('include_pending', False), include_invalid=body.get('include_invalid', False))
            try:
                for hit in result['items']:
                    entity = metadata(collection, hit['id'], user, authorization)
                    indexed_version = hit['record_version']
                    if entity['version'] < indexed_version:
                        failure(503, 'CANONICAL_SEARCH_VERSION_MISMATCH', 'Versão do cadastro diverge da projeção.')
                    hit.update(entity=entity, record_version=entity['version'], indexed_version=indexed_version,
                               indexing_pending=entity['version'] != indexed_version)
            except Exception:
                if body.get('cursor') is None:
                    reads.search_reader.close(result['release_cursor'], principal_id=user['id'], authorization=authorization)
                raise
            audit_read(user, 'canonical_search', collection)
            return exact_response(result)

    @app.post('/api/v1/canonical-search/close')
    def close_search(body: dict, request: Request):
        with available(request) as (user, key, authorization):
            strict_body(body, ('release_cursor',))
            if reads.search_reader is None:
                failure(503, 'CANONICAL_SEARCH_DISABLED', 'Pesquisa canônica não configurada.')
            return reads.search_reader.close(body.get('release_cursor'), principal_id=user['id'], authorization=authorization)

    @app.post('/api/v1/canonical/{collection}/{id}/{kind}')
    def page(collection: str, id: str, kind: str, body: dict, request: Request):
        with available(request) as (user, key, authorization):
            expected_type = typed_collection(collection)
            owner = owner_id(id)
            if kind not in {'items', 'fields', 'history'}:
                failure(404, 'CANONICAL_COLLECTION_NOT_FOUND', 'Coleção não encontrada.')
            filters = filters_for(kind)
            strict_body(body, (*filters, 'cursor', 'limit', 'order'))
            filters.update({name: body[name] for name in filters if name in body})
            limit, order = body.get('limit', 20), body.get('order', 'asc')
            if type(limit) is not int or not 1 <= limit <= 200 or order not in ('asc', 'desc'):
                failure(422, 'INVALID_PAGE', 'Informe limite de 1 a 200 e ordem asc ou desc.')
            if any(value is not None and (not isinstance(value, str) or len(value) > 2048) for value in filters.values()):
                failure(422, 'INVALID_PAGE_FILTER', 'Filtro textual inválido.')
            if filters.get('item_id') is not None:
                filters['item_id'] = owner_id(filters['item_id'])
            bound = binding(user, authorization, collection, owner, kind, filters, order)
            cursor = unwrap(body['cursor'], bound) if body.get('cursor') is not None else None
            # Constant-size metadata only; never get_entity's unbounded hydration.
            with reads.repository.connection() as c:
                row = c.execute('SELECT entity_type FROM entities WHERE owner_id=%s', (UUID(owner),)).fetchone()
                if not row or row['entity_type'] != expected_type:
                    failure(404, 'CANONICAL_ENTITY_NOT_FOUND', 'Cadastro canônico não encontrado.')
            result = getattr(reads.repository, 'page_' + kind)(owner, **filters, order=order, limit=limit, cursor=cursor)
            result['next_cursor'] = wrap(result['next_cursor'], bound)
            if kind == 'items':
                for item in result['items']:
                    wrap_children(item, user, authorization, collection, owner, item_id=item['id'])
            audit_read(user, 'canonical_' + kind, owner)
            return exact_response(result)

"""Directed scalar observations; identities and confirmations are independent."""
from decimal import Decimal
from uuid import UUID

import psycopg
from fastapi import Request
from starlette.concurrency import run_in_threadpool

from .canonical_enrichment import strict, text
from .canonical_http import exact_response, failure
from .canonical_store import CanonicalError, VersionConflict, IdempotencyConflict, decode, json_text
from .domain import timestamp

CONTRACT_VERSION = 'canonical-http-scalar-2026-09-09.1'


def prepare_scalar_patch(body):
    strict(body, ('source_id', 'expected_version', 'field_path', 'value', 'observed_at', 'source_updated_at', 'reason'),
           ('source_id', 'expected_version', 'field_path', 'value'))
    text(body['source_id'], 120)
    text(body['field_path'], 16000)
    if type(body['expected_version']) is not int or not 1 <= body['expected_version'] < 2**63 - 1:
        raise CanonicalError('Expected positive entity version')
    value = body['value']
    if value is not None and type(value) not in (str, bool, int, Decimal):
        raise CanonicalError('Expected an exact JSON scalar')
    json_text(value)  # Reject nonfinite decimals and unsupported numbers before opening a transaction.
    for name in ('observed_at', 'source_updated_at'):
        timestamp(body.get(name))
    if 'reason' in body:
        text(body['reason'], 2000)
    return body


def install_canonical_scalar_patch(app, reads, *, store, auth, source_check):
    @app.patch('/api/v1/canonical/{collection}/{owner_id}/items/{item_id}/value')
    async def patch_value(collection: str, owner_id: str, item_id: str, request: Request):
        raw = await request.body()

        def execute():
            with store.transaction() as c:
                user, key = auth(c, request, 'enrich')
                if reads is None or not reads.writes_enabled:
                    failure(503, 'CANONICAL_WRITES_DISABLED', 'Escrita canônica sintética não configurada.')
                if collection not in {'people', 'companies'}:
                    failure(404, 'CANONICAL_COLLECTION_NOT_FOUND', 'Coleção não encontrada.')
                try:
                    UUID(owner_id); UUID(item_id)
                    body = decode(raw)
                    prepare_scalar_patch(body)
                except (ValueError, TypeError, RecursionError):
                    failure(422, 'INVALID_CANONICAL_VALUE_PATCH', 'Edição inválida; confira campo, valor escalar, versão, motivo e datas.')
                source_check(c, body['source_id'], key)
                request_key = request.headers.get('Idempotency-Key', '')
                if not request_key or len(request_key) > 200:
                    failure(422, 'IDEMPOTENCY_KEY_REQUIRED', 'Idempotency-Key obrigatório, até 200 caracteres.')
            try:
                reads.verify()
                receipt = reads.repository.apply_scalar_patch(body, owner_id=owner_id, item_id=item_id,
                    entity_type='person' if collection == 'people' else 'company', actor_id=user['id'],
                    api_key_id=key['public_id'] if key else None, request_key=request_key)
                return exact_response({**receipt, 'environment': 'synthetic', 'production_connected': False})
            except VersionConflict:
                failure(409, 'CANONICAL_VERSION_CONFLICT', 'A versão mudou; reabra a ficha antes de editar.')
            except IdempotencyConflict:
                failure(409, 'CANONICAL_IDEMPOTENCY_CONFLICT', 'Chave idempotente reutilizada com conteúdo diferente.')
            except CanonicalError:
                failure(422, 'INVALID_CANONICAL_VALUE_PATCH', 'Campo escalar inexistente ou observação inválida; operação não aplicada.')
            except (psycopg.Error, ValueError):
                failure(503, 'CANONICAL_WRITES_UNAVAILABLE', 'Destino canônico sintético indisponível; repita com a mesma chave.')
        return await run_in_threadpool(execute)

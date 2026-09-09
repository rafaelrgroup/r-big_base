"""Dedicated, value-bound flag observations in the synthetic canonical store."""
from uuid import UUID

import psycopg
from fastapi import Request
from starlette.concurrency import run_in_threadpool

from .canonical_enrichment import strict, text
from .canonical_http import exact_response, failure
from .canonical_store import CanonicalError, VersionConflict, IdempotencyConflict, decode
from .domain import FLAGS, FlagEvidence, timestamp

CONTRACT_VERSION = 'canonical-http-flags-2026-09-09.1'


def prepare_validation(body):
    strict(body, ('source_id', 'expected_version', 'field_path', 'value_observation_id', 'flags'),
           ('source_id', 'expected_version', 'field_path', 'value_observation_id', 'flags'))
    text(body['source_id'], 120)
    text(body['field_path'], 16000)
    UUID(text(body['value_observation_id'], 36))
    if type(body['expected_version']) is not int or not 1 <= body['expected_version'] < 2**63 - 1:
        raise CanonicalError('Expected positive entity version')
    flags = body['flags']
    if not isinstance(flags, dict) or not flags or set(flags) - FLAGS:
        raise CanonicalError('Expected known confirmation flags')
    result = {}
    for name, flag in flags.items():
        strict(flag, ('value', 'reason', 'observed_at', 'source_updated_at', 'checked_at', 'expires_at', 'method', 'reference'), ('value',))
        if flag['value'] is not None and type(flag['value']) is not bool:
            raise CanonicalError('Flag requires boolean or null')
        FlagEvidence.model_validate({k: v for k, v in flag.items() if k in {'value', 'checked_at', 'expires_at', 'method', 'reference'}})
        for date in ('observed_at', 'source_updated_at'):
            timestamp(flag.get(date))
        if 'reason' in flag:
            text(flag['reason'], 2000)
        if name == 'valid' and flag['value'] is False and not flag.get('reason'):
            raise CanonicalError('Invalidation requires a reason')
        result[name] = dict(flag)
        if flag.get('checked_at') is not None and 'observed_at' not in flag:
            result[name]['observed_at'] = flag['checked_at']
    return {**body, 'flags': result}


def install_canonical_validation(app, reads, *, store, auth, source_check):
    @app.patch('/api/v1/canonical/{collection}/{owner_id}/items/{item_id}/flags')
    async def validate(collection: str, owner_id: str, item_id: str, request: Request):
        raw = await request.body()

        def execute():
            with store.transaction() as c:
                user, key = auth(c, request, 'validate')
                if reads is None or not reads.writes_enabled:
                    failure(503, 'CANONICAL_WRITES_DISABLED', 'Escrita canônica não configurada.')
                if collection not in {'people', 'companies'}:
                    failure(404, 'CANONICAL_COLLECTION_NOT_FOUND', 'Coleção não encontrada.')
                try:
                    UUID(owner_id); UUID(item_id)
                    body = decode(raw)
                    prepare_validation(body)
                except (ValueError, TypeError, RecursionError):
                    failure(422, 'INVALID_CANONICAL_VALIDATION', 'Validação inválida; confira valor de referência, flags, motivo e datas.')
                source_check(c, body['source_id'], key)
                request_key = request.headers.get('Idempotency-Key', '')
                if not request_key or len(request_key) > 200:
                    failure(422, 'IDEMPOTENCY_KEY_REQUIRED', 'Idempotency-Key obrigatório, até 200 caracteres.')
            try:
                reads.verify()
                receipt = reads.repository.apply_validation(body, owner_id=owner_id, item_id=item_id,
                    entity_type='person' if collection == 'people' else 'company', actor_id=user['id'],
                    api_key_id=key['public_id'] if key else None, request_key=request_key)
                return exact_response({**receipt, 'environment': getattr(reads, 'environment', 'synthetic'), 'production_connected': getattr(reads, 'environment', None) == 'production'})
            except VersionConflict:
                failure(409, 'CANONICAL_VERSION_CONFLICT', 'A versão mudou; reabra a ficha antes de validar.')
            except IdempotencyConflict:
                failure(409, 'CANONICAL_IDEMPOTENCY_CONFLICT', 'Chave idempotente reutilizada com conteúdo diferente.')
            except CanonicalError:
                failure(422, 'INVALID_CANONICAL_VALIDATION', 'Referência de valor ou observações inválidas; operação não aplicada.')
            except (psycopg.Error, ValueError):
                failure(503, 'CANONICAL_WRITES_UNAVAILABLE', 'Destino canônico indisponível; repita com a mesma chave.')
        return await run_in_threadpool(execute)

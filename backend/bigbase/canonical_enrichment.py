"""Bounded HTTP field observations; the server owns provenance and identities."""
from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import psycopg
from fastapi import Request

from .canonical_store import (CanonicalError, IdentityConflict, IdempotencyConflict,
                              VersionConflict, decode, digest, json_text)
from .canonical_http import exact_response, failure
from .domain import FLAGS, KINDS, FlagEvidence, normalize, timestamp, NORMALIZER_VERSION
from .canonical_phone import normalize_item_phone, PHONE_CONTRACT
from .canonical_email import normalize_item_email, EMAIL_CONTRACT
from .canonical_postal import normalize_item_postal, POSTAL_CONTRACT
from .canonical_username import normalize_item_username, USERNAME_CONTRACT
from .canonical_fields import prepare_item_field, bind_fields, FIELD_CONTRACT


CONTRACT_VERSION = 'canonical-http-fields-2026-09-08.1'
MAX_ATOMS = 1000


def strict(obj, allowed, required=()):
    if not isinstance(obj, dict) or set(obj) - set(allowed) or set(required) - set(obj):
        raise CanonicalError('Unsupported or missing enrichment parameter')


def text(value, limit=2048):
    if not isinstance(value, str) or not value or len(value) > limit:
        raise CanonicalError('Expected bounded nonempty text')
    return value


def escape(value):
    return str(value).replace('~', '~0').replace('/', '~1')


def prepare_enrichment(body, entity_type, *, api_key_id=None):
    strict(body, ('source_id', 'source_record_id', 'entity_id', 'expected_version', 'document', 'items'),
           ('source_id', 'source_record_id', 'expected_version', 'items'))
    source = text(body['source_id'], 120)
    external = text(body['source_record_id'])
    version = body['expected_version']
    if type(version) is not int or not 0 <= version < 2**63 - 1:
        raise CanonicalError('Expected version must be a nonnegative integer')
    if 'entity_id' in body:
        UUID(text(body['entity_id']))
    items = body['items']
    if not isinstance(items, list) or not 1 <= len(items) <= 100:
        raise CanonicalError('Enrichment requires 1 to 100 items')
    facts, containers, targets = [], [], set()

    def atom(value, source_path, target, kind, key, metadata, flags=None, normalized=None, transformed=False):
        if len(facts) >= MAX_ATOMS:
            raise CanonicalError('Enrichment exceeds 1000 field atoms')
        address = (kind, key, target)
        if address in targets:
            raise CanonicalError('Repeated target field in the same operation')
        targets.add(address)
        typ = ('null' if value is None else 'boolean' if type(value) is bool else 'integer' if type(value) is int
               else 'decimal' if isinstance(value, Decimal) else 'empty_object' if value == {} else 'empty_array' if value == [] else 'text')
        facts.append({'id': digest(source_path), 'source_path': source_path, 'target_path': target,
                      'target_kind': kind, 'item_key': key, 'input_value': value, 'input_type': typ,
                      'normalized_value': normalized if transformed else value, 'status': 'unknown',
                      'normalization': NORMALIZER_VERSION if transformed else 'preserved-no-inference',
                      'api_key_id': api_key_id, **metadata, 'flags': flags or {}})

    def walk(value, source_path, target, kind, key, metadata, depth=0):
        if depth > 24:
            raise CanonicalError('Structured field exceeds 24 levels')
        if isinstance(value, (list, dict)):
            containers.append({'source_path': source_path, 'type': 'array' if isinstance(value, list) else 'object', 'length': len(value)})
            if value:
                for name, child in (enumerate(value) if isinstance(value, list) else value.items()):
                    walk(child, source_path+'/'+escape(name), target+'/'+escape(name), kind, key, metadata, depth+1)
                return
        atom(value, source_path, target, kind, key, metadata)

    candidate = None
    if 'document' in body:
        doc = body['document']
        strict(doc, ('type', 'country', 'value'), ('type', 'country', 'value'))
        for value in doc.values(): text(value, 100)
        expected_type = 'CPF' if entity_type == 'person' else 'CNPJ'
        normalized, _ = normalize('document', {'type': doc['type'], 'country': doc['country'], 'number': doc['value']})
        if normalized['country'] != 'BR' or normalized['type'] != expected_type or normalized['syntax_valid'] is not True:
            raise CanonicalError('Identity document must be a valid Brazilian CPF/CNPJ for the collection')
        candidate = {'country': 'BR', 'type': expected_type, 'value': normalized['number']}
        key = 'http-document:'+digest(candidate)
        for field, target in [('type', 'type'), ('country', 'country'), ('value', 'number')]:
            atom(doc[field], '/document/'+field, target, 'document', key, {}, normalized=normalized[target], transformed=True)

    seen_items = set()
    for index, item in enumerate(items):
        strict(item, ('kind', 'key', 'fields', 'phone_normalization', 'email_normalization', 'postal_normalization', 'username_normalization', 'custom_field'), ('kind', 'key', 'fields'))
        kind, key = text(item['kind'], 120), text(item['key'], 500)
        if kind not in KINDS or kind == 'relationship':
            raise CanonicalError('Unsupported item kind; relationships require their own contract')
        if (kind, key) in seen_items:
            raise CanonicalError('Repeated item key')
        seen_items.add((kind, key))
        first_fact = len(facts)
        fields = item['fields']
        if not isinstance(fields, list) or not 1 <= len(fields) <= 100:
            raise CanonicalError('Item requires 1 to 100 fields')
        seen_fields = set()
        for position, field in enumerate(fields):
            strict(field, ('path', 'value', 'observed_at', 'source_updated_at', 'reason', 'flags'), ('path', 'value'))
            path = text(field['path'], 500)
            if path in seen_fields:
                raise CanonicalError('Repeated field name in the same item')
            seen_fields.add(path)
            # Scalar paths never share the slash namespace used for structured children.
            if '/' in path or '~' in path:
                raise CanonicalError('Field name cannot contain slash or tilde')
            metadata = {name: field[name] for name in ('observed_at', 'source_updated_at', 'reason') if name in field}
            for name in ('observed_at', 'source_updated_at'): timestamp(metadata.get(name))
            if 'reason' in metadata and (not isinstance(metadata['reason'], str) or len(metadata['reason']) > 2000):
                raise CanonicalError('Invalid observation reason')
            flags = field.get('flags', {})
            if not isinstance(flags, dict) or set(flags) - FLAGS:
                raise CanonicalError('Invalid confirmation flags')
            for flag in flags.values():
                strict(flag, ('value', 'observed_at', 'source_updated_at', 'checked_at', 'expires_at', 'method', 'reference', 'confirmed_value'), ('value',))
                if flag['value'] is not None and type(flag['value']) is not bool:
                    raise CanonicalError('Flag requires boolean or null')
                FlagEvidence.model_validate({k: v for k, v in flag.items() if k in {'value', 'checked_at', 'expires_at', 'method', 'reference'}})
                for name in ('observed_at', 'source_updated_at'): timestamp(flag.get(name))
            source_path = f'/items/{index}/fields/{position}/value'
            value = field['value']
            if isinstance(value, (dict, list)):
                if flags:
                    raise CanonicalError('Confirmations require an explicit scalar field')
                walk(value, source_path, path, kind, key, metadata)
            else:
                prepared_flags = {}
                for name, flag in flags.items():
                    prepared_flags[name] = {k: v for k, v in flag.items() if k != 'confirmed_value'}
                    if flag.get('checked_at') is not None and 'observed_at' not in flag:
                        prepared_flags[name]['observed_at'] = flag['checked_at']
                    if 'confirmed_value' in flag:
                        prepared_flags[name]['confirmed_value_json'] = json_text(flag['confirmed_value'])
                atom(value, source_path, path, kind, key, metadata, prepared_flags)
        if 'custom_field' in item:
            prepare_item_field(item, facts[first_fact:])
        if 'phone_normalization' in item:
            normalize_item_phone(item, facts[first_fact:])
        if 'email_normalization' in item:
            normalize_item_email(item, facts[first_fact:])
        if 'postal_normalization' in item:
            normalize_item_postal(item, facts[first_fact:])
        if 'username_normalization' in item:
            normalize_item_username(item, facts[first_fact:])
    result = {'source_id': source, 'source_record_id': external, 'entity_type': entity_type,
              'adapter_version': (FIELD_CONTRACT if any('custom_field' in item for item in items) else
                                  USERNAME_CONTRACT if any('username_normalization' in item for item in items) else
                                  POSTAL_CONTRACT if any('postal_normalization' in item for item in items) else
                                  EMAIL_CONTRACT if any('email_normalization' in item for item in items) else
                                  PHONE_CONTRACT if any('phone_normalization' in item for item in items) else CONTRACT_VERSION),
              'record_hash': digest(body),
              'normalizer_version': NORMALIZER_VERSION, 'facts': facts, 'containers': containers}
    if candidate is not None: result['identity_candidate'] = candidate
    return result


def install_canonical_writes(app, reads, *, store, auth, source_check):
    @app.post('/api/v1/canonical/{collection}/enrich')
    async def enrich(collection: str, request: Request):
        # Read raw bytes again with Decimal; the shared middleware checks duplicate
        # keys/size and permits exact decimal input only on this explicit route.
        from starlette.concurrency import run_in_threadpool
        raw = await request.body()

        def execute():
            with store.transaction() as c:
                user, key = auth(c, request, 'enrich')
                if reads is None or not reads.writes_enabled:
                    failure(503, 'CANONICAL_WRITES_DISABLED', 'Escrita canônica não configurada.')
                if collection not in {'people', 'companies'}:
                    failure(404, 'CANONICAL_COLLECTION_NOT_FOUND', 'Coleção não encontrada.')
                try:
                    body = decode(raw)
                    record = prepare_enrichment(body, 'person' if collection == 'people' else 'company',
                                                api_key_id=key['public_id'] if key else None)
                except (CanonicalError, ValueError, TypeError, RecursionError):
                    failure(422, 'INVALID_CANONICAL_ENRICHMENT', 'Enriquecimento inválido; confira campos, valores, datas e identidade.')
                source_check(c, record['source_id'], key)
                has_custom = any('custom_field' in item for item in body['items'])
                if getattr(reads,'environment',None) in {'staging','production'} and any(item.get('custom_field',{}).get('contract') == FIELD_CONTRACT for item in body['items']):
                    failure(422,'POSTGRESQL_CATALOG_REQUIRED','Use o contrato do catálogo PostgreSQL; definições locais não são aceitas na implantação.')
                definitions = {item['custom_field']['field_id']: store.get(c, 'field', item['custom_field']['field_id'])
                               for item in body['items'] if item.get('custom_field', {}).get('contract') == FIELD_CONTRACT}
                if any(fact['flags'] for fact in record['facts']):
                    # Same principal and fresh permission resolution; no client-supplied actor.
                    auth(c, request, 'validate')
                request_key = request.headers.get('Idempotency-Key', '')
                if not request_key or len(request_key) > 200:
                    failure(422, 'IDEMPOTENCY_KEY_REQUIRED', 'Idempotency-Key obrigatório, até 200 caracteres.')
            try:
                reads.verify()
                receipt = reads.repository.apply_enrichment(record, actor_id=user['id'], request_key=request_key,
                    expected_version=body['expected_version'], expected_owner=body.get('entity_id'),
                    validate_record=(lambda pg: bind_fields(record, body, definitions, pg)) if has_custom else None)
                # The immutable canonical operation/observations are the write audit.
                # Do not make a committed write depend on a second SQLite commit.
                return exact_response({**receipt, 'environment': getattr(reads, 'environment', 'synthetic'), 'production_connected': getattr(reads, 'environment', None) == 'production'})
            except VersionConflict:
                failure(409, 'CANONICAL_VERSION_CONFLICT', 'A versão mudou; reabra a ficha antes de editar.')
            except IdempotencyConflict:
                failure(409, 'CANONICAL_IDEMPOTENCY_CONFLICT', 'Chave idempotente reutilizada com conteúdo diferente.')
            except IdentityConflict:
                failure(409, 'CANONICAL_IDENTITY_CONFLICT', 'Identidades divergentes; resolução explícita necessária.')
            except CanonicalError as exc:
                failure(422, 'INVALID_CANONICAL_ENRICHMENT', str(exc) if has_custom else 'Observações inválidas; operação não aplicada.')
            except (psycopg.Error, ValueError):
                failure(503, 'CANONICAL_WRITES_UNAVAILABLE', 'Destino canônico indisponível ou divergente; repita com a mesma chave.')
        return await run_in_threadpool(execute)

"""Shared entity enrichment for synchronous requests and background imports.

The caller owns the transaction and must enforce authorization, active source,
rate limits, idempotency and audit. This service never commits independently;
an exception must roll back the caller's transaction (or per-entry savepoint).
"""
from datetime import datetime, timezone, timedelta

from fastapi import HTTPException

from .catalogs import validate_custom, bind_definition
from .domain import (
    Enrichment, NORMALIZER_VERSION, add_observation, fingerprint, item_key,
    normalize, now, project, record_flags, timestamp, uid,
)


def enrich_entity(store, connection, body: Enrichment, entity_type: str, actor_id: str):
    """Validate, project and persist one entry in the existing transaction."""
    if entity_type not in {'person', 'company'} or body.entity_type != entity_type:
        raise HTTPException(422, 'Tipo não corresponde à rota')

    def get_entity(entity_id):
        entity = store.get(connection, 'entity', entity_id)
        if not entity:
            raise HTTPException(404, 'Cadastro não encontrado')
        return project(entity)

    normalized = [(item, *normalize(item.kind, item.value)) for item in body.items]
    custom_metadata = [
        validate_custom(store, connection, value, entity_type) if item.kind == 'custom' else None
        for item, value, _ in normalized
    ]
    effective_date = body.source_updated_at or body.observed_at
    future = bool(effective_date and timestamp(effective_date) > datetime.now(timezone.utc) + timedelta(minutes=5))
    documents = [] if future else [
        item_key('document', value) for item, value, _ in normalized
        if item.kind == 'document' and value.get('syntax_valid') and item.flags.get('valid') is not False
    ]
    existing = store.all(connection, 'entity')
    owners = {
        entity['id'] for entity in existing for item in entity['items']
        if item['kind'] == 'document' and item['value'].get('syntax_valid') is True
        and item['key'] in documents and item['flags'].get('valid') is not False
    }
    if body.external_id:
        owners.update(entity['id'] for entity in existing if any(
            observation.get('external_id') == body.external_id and observation['source_id'] == body.source_id
            for observation in entity['observations']
        ))
    if len(owners) > 1 or (body.entity_id and owners and body.entity_id not in owners):
        raise HTTPException(409, 'Documento atribuído a outro cadastro; revisão necessária')
    if body.entity_id:
        entity = get_entity(body.entity_id)
    elif owners:
        entity = get_entity(next(iter(owners)))
    else:
        entity = {
            'id': uid(), 'entity_type': entity_type, 'name': 'Sem nome informado', 'version': 0,
            'items': [], 'observations': [], 'created_at': now(), 'updated_at': now(),
        }
    if entity['entity_type'] != entity_type:
        raise HTTPException(409, 'Documento em tipo de entidade incompatível')

    operation = uid()
    received = now()
    for input_index, (incoming, value, notes) in enumerate(normalized):
        if incoming.kind == 'relationship' and not any(value.get(key) for key in ('target_id', 'target_document', 'target_name')):
            raise HTTPException(422, 'Informe documento, ID ou nome do cadastro relacionado')
        if incoming.kind == 'relationship' and value.get('target_id') == entity['id']:
            raise HTTPException(422, 'Um vínculo não pode apontar para o próprio cadastro')
        key = item_key(incoming.kind, value)
        item = next((item for item in entity['items'] if item['kind'] == incoming.kind and item['key'] == key), None)
        if item is None:
            item = {'id': uid(), 'kind': incoming.kind, 'key': key, 'fields': {}, 'sources': [], 'notes': notes, 'version': 0}
            entity['items'].append(item)
        before = len(entity['observations'])
        for field, field_value in value.items():
            raw = incoming.value.get(field, field_value)
            add_observation(entity, item, 'value.' + field, field_value, body.source_id, effective_date,
                            actor_id, operation, body.reason, received, raw)
            entity['observations'][-1]['normalization'] = {
                'version': NORMALIZER_VERSION,
                'input_path': '/items/' + str(input_index) + '/value/' + field.replace('~', '~0').replace('/', '~1'),
                'derived': field not in incoming.value, 'changed': fingerprint(raw) != fingerprint(field_value),
            }
        record_flags(entity, item, incoming, body.source_id, effective_date, actor_id,
                     operation, body.reason, received, confirmed_values=value)
        for observation in entity['observations'][before:]:
            observation.update(source_updated_at=body.source_updated_at, source_observed_at=body.observed_at,
                               external_id=body.external_id)
        if custom_metadata[input_index]:
            bind_definition(item, entity['observations'][before:], custom_metadata[input_index])
        item['version'] += 1
    entity['version'] += 1
    entity['updated_at'] = received
    project(entity)
    store.put(connection, 'entity', entity)
    return entity

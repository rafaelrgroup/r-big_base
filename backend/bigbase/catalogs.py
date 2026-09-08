"""Versioned custom-field definitions for the isolated development adapter.

Values are scalar per custom item; ``multiple`` describes the field's intended
cardinality, not permission to destroy competing evidence. Alternative values
always remain separate items with independent flags. Unknown field IDs are
accepted as pending classification so an importer cannot silently discard them.
"""
from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation
import math
import re
from typing import Literal
from urllib.parse import urlsplit

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, field_validator, model_validator

from .domain import fingerprint, now, uid

FIELD_TYPES = Literal['text', 'integer', 'decimal', 'boolean', 'date', 'enum', 'url', 'reference']
SCOPE = Literal['person', 'company', 'both']
VALUE_POLICY = 'scalar_per_item_alternatives_preserved'
IMMUTABLE = {'id', 'type', 'multiple', 'scope', 'options'}


class FieldDefinitionInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: StrictStr | None = Field(default=None, pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$')
    name: StrictStr = Field(min_length=1, max_length=160)
    type: FIELD_TYPES
    multiple: StrictBool = True
    scope: SCOPE = 'both'
    options: list[StrictStr] = Field(default_factory=list, max_length=500)

    @field_validator('name')
    @classmethod
    def name_not_blank(cls, value):
        if not value.strip():
            raise ValueError('Nome não pode ficar vazio')
        return value.strip()

    @model_validator(mode='after')
    def enum_options(self):
        if self.type == 'enum':
            if not self.options or any(not v.strip() or len(v) > 160 for v in self.options):
                raise ValueError('Enum exige opções textuais não vazias, até 160 caracteres')
            if len(set(self.options)) != len(self.options):
                raise ValueError('As opções do enum devem ser únicas')
        elif self.options:
            raise ValueError('Opções só se aplicam a campos enum')
        return self


def public_definition(record):
    """Fill legacy presentation defaults without writing to its persisted record."""
    result = deepcopy(record)
    for key, value in {'version': 1, 'active': True, 'multiple': True, 'scope': 'both',
                       'options': [], 'search_state': 'pending'}.items():
        result.setdefault(key, value)
    result['value_policy'] = VALUE_POLICY
    if 'definition_history' not in result:
        result['definition_history'] = [snapshot(result)]
    return result


def snapshot(record):
    return deepcopy({key: value for key, value in record.items() if key != 'definition_history'})


def persist_version(store, connection, record):
    version_id = record['id'] + ':' + str(record['version'])
    if store.get(connection, 'field_definition_version', version_id):
        raise HTTPException(409, 'Versão da definição já registrada; nenhuma versão será sobrescrita')
    store.put(connection, 'field_definition_version', {
        'id': version_id, 'field_id': record['id'], 'definition': snapshot(record),
    })


def create_definition(store, connection, body: FieldDefinitionInput, actor_id):
    data = body.model_dump()
    data['id'] = data['id'] or uid()
    if store.get(connection, 'field', data['id']):
        raise HTTPException(409, 'Identificador de campo já cadastrado')
    at = now()
    record = public_definition({**data, 'version': 1, 'active': True,
                               'search_state': 'pending', 'created_at': at, 'updated_at': at,
                               'created_by': actor_id, 'updated_by': actor_id})
    persist_version(store, connection, record)
    store.put(connection, 'field', record)
    return record


def update_definition(store, connection, field_id, body, if_match, actor_id):
    existing = store.get(connection, 'field', field_id)
    if not existing:
        raise HTTPException(404, 'Campo não encontrado')
    record = public_definition(existing)
    if if_match is None:
        raise HTTPException(428, 'If-Match obrigatório com a versão da definição')
    if not re.fullmatch(r'(?:[1-9][0-9]*|"[1-9][0-9]*")', if_match):
        raise HTTPException(422, 'If-Match deve informar uma versão inteira')
    if int(if_match.strip('"')) != record['version']:
        raise HTTPException(409, 'Definição alterada por outra operação; recarregue antes de editar')
    if not body or set(body) - (IMMUTABLE | {'name', 'active'}):
        raise HTTPException(422, 'Informe nome e/ou estado; propriedades desconhecidas não são aceitas')
    incompatible = [key for key in IMMUTABLE if key in body and fingerprint(body[key]) != fingerprint(record.get(key))]
    if incompatible:
        raise HTTPException(409, {
            'code': 'field_definition_migration_required', 'fields': sorted(incompatible),
            'message': 'Mudança incompatível: crie uma nova definição e uma migração explícita, preservando os valores e versões anteriores.',
        })
    if 'name' in body:
        if not isinstance(body['name'], str) or not body['name'].strip() or len(body['name']) > 160:
            raise HTTPException(422, 'Nome deve ser um texto não vazio de até 160 caracteres')
        body = {**body, 'name': body['name'].strip()}
    if 'active' in body and type(body['active']) is not bool:
        raise HTTPException(422, 'active deve ser true ou false, sem conversão de tipo')
    changes = {key: body[key] for key in ('name', 'active') if key in body and body[key] != record[key]}
    if not changes:
        return record
    # Older local definitions receive an immutable copy before the first edit.
    if not store.get(connection, 'field_definition_version', field_id + ':' + str(record['version'])):
        persist_version(store, connection, record)
    record.update(changes)
    record.update(version=record['version'] + 1, updated_at=now(), updated_by=actor_id)
    record['definition_history'].append(snapshot(record))
    persist_version(store, connection, record)
    store.put(connection, 'field', record)
    return record


def validate_custom(store, connection, value, entity_type):
    """Validate without coercion; return metadata to bind evidence to its schema."""
    field_id = value.get('field_id')
    if not isinstance(field_id, str) or not field_id.strip() or len(field_id) > 160:
        raise HTTPException(422, 'Campo adicional exige field_id textual não vazio, até 160 caracteres')
    if 'value' not in value:
        raise HTTPException(422, 'Campo adicional exige value; null deve ser enviado explicitamente')
    found = store.get(connection, 'field', field_id)
    if not found:
        return {'field_id': field_id, 'field_definition_version': None,
                'classification_state': 'pending', 'value_policy': VALUE_POLICY,
                'warning': 'Campo sem definição cadastrada; valor original preservado para classificação.'}
    definition = public_definition(found)
    if not definition['active']:
        raise HTTPException(422, 'Campo desativado: valores anteriores permanecem consultáveis; reative a definição para agregar novos valores')
    if definition['scope'] not in {'both', entity_type}:
        raise HTTPException(422, 'Escopo do campo incompatível com o tipo de cadastro')
    raw = value['value']
    if raw is not None:
        kind = definition['type']
        good = False
        if kind == 'text':
            good = type(raw) is str
        elif kind == 'integer':
            good = type(raw) is int
        elif kind == 'decimal':
            good = type(raw) in (int, float) and (type(raw) is int or math.isfinite(raw))
            if type(raw) is str and re.fullmatch(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?', raw):
                try:
                    good = Decimal(raw).is_finite()
                except InvalidOperation:
                    good = False
        elif kind == 'boolean':
            good = type(raw) is bool
        elif kind == 'date':
            if type(raw) is str and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', raw):
                try:
                    date.fromisoformat(raw)
                    good = True
                except ValueError:
                    pass
        elif kind == 'enum':
            good = type(raw) is str and raw in definition['options']
        elif kind == 'url':
            if type(raw) is str and not any(ch.isspace() or ord(ch) < 32 for ch in raw):
                try:
                    parsed = urlsplit(raw)
                    good = parsed.scheme in {'http', 'https'} and bool(parsed.hostname) and not parsed.username and not parsed.password
                    # Force invalid ports to fail validation without requesting the URL.
                    parsed.port
                except ValueError:
                    good = False
        elif kind == 'reference':
            if type(raw) is dict and set(raw) == {'entity_type', 'id'} and type(raw['entity_type']) is str and raw['entity_type'] in {'person', 'company'} and isinstance(raw['id'], str):
                target = store.get(connection, 'entity', raw['id'])
                good = target is not None and target['entity_type'] == raw['entity_type']
        if not good:
            raise HTTPException(422, {
                'code': 'custom_field_invalid_value', 'field_id': field_id,
                'field_definition_version': definition['version'],
                'message': 'Valor incompatível com o tipo ' + kind + '; cada item recebe um valor sem conversão automática. Envie null explicitamente se desconhecido.',
            })
    return {'field_id': field_id, 'field_definition_version': definition['version'],
            'classification_state': 'defined', 'value_policy': VALUE_POLICY}


def bind_definition(item, observations, metadata):
    """Do not rewrite earlier observations or their original definition versions."""
    item.update({key: value for key, value in metadata.items() if key != 'warning'})
    if metadata.get('warning') and metadata['warning'] not in item['notes']:
        item['notes'].append(metadata['warning'])
    for observation in observations:
        observation.update({key: metadata[key] for key in ('field_id', 'field_definition_version', 'classification_state')})
        state = item['fields'].get(observation['path'])
        if state and state['id'] == observation['id']:
            state.update({key: metadata[key] for key in ('field_id', 'field_definition_version', 'classification_state')})

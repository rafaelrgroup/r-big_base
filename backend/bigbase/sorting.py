"""Versioned ordering for the single-process local adapter, shared by search/XLSX.

This scans materialized records; it is not a production index or cursor.
"""
from datetime import date
import re
import unicodedata

from fastapi import HTTPException

from .domain import timestamp


SORT_FIELDS = {
    'name': ('Nome', 'identity'),
    'updated_at': ('Atualização', None),
    'id': ('ID', None),
    'birth_date': ('Nascimento', 'identity'),
    'city': ('Cidade', 'address'),
    'state': ('UF / estado', 'address'),
    'postal_code': ('CEP / código postal', 'address'),
}


def sorting_catalog():
    return {
        'version': 1, 'max_criteria': 5, 'backend': 'development-adapter',
        'fields': [{'field': field, 'label': label, 'multiple': kind is not None,
                    'status': 'ready_local'} for field, (label, kind) in SORT_FIELDS.items()],
        'missing': 'last', 'default_modes': {'asc': 'min', 'desc': 'max'},
        'final_tiebreaker': {'field': 'id', 'direction': 'asc'},
        'text_comparison': 'unicode_nfc_casefold',
    }


def resolve_sort(body, *, default_legacy=True):
    """Validate input and return a JSON-serializable, frozen execution contract."""
    if type(body.get('include_invalid', False)) is not bool:
        raise HTTPException(422, 'include_invalid deve ser booleano')
    if 'sorts' not in body:
        if not default_legacy and not {'sort', 'direction'} & body.keys():
            return {'version': 1, 'contract': 'snapshot_order', 'criteria': []}
        field, direction = body.get('sort', 'name'), body.get('direction', 'asc')
        if not isinstance(field, str) or field not in {'name', 'updated_at', 'id'} or direction not in ('asc', 'desc'):
            raise HTTPException(422, 'Ordenação legada inválida')
        criteria = [{'field': field, 'direction': direction}]
        if field != 'id':
            criteria.append({'field': 'id', 'direction': direction})
        return {'version': 1, 'contract': 'legacy', 'criteria': criteria,
                'text_comparison': 'legacy_str_casefold', 'missing': 'legacy'}
    if {'sort', 'direction'} & body.keys():
        raise HTTPException(422, 'Não misture sorts com sort/direction legados')
    sorts = body['sorts']
    if not isinstance(sorts, list) or not 1 <= len(sorts) <= 5:
        raise HTTPException(422, 'sorts exige de 1 a 5 critérios')
    criteria, seen = [], set()
    for index, criterion in enumerate(sorts):
        if not isinstance(criterion, dict) or set(criterion) - {'field', 'direction', 'mode'} or not {'field', 'direction'} <= criterion.keys():
            raise HTTPException(422, 'Critério exige field, direction e mode opcional')
        field, direction = criterion['field'], criterion['direction']
        if not isinstance(field, str) or field not in SORT_FIELDS:
            raise HTTPException(422, 'Campo de ordenação não disponível ou ainda não preparado no adaptador local')
        if field in seen:
            raise HTTPException(422, 'Campo de ordenação repetido')
        if field == 'id' and index != len(sorts) - 1:
            raise HTTPException(422, 'ID explícito deve ser o último critério')
        if direction not in ('asc', 'desc'):
            raise HTTPException(422, 'Direção deve ser asc ou desc')
        seen.add(field)
        normalized = {'field': field, 'direction': direction}
        if SORT_FIELDS[field][1] is not None:
            mode = criterion.get('mode', 'min' if direction == 'asc' else 'max')
            if mode not in ('min', 'max'):
                raise HTTPException(422, 'Modo deve ser min ou max')
            normalized['mode'] = mode
        elif 'mode' in criterion:
            raise HTTPException(422, 'mode só é aceito para campos múltiplos')
        criteria.append(normalized)
    if 'id' not in seen:
        criteria.append({'field': 'id', 'direction': 'asc'})
    return {'version': 1, 'contract': 'multi', 'criteria': criteria,
            'text_comparison': 'unicode_nfc_casefold', 'missing': 'last'}


def _value(value, field):
    if not isinstance(value, str):
        return None
    if field == 'birth_date':
        try:
            return date.fromisoformat(value) if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value) else None
        except ValueError:
            return None
    if field == 'updated_at':
        try:
            return timestamp(value)
        except (ValueError, OverflowError):
            return None
    # Codes remain text; canonical Unicode equivalents compare equally.
    return unicodedata.normalize('NFC', unicodedata.normalize('NFC', value).casefold())


def _key(entity, criterion, include_invalid):
    field = criterion['field']
    kind = SORT_FIELDS[field][1]
    if kind is None:
        return _value(entity.get(field), field)
    values = [_value(item.get('value', {}).get(field), field)
              for item in entity.get('items', []) if item['kind'] == kind
              and (include_invalid or item.get('flags', {}).get('valid') is not False)]
    values = [value for value in values if value is not None]
    return (min(values) if criterion['mode'] == 'min' else max(values)) if values else None


def sort_entities(entities, plan, *, include_invalid=False):
    result = list(entities)
    if plan['contract'] == 'snapshot_order':
        return result
    if plan['contract'] == 'legacy':
        first = plan['criteria'][0]
        return sorted(result, key=lambda e: (str(e.get(first['field'], '')).casefold(), e['id']),
                      reverse=first['direction'] == 'desc')
    # Stable passes preserve lower priorities, including missing values in either direction.
    for criterion in reversed(plan['criteria']):
        keyed = [(entity, _key(entity, criterion, include_invalid)) for entity in result]
        present = [(entity, key) for entity, key in keyed if key is not None]
        present.sort(key=lambda pair: pair[1], reverse=criterion['direction'] == 'desc')
        result = [entity for entity, _ in present] + [entity for entity, key in keyed if key is None]
    return result

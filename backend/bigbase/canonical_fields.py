"""Explicit catalog binding for synthetic canonical enrichment, never a live schema."""
from copy import deepcopy
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException

from .canonical_store import CanonicalError, decode, digest
from .catalogs import public_definition, snapshot, validate_custom, VALUE_POLICY
from .canonical_catalog import PG_FIELD_CONTRACT, load_definitions

FIELD_CONTRACT = 'canonical-custom-field-2026-09-09.1'


def prepare_item_field(item, facts):
    contract = item['custom_field']
    if (item['kind'] != 'custom' or not isinstance(contract, dict)
            or set(contract) != {'contract', 'field_id', 'version'}
            or contract['contract'] not in {FIELD_CONTRACT, PG_FIELD_CONTRACT}
            or not isinstance(contract['field_id'], str) or not contract['field_id'].strip()
            or len(contract['field_id']) > 160
            or (contract['version'] is not None and
                (type(contract['version']) is not int or not 1 <= contract['version'] < 2**63))
            or len(item['fields']) != 1 or item['fields'][0]['path'] != 'value'):
        raise CanonicalError('Campo adicional exige contrato, field_id, versão e um campo value por item.')
    for fact in facts:
        fact['custom_field'] = deepcopy(contract)


def bind_fields(record, body, definitions, connection):
    """Runs after durable replay lookup; references use this PostgreSQL transaction.

    Legacy definitions use the local authorization snapshot. PostgreSQL contracts
    resolve and lock their own catalog in the observation transaction, including
    unknown IDs. Every observation retains the definition and its hash.
    """
    result = deepcopy(record)
    pg_ids = {item['custom_field']['field_id'] for item in body['items']
              if item.get('custom_field', {}).get('contract') == PG_FIELD_CONTRACT}
    pg_definitions = load_definitions(connection, pg_ids) if pg_ids else {}
    deployment = str(connection.execute('SELECT deployment_id FROM field_catalog_meta WHERE singleton').fetchone()['deployment_id']) if pg_ids else None

    class CatalogView:
        def get(self, _, kind, key):
            if kind == 'field':
                return selected_definitions.get(key)
            if kind == 'entity':
                try:
                    owner = UUID(key)
                except (ValueError, TypeError):
                    return None
                return connection.execute('SELECT entity_type FROM entities WHERE owner_id=%s', (owner,)).fetchone()
            return None

    for item in body['items']:
        contract = item.get('custom_field')
        if contract is None:
            continue
        field_id, version = contract['field_id'], contract['version']
        is_pg = contract['contract'] == PG_FIELD_CONTRACT
        selected_definitions = pg_definitions if is_pg else definitions
        found = selected_definitions.get(field_id)
        definition = public_definition(found) if found else None
        if (definition is None and version is not None) or (definition is not None and version != definition['version']):
            raise CanonicalError('Versão da definição divergente; recarregue o catálogo. Campo desconhecido exige versão null.')
        raw = item['fields'][0]['value']
        # The shared local validator accepts exact decimal text; only its probe
        # uses text. The canonical observation keeps the received numeric type.
        probe = str(raw) if isinstance(raw, Decimal) and definition and definition['type'] == 'decimal' else raw
        try:
            metadata = validate_custom(CatalogView(), None, {'field_id': field_id, 'value': probe}, record['entity_type'])
        except HTTPException as exc:
            detail = exc.detail
            raise CanonicalError(detail.get('message', 'Valor incompatível com a definição.') if isinstance(detail, dict) else detail) from None
        saved = snapshot(definition) if definition else None
        metadata.update(custom_field=deepcopy(contract), field_definition=saved,
                        field_definition_sha256=digest(saved) if saved else None,
                        catalog_environment='postgresql_synthetic' if is_pg else 'local_synthetic', canonical_search_state='pending',
                        value_policy=VALUE_POLICY,
                        field_input_dates={k: item['fields'][0][k] for k in ('observed_at', 'source_updated_at') if k in item['fields'][0]})
        if is_pg:
            metadata['catalog_deployment_id'] = deployment
        for fact in result['facts']:
            if fact['target_kind'] == item['kind'] and fact['item_key'] == item['key']:
                fact.update(deepcopy(metadata))
                for flag in fact['flags'].values():
                    flag.update(deepcopy(metadata))
    return result


def guard_custom_field(connection, owner, item_id, fact):
    """Binding applies even to late/pending evidence and cannot be bypassed by PATCH."""
    # The first value pins the item; every later write checks it under the owner lock.
    rows = connection.execute("SELECT metadata_json FROM observations WHERE owner_id=%s AND item_id=%s AND dimension='value' ORDER BY entity_version,operation_sequence LIMIT 1", (owner, item_id)).fetchall()
    incoming = decode(fact['metadata_json']).get('custom_field')
    for row in rows:
        prior = decode(row['metadata_json']).get('custom_field')
        if prior is None and incoming is None:
            continue
        if (prior is None or incoming is None or prior['field_id'] != incoming['field_id']
                or prior['contract'] != incoming['contract']):
            raise CanonicalError('Item vinculado a campo adicional: use o contrato e a mesma definição; para reclassificar, use outra referência.')

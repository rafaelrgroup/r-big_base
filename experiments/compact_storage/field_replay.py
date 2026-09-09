"""Field-state integration for bounded, already owned operation streams.

This is an equivalence harness, not an identity registry or a durable repository.
It uses the application's resolver after block decoding. Production adoption
still requires catalogue/identity guards, atomic durable pointers, history
pagination and independent capacity measurement. Never log the returned rows.
"""
from copy import deepcopy
from uuid import UUID

from bigbase.canonical_store import (
    CanonicalError, IdempotencyConflict, IdentityConflict, VersionConflict,
    _date, _prepare, digest, identifier, json_text, resolve_canonical_observation,
)


class FieldReplay:
    def __init__(self, owner_id, entity_type):
        if entity_type not in {'person', 'company'}:
            raise CanonicalError('Invalid entity type')
        self.owner = UUID(str(owner_id))
        self.entity_type = entity_type
        self.version = 0
        self.fields = {}
        self.items = {}
        self.operations = {}

    def apply(self, record, *, actor, received_at, entity_version):
        prepared = _prepare(record)
        if prepared['entity_type'] != self.entity_type:
            raise IdentityConflict('Entity type changed')
        if not isinstance(actor, str) or not actor:
            raise CanonicalError('Actor is required')
        received = _date(received_at)
        if received is None:
            raise CanonicalError('Committed reception time is required')
        operation = prepared['operation_id']
        previous = self.operations.get(operation)
        if previous:
            if previous['prepared_hash'] != prepared['prepared_hash']:
                raise IdempotencyConflict('Operation content changed')
            return {'replayed': True, 'observations': [], 'version': previous['version']}
        if type(entity_version) is not int or entity_version != self.version + 1:
            raise VersionConflict('Committed operation sequence has a gap')

        # Work on a private projection so an error never leaves half an operation.
        fields = self.fields.copy()
        items = deepcopy(self.items)
        rows, touched = [], set()
        for fact in prepared['facts']:
            raw = fact['raw']
            item_id = identifier('item', [str(self.owner), fact['kind'], raw['item_key']])
            item = items.setdefault(item_id, {
                'kind': fact['kind'], 'item_key_json': json_text(raw['item_key']),
                'version': 0, 'created_at': received, 'updated_at': received,
            })
            if item['kind'] != fact['kind'] or item['item_key_json'] != json_text(raw['item_key']):
                raise IdentityConflict('Item hash collision')
            path_hash = digest(raw['target_path'])
            for flag_name, flag in [(None, None), *raw.get('flags', {}).items()]:
                dimension = 'value' if flag is None else 'flag:' + flag_name
                key = (item_id, path_hash, dimension)
                observation, state = resolve_canonical_observation(
                    owner=self.owner, item_id=item_id, operation_id=operation,
                    source=prepared['source'], actor=actor, received=received,
                    fact=fact, previous=fields.get(key),
                    value_state=fields.get((item_id, path_hash, 'value')),
                    flag_name=flag_name, flag=flag, entity_version=entity_version,
                    operation_sequence=len(rows) + 1, item_version=item['version'] + 1,
                )
                rows.append(observation)
                if state is not None:
                    fields[key] = state
            touched.add(item_id)
        for item_id in touched:
            items[item_id]['version'] += 1
            items[item_id]['updated_at'] = received
        self.fields, self.items, self.version = fields, items, entity_version
        self.operations[operation] = {
            'prepared_hash': prepared['prepared_hash'], 'version': entity_version,
            'actor': actor, 'received_at': received,
        }
        return {'replayed': False, 'observations': rows, 'version': entity_version}

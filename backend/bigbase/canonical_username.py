"""Explicit platform formatting; account identifiers remain literal."""
from .canonical_store import CanonicalError, decode, digest, json_text
from .domain import NORMALIZER_VERSION, normalize


USERNAME_CONTRACT = 'canonical-username-2026-09-09.1'


def normalize_item_username(item, facts):
    if item['kind'] != 'username' or item['username_normalization'] != {'contract': USERNAME_CONTRACT}:
        raise CanonicalError('Username normalization requires a username item and the supported explicit contract')
    fields = {field['path']: field for field in item['fields']}
    if not {'username', 'platform'} <= fields.keys():
        raise CanonicalError('Username normalization requires explicit username and platform fields')
    for name in ('username', 'platform'):
        value = fields[name]['value']
        if not isinstance(value, str) or len(value) > 2048 or (name == 'platform' and not value.strip()):
            raise CanonicalError('Username and platform must be bounded text; platform must be nonempty')
    result, notes = normalize('username', {name: fields[name]['value'] for name in ('username', 'platform')})
    context = {name: {key: fields[name][key] for key in ('value', 'observed_at', 'source_updated_at') if key in fields[name]}
               for name in ('username', 'platform')}
    for fact in facts:
        name = fact['target_path']
        if name not in ('username', 'platform'):
            continue
        fact.update(normalized_value=result[name], normalization=NORMALIZER_VERSION,
                    status='normalized' if name == 'platform' else 'unknown',
                    username_contract=USERNAME_CONTRACT, context_fields=context,
                    username_input_dates={key: fields[name][key] for key in ('observed_at', 'source_updated_at') if key in fields[name]},
                    username_normalization={'rule_id': 'platform-casefold' if name == 'platform' else 'username-preserve',
                                            'decision': 'platform_normalized' if name == 'platform' else 'literal_no_rule',
                                            'platform': result['platform'], 'syntax_valid': None},
                    normalization_notes=notes)


def guard_username_platform(c, owner, item_id, fact):
    """Pin opted-in items to their platform, including future/late observations.

    A different platform requires a different item reference. Literal writes and
    scalar PATCH cannot move existing account confirmations across platforms.
    """
    if fact['raw']['target_path'] != 'platform':
        return
    prior = c.execute("""SELECT normalized_json,metadata_json FROM observations
        WHERE owner_id=%s AND item_id=%s AND target_path_hash=%s AND dimension='value'
        AND metadata_json LIKE %s ORDER BY entity_version,operation_sequence LIMIT 1""",
        (owner, item_id, digest('platform'), '%'+json_text({'username_contract': USERNAME_CONTRACT})[1:-1]+'%')).fetchone()
    metadata = decode(fact['metadata_json'])
    incoming = decode(fact['normalized_json'])
    if prior and decode(prior['metadata_json']).get('username_contract') == USERNAME_CONTRACT:
        if incoming != decode(prior['normalized_json']):
            raise CanonicalError('A different username platform requires a distinct item reference')
    elif metadata.get('username_contract') == USERNAME_CONTRACT:
        # Never attach the opt-in context to a legacy item with conflicting
        # platform history (even when that earlier event did not become current).
        rows = c.execute("""SELECT normalized_json FROM observations WHERE owner_id=%s AND item_id=%s
            AND target_path_hash=%s AND dimension='value'""", (owner, item_id, digest('platform')))
        for row in rows:
            previous = decode(row['normalized_json'])
            if not isinstance(previous, str) or previous.casefold() != incoming:
                raise CanonicalError('Conflicting legacy platform requires a distinct item reference')

"""Opt-in postal formatting with explicit country and field-local provenance."""
import re

from .canonical_store import CanonicalError
from .domain import NORMALIZER_VERSION, normalize


POSTAL_CONTRACT = 'canonical-postal-2026-09-09.1'


def normalize_item_postal(item, facts):
    if item['kind'] != 'address' or item['postal_normalization'] != {'contract': POSTAL_CONTRACT}:
        raise CanonicalError('Postal normalization requires an address and the supported explicit contract')
    fields = {field['path']: field for field in item['fields']}
    if 'postal_code' not in fields:
        raise CanonicalError('Postal normalization requires an explicit postal_code in this operation')
    value = fields['postal_code']['value']
    if not isinstance(value, str) or len(value) > 2048:
        raise CanonicalError('Normalized postal code must be bounded text')
    country = fields['country']['value'] if 'country' in fields else None
    if 'country' in fields and (not isinstance(country, str) or not re.fullmatch('[A-Z]{2}', country)):
        raise CanonicalError('Postal country must be an explicit uppercase two-letter code')
    # Never let the legacy normalizer's default BR guess a missing country.
    candidate, notes = normalize('address', {'postal_code': value, 'country': country})
    candidate = candidate['postal_code']
    valid = bool(re.fullmatch('[0-9]{8}', candidate)) if country == 'BR' else None
    fact = next(fact for fact in facts if fact['target_path'] == 'postal_code')
    fact.update(normalized_value=candidate if valid else value, normalization=NORMALIZER_VERSION,
                status='normalized' if valid else 'pending', postal_contract=POSTAL_CONTRACT,
                postal_input_dates={name: fields['postal_code'][name] for name in ('observed_at', 'source_updated_at')
                                    if name in fields['postal_code']},
                context_fields={name: {key: field[key] for key in ('value', 'observed_at', 'source_updated_at') if key in field}
                                for name, field in fields.items() if name == 'country'},
                postal_normalization={
                    'rule_id': 'br-postal-format' if country == 'BR' else 'postal-preserve',
                    'decision': 'format_normalized' if valid else 'review',
                    'country': country, 'syntax_valid': valid, 'candidate_value': candidate,
                }, normalization_notes=notes)
    # No observations for absent street/city/country or derived syntax. Supplied
    # components retain independent dates and flags; formatting confirms nothing.

"""Explicit phone transformation for HTTP enrichment, preserving field boundaries."""
from .canonical_store import CanonicalError
from .domain import NORMALIZER_VERSION, normalize


PHONE_CONTRACT = 'canonical-phone-2026-09-09.1'
COMPONENTS = ('number', 'country', 'ddd', 'extension')


def normalize_item_phone(item, facts):
    if item['kind'] != 'phone' or item['phone_normalization'] != {'contract': PHONE_CONTRACT}:
        raise CanonicalError('Phone normalization requires a phone item and the supported explicit contract')
    fields = {field['path']: field for field in item['fields']}
    if 'number' not in fields:
        raise CanonicalError('Phone normalization requires an explicit number in this operation')
    components = {name: fields[name]['value'] for name in COMPONENTS if name in fields}
    # The literal contract still accepts null/false/zero. Opting into the phone
    # contract never coerces nontext input into a telephone or country code.
    if any(not isinstance(value, str) or len(value) > 2048 for value in components.values()):
        raise CanonicalError('Normalized phone components must be bounded text')
    result, notes = normalize('phone', components)
    number = next(fact for fact in facts if fact['target_path'] == 'number')
    number.update(normalized_value=result['number'], normalization=NORMALIZER_VERSION,
                  status='pending' if result['phone_normalization']['decision'] == 'review' else 'normalized',
                  phone_contract=PHONE_CONTRACT,
                  phone_normalization=result['phone_normalization'],
                  phone_output={name: result[name] for name in (*COMPONENTS, 'canonical_number',
                      'classification', 'syntax_valid', 'country_calling_code', 'national_number') if name in result},
                  normalization_notes=notes,
                  context_fields={name: {key: field[key] for key in ('value', 'observed_at', 'source_updated_at') if key in field}
                                  for name, field in fields.items() if name in COMPONENTS and name != 'number'})
    # No extra observations for inferred DDD/country/extension/classification;
    # derived output belongs to this number's audit. Supplied component facts
    # keep their own literal values, timestamps, flags and provenance. Existing
    # confirmation binding uses input_value unless the caller binds explicitly.

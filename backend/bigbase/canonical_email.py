"""Opt-in domain normalization; never infer delivery or ownership from syntax."""
from .canonical_store import CanonicalError
from .domain import NORMALIZER_VERSION, normalize


EMAIL_CONTRACT = 'canonical-email-2026-09-09.1'


def normalize_item_email(item, facts):
    if item['kind'] != 'email' or item['email_normalization'] != {'contract': EMAIL_CONTRACT}:
        raise CanonicalError('Email normalization requires an email item and the supported explicit contract')
    fields = {field['path']: field for field in item['fields']}
    if 'email' not in fields:
        raise CanonicalError('Email normalization requires an explicit email in this operation')
    address = fields['email']['value']
    if not isinstance(address, str) or len(address) > 2048:
        raise CanonicalError('Normalized email must be bounded text')
    result, notes = normalize('email', {'email': address})
    local, separator, domain = address.rpartition('@')
    fact = next(fact for fact in facts if fact['target_path'] == 'email')
    fact.update(normalized_value=result['email'], normalization=NORMALIZER_VERSION,
                status='normalized' if result['syntax_valid'] else 'pending',
                email_contract=EMAIL_CONTRACT,
                email_input_dates={name: fields['email'][name] for name in ('observed_at', 'source_updated_at')
                                   if name in fields['email']},
                email_normalization={
                    'rule_id': 'email-domain-lowercase',
                    'decision': 'domain_normalized' if result['syntax_valid'] else 'review',
                    'input_local_part': local if separator else None,
                    'input_domain': domain if separator else None,
                    'output_domain': domain.lower() if separator else None,
                    'syntax_valid': result['syntax_valid'],
                }, normalization_notes=notes)
    # Derived components stay in this observation's audit. Other supplied fields
    # retain their own dates/provenance, and absent fields create no observations.
    # Flags use the existing exact-value binding, including after transformation.

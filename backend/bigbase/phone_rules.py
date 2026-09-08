"""Conservative, auditable phone normalization; never evidence of an active line."""
from copy import deepcopy
import re

import phonenumbers

PHONE_RULES_VERSION = 'br-anatel-2026-09-08.1'
ANATEL_LEGACY = 'https://informacoes.anatel.gov.br/legislacao/component/content/article/17-resolucoes/2002/90-resolucao-301'
ANATEL_TRANSITION = 'https://informacoes.anatel.gov.br/legislacao/resolucoes/25-2010/16-resolucao-553'
ANATEL_SCHEDULE = 'https://www.gov.br/anatel/pt-br/regulado/perguntas-frequentes'
ANATEL_CURRENT = 'https://www.gov.br/anatel/pt-br/regulado/numeracao/perguntas-frequentes'

# Actual rollout dates, not source-observation dates. The original art. 19 of
# Resolution 301/2002 designates 8/9 for SMP; 6/7 are deliberately not generalized.
ROLLOUT_GROUPS = (
    ('2012-07-29', ('11',)),
    ('2013-08-25', ('12', '13', '14', '15', '16', '17', '18', '19')),
    ('2013-10-27', ('21', '22', '24', '27', '28')),
    ('2014-11-02', ('91', '92', '93', '94', '95', '96', '97', '98', '99')),
    ('2015-05-31', ('81', '82', '83', '84', '85', '86', '87', '88', '89')),
    ('2015-10-11', ('31', '32', '33', '34', '35', '37', '38', '71', '73', '74', '75', '77', '79')),
    ('2016-05-29', ('61', '62', '63', '64', '65', '66', '67', '68', '69')),
    ('2016-11-06', ('41', '42', '43', '44', '45', '46', '47', '48', '49', '51', '53', '54', '55')),
)
ROLLOUT_BY_DDD = {ddd: at for at, areas in ROLLOUT_GROUPS for ddd in areas}


def original_phone_components(normalized):
    """Recover the received components for review; does not mutate stored data."""
    return deepcopy(normalized['phone_normalization']['input_components'])


def normalize_phone(value):
    result = deepcopy(value)
    notes = []
    audit = {
        'version': PHONE_RULES_VERSION, 'library_version': phonenumbers.__version__,
        'input_components': {key: deepcopy(value[key]) for key in ('number', 'country', 'ddd', 'extension') if key in value},
        'decision': 'review', 'reason': None, 'rule_id': None, 'sources': [],
        'output_number': value.get('number'), 'previous_number': None,
        'changed_digits': False, 'country_context': None, 'candidates': [],
    }
    result.update(classification='unknown', syntax_valid=False, canonical_number=None)
    result.setdefault('usage', 'unknown')
    result['phone_normalization'] = audit

    def review(reason, text, candidates=()):
        audit.update(reason=reason, candidates=list(candidates))
        notes.append(text)
        return result, notes

    raw = value.get('number')
    if not isinstance(raw, str) or not raw.strip():
        return review('non_text_or_empty_number', 'Número vazio ou não textual; entrada preservada sem conversão automática.')
    number = raw.strip()
    if number.lower().startswith('tel:'):
        number = number[4:]
    extension_match = re.search(r'(?:\s*(?:ext\.?|ramal|x|#)\s*|;ext=)([0-9]{1,10})$', number, re.IGNORECASE)
    inline_extension = extension_match.group(1) if extension_match else None
    if extension_match:
        number = number[:extension_match.start()].strip()
    extension = value.get('extension')
    if extension not in (None, '') and (type(extension) is not str or not re.fullmatch(r'[0-9]{1,10}', extension)):
        return review('invalid_extension', 'Ramal não textual ou não interpretado; número e ramal originais preservados.')
    if inline_extension and extension not in (None, '', inline_extension):
        audit['extension_candidates'] = [extension, inline_extension]
        return review('conflicting_extensions', 'Ramal no número diverge do campo de ramal; nenhuma alternativa foi sobrescrita.')
    extension = inline_extension or extension
    if re.search(r'[A-Za-z]', number) or not re.fullmatch(r'\+?[0-9\s().-]+', number):
        return review('unsupported_characters', 'Número alfabético ou formato não reconhecido; letras e caracteres preservados para revisão.')
    compact = re.sub(r'[\s().-]', '', number)
    supplied_region = value.get('country')
    explicit_region = isinstance(supplied_region, str) and bool(supplied_region.strip())
    region = supplied_region.strip().upper() if explicit_region else 'BR'
    if supplied_region is not None and type(supplied_region) is not str:
        return review('invalid_country', 'País não textual; entrada preservada para revisão.')
    if region not in phonenumbers.SUPPORTED_REGIONS:
        return review('unknown_country', 'País não reconhecido; não foi presumido outro país.')
    audit['country_context'] = 'explicit_country' if explicit_region else 'international_prefix' if compact.startswith('+') else 'application_default_BR'
    explicit_brazil = (explicit_region and region == 'BR') or (not explicit_region and compact.startswith('+55'))
    supplied_ddd = value.get('ddd')
    if supplied_ddd is not None and (type(supplied_ddd) is not str or supplied_ddd not in ROLLOUT_BY_DDD):
        return review('invalid_ddd', 'DDD informado não está no catálogo brasileiro auditado; entrada preservada.')
    if supplied_ddd and not compact.startswith('+') and len(compact) in (8, 9):
        if not explicit_brazil:
            return review('country_required_for_ddd', 'País Brasil precisa ser explícito para combinar número local e DDD.')
        compact = '+55' + supplied_ddd + compact
    try:
        parsed = phonenumbers.parse(compact, region)
    except phonenumbers.NumberParseException:
        return review('unparseable', 'Número não interpretado; entrada preservada.')
    if explicit_region and parsed.country_code != phonenumbers.country_code_for_region(region):
        return review('country_conflict', 'Código internacional diverge do país informado; entrada preservada para revisão.')
    if explicit_region and phonenumbers.is_valid_number(parsed) and not phonenumbers.is_valid_number_for_region(parsed, region):
        return review('country_conflict', 'Região do número diverge do país informado, apesar do código internacional compartilhado; entrada preservada.')
    national = phonenumbers.national_significant_number(parsed)
    brazil = parsed.country_code == 55
    ddd = national[:2] if brazil and len(national) in (10, 11) else None
    local = national[2:] if ddd else None
    if supplied_ddd and supplied_ddd != ddd:
        return review('ddd_conflict', 'DDD no número diverge do DDD informado; entrada preservada para revisão.')
    if brazil and (ddd is None or ddd not in ROLLOUT_BY_DDD) and not phonenumbers.is_valid_number(parsed):
        return review('missing_or_invalid_ddd', 'Número brasileiro sem DDD válido ou comprimento não reconhecido; nenhum dígito adicionado.')

    def resolved(phone, classification, rule_id, sources=(), changed=False):
        canonical = phonenumbers.format_number(phone, phonenumbers.PhoneNumberFormat.E164)
        result.update(number=canonical, canonical_number=canonical, classification=classification, syntax_valid=True,
                      country_calling_code=str(phone.country_code), national_number=phonenumbers.national_significant_number(phone))
        if explicit_region:
            result['country'] = region
        else:
            inferred_region = phonenumbers.region_code_for_number(phone)
            if inferred_region:
                result['country'] = inferred_region
        if brazil and ddd in ROLLOUT_BY_DDD:
            result['ddd'] = ddd
        if extension is not None:
            result['extension'] = extension
        audit.update(decision='historical_conversion' if changed else 'canonical', reason=None,
                     rule_id=rule_id, sources=list(sources), output_number=canonical, changed_digits=changed)
        return result, notes

    # SME/radio must not inherit libphonenumber's broad Brazilian MOBILE category.
    if brazil and ddd in ROLLOUT_BY_DDD and len(local) == 8 and local.startswith('7'):
        if phonenumbers.is_valid_number(parsed):
            notes.append('Prefixo 7: serviço especializado/rádio; não recebe nono dígito e não comprova celular pessoal.')
            return resolved(parsed, 'other', 'br_sme_7', [ANATEL_CURRENT])
        return review('specialized_or_legacy_radio', 'Prefixo 7 de serviço especializado/rádio; nenhum nono dígito foi acrescentado.')
    if phonenumbers.is_valid_number(parsed):
        typ = phonenumbers.number_type(parsed)
        classification = ('mobile' if typ == phonenumbers.PhoneNumberType.MOBILE else
                          'fixed' if typ == phonenumbers.PhoneNumberType.FIXED_LINE else
                          'unknown' if typ == phonenumbers.PhoneNumberType.FIXED_LINE_OR_MOBILE else 'other')
        return resolved(parsed, classification, 'libphonenumber_current')

    if brazil and ddd in ROLLOUT_BY_DDD and len(local) == 8 and local[0] in '689':
        previous = '+55' + ddd + local
        candidate_number = '+55' + ddd + '9' + local
        candidate = phonenumbers.parse(candidate_number, 'BR')
        candidate_valid = phonenumbers.is_valid_number(candidate) and phonenumbers.number_type(candidate) == phonenumbers.PhoneNumberType.MOBILE
        alternatives = [{'number': candidate_number, 'requires_review': True, 'reason': 'possible_legacy_mobile'}] if candidate_valid else []
        if not explicit_brazil:
            return review('country_required_for_historical_rule', 'Possível celular legado; confirme país Brasil antes de acrescentar um dígito.', alternatives)
        if local[0] == '6':
            return review('historical_prefix_not_proven', 'Prefixo 6 não consta na regra histórica aprovada; alternativa preservada para revisão, sem alterar o número.', alternatives)
        if local[1:3] == '00':
            return review('reserved_historical_range', 'Faixa reservada no plano histórico; não é evidência suficiente de celular legado.')
        if candidate_valid:
            audit.update(previous_number=previous, rollout_date=ROLLOUT_BY_DDD[ddd],
                         legacy_prefix=local[0], evidence='historical_numbering_plan', requires_new_confirmation=True)
            notes.append('Nono dígito acrescentado pela regra histórica Anatel 8/9 com DDD e país conhecidos. A alteração não confirma linha ativa, titularidade ou WhatsApp.')
            return resolved(candidate, 'mobile', 'br_smp_8_9_add_ninth', [ANATEL_LEGACY, ANATEL_TRANSITION, ANATEL_SCHEDULE], True)
    return review('invalid_current_format', 'Formato não validado; entrada preservada sem inserir ou descartar dígitos.')

from __future__ import annotations
import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone, timedelta
from uuid import uuid4
import phonenumbers
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator, StrictBool
from .phone_rules import normalize_phone, PHONE_RULES_VERSION

NORMALIZER_VERSION = 'bigbase-2026-09-08.2+' + PHONE_RULES_VERSION + '+phonenumbers-' + phonenumbers.__version__

KINDS = {'identity', 'document', 'phone', 'email', 'address', 'username', 'relationship', 'activity', 'custom'}
FLAGS = {'valid', 'is_whatsapp', 'ownership_confirmed', 'deliverable', 'residence_confirmed'}

def now(): return datetime.now(timezone.utc).isoformat()
def uid(): return str(uuid4())
def canonical(v): return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
def fingerprint(v): return hashlib.sha256(canonical(v).encode()).hexdigest()
def fold(v): return ''.join(c for c in unicodedata.normalize('NFKD', str(v)).casefold() if not unicodedata.combining(c))
def timestamp(v):
    if v is None: return None
    if not isinstance(v,str):raise ValueError('Data deve ser textual com fuso horário')
    t = datetime.fromisoformat(v.replace('Z', '+00:00'))
    if t.tzinfo is None: raise ValueError('Data precisa informar fuso horário')
    return t.astimezone(timezone.utc)

def cpf_valid(v):
    if not re.fullmatch(r'[0-9]{11}', v) or len(set(v)) == 1: return False
    for n in (9, 10):
        d = (sum(int(v[i]) * (n + 1 - i) for i in range(n)) * 10) % 11
        if int(v[n]) != (0 if d == 10 else d): return False
    return True

def normalize(kind, value):
    v = dict(value); notes = []
    if kind == 'document':
        v['type'] = str(v.get('type', '')).upper()
        raw = str(v.get('number', ''))
        v['number'] = re.sub(r'[.\-/\s]', '', raw).upper()
        v['country'] = str(v.get('country', 'BR')).upper()
        if v['type'] == 'CPF': v['syntax_valid'] = cpf_valid(v['number'])
        elif v['type'] == 'CNPJ':
            n = v['number']; good = bool(re.fullmatch(r'[A-Z0-9]{12}[0-9]{2}', n)) and len(set(n)) > 1
            for weights, pos in [([5,4,3,2,9,8,7,6,5,4,3,2],12),([6,5,4,3,2,9,8,7,6,5,4,3,2],13)]:
                if good:
                    mod = sum((ord(c)-48)*w for c,w in zip(n[:pos],weights)) % 11
                    good = int(n[pos]) == (0 if mod < 2 else 11-mod)
            v['syntax_valid'] = good
        else: v['syntax_valid'] = None
    elif kind == 'phone':
        return normalize_phone(value)
    elif kind == 'email':
        address = str(v.get('email', ''))
        if '@' in address:
            local, domain = address.rsplit('@', 1); v['email'] = local + '@' + domain.lower()
        v['syntax_valid'] = bool(re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', address))
    elif kind == 'username':
        v['platform'] = str(v.get('platform', '')).casefold()
    elif kind == 'address' and v.get('country', 'BR') == 'BR' and 'postal_code' in v:
        v['postal_code'] = re.sub(r'[\s-]', '', str(v['postal_code']))
    return v, notes

class FlagEvidence(BaseModel):
    model_config = ConfigDict(extra='forbid')
    value: StrictBool | None
    checked_at: str | None = None
    expires_at: str | None = None
    method: str | None = Field(default=None,max_length=160)
    reference: str | None = Field(default=None,max_length=1000)
    @model_validator(mode='after')
    def chronology(self):
        checked=timestamp(self.checked_at);expires=timestamp(self.expires_at)
        if expires and (not checked or expires<=checked):raise ValueError('Expiração exige verificação anterior com data')
        if self.value is None and expires:raise ValueError('Resultado desconhecido não possui confirmação para vencer')
        return self

class ItemInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: str
    value: dict
    flags: dict[str, bool | None] = Field(default_factory=dict)
    flag_evidence: dict[str, FlagEvidence] = Field(default_factory=dict)
    @model_validator(mode='after')
    def evidence_keys(self):
        if set(self.flag_evidence)-FLAGS:raise ValueError('Tipo de confirmação desconhecido')
        if set(self.flag_evidence)&set(self.flags):raise ValueError('Informe a flag em flags ou flag_evidence, sem duplicar')
        return self
    @field_validator('kind')
    @classmethod
    def kind_ok(cls, v):
        if v not in KINDS: raise ValueError('Tipo desconhecido; use custom e identifique o campo')
        return v
    @field_validator('flags', mode='before')
    @classmethod
    def flags_ok(cls, v):
        if not isinstance(v,dict): raise ValueError('Flags devem ser um objeto')
        if any(k not in FLAGS or (x is not None and type(x) is not bool) for k,x in v.items()): raise ValueError('Flag deve ser true, false ou null e possuir tipo conhecido')
        return v

class Enrichment(BaseModel):
    model_config = ConfigDict(extra='forbid')
    entity_id: str | None = None
    entity_type: str = 'person'
    source_id: str
    external_id: str | None = None
    observed_at: str | None = None
    source_updated_at: str | None = None
    reason: str = ''
    items: list[ItemInput] = Field(min_length=1, max_length=100)
    @field_validator('entity_type')
    @classmethod
    def entity_ok(cls,v):
        if v not in {'person','company'}: raise ValueError('Tipo inválido')
        return v
    @field_validator('observed_at', 'source_updated_at')
    @classmethod
    def date_ok(cls,v): timestamp(v); return v

def item_key(kind, value):
    keys = {'document':['type','country','number'], 'phone':['number','extension'], 'email':['email'], 'username':['platform','username'], 'relationship':['target_id','target_document','type','from','until'], 'activity':['code','role'], 'custom':['field_id','value']}
    if kind == 'identity': return 'identity'
    return fingerprint({k:value.get(k) for k in keys.get(kind, sorted(value))})

_MISSING=object()

def add_observation(entity, item, path, value, source, observed_at, actor, operation, reason, received_at, raw=_MISSING, *, can_apply=True, pending_reason=None):
    previous = item['fields'].get(path)
    effective = timestamp(observed_at)
    future = effective is not None and effective > timestamp(received_at) + timedelta(minutes=5)
    # Dated evidence wins; undated evidence never overwrites dated evidence.
    wins = can_apply and not future and (previous is None or ((effective is not None or previous['observed_at'] is None) and (effective or datetime.min.replace(tzinfo=timezone.utc)) >= (timestamp(previous['observed_at']) or datetime.min.replace(tzinfo=timezone.utc))))
    obs = {'id':uid(),'item_id':item['id'],'path':path,'value':value,'input_value':value if raw is _MISSING else raw,'source_id':source,'observed_at':observed_at,'received_at':received_at,'actor_id':actor,'operation_id':operation,'reason':reason,'applied':wins,'pending_reason':'future_timestamp' if future else pending_reason,'previous':previous['value'] if previous else None}
    entity['observations'].append(obs)
    if wins: item['fields'][path] = {k:obs[k] for k in ['value','source_id','observed_at','received_at','id']}
    if source not in item['sources']: item['sources'].append(source)

def flag_binding(item,flag,values=None):
    if values is None:values={k[6:]:v['value'] for k,v in item['fields'].items() if k.startswith('value.')}
    selected={'phone':['number','extension'],'email':['email'],'username':['platform','username']}.get(item['kind'])
    if item['kind']=='phone' and flag=='is_whatsapp':selected=['number']
    return fingerprint({k:values.get(k) for k in selected} if selected else values)

def record_flags(entity,item,inp,source,observed_at,actor,operation,reason,received_at,confirmed_values=None):
    number_transformed = bool(item['kind']=='phone' and confirmed_values is not None
        and confirmed_values.get('phone_normalization',{}).get('changed_digits'))
    if number_transformed:
        # Evidence received for an eight-digit value cannot silently validate the
        # corrected nine-digit contact. Keep it bound to the original digits.
        confirmed_values={**confirmed_values,'number':confirmed_values['phone_normalization']['previous_number']}
    def record(flag,value,checked_at,verification=None):
        binding=flag_binding(item,flag,confirmed_values)
        applicable=binding==flag_binding(item,flag)
        add_observation(entity,item,'flag.'+flag,value,source,checked_at,actor,operation,reason,received_at,
            can_apply=applicable,pending_reason=None if applicable else 'phone_number_transformed' if number_transformed else 'value_mismatch')
        obs=entity['observations'][-1]
        obs['value_binding']=binding
        if verification is not None:obs['verification']=verification
        if obs['applied']:
            state=item['fields']['flag.'+flag];state['value_binding']=binding
            if verification is not None:state['verification']=verification
    for flag,value in inp.flags.items():record(flag,value,observed_at)
    for flag,evidence in inp.flag_evidence.items():
        at=evidence.checked_at or observed_at
        record(flag,evidence.value,at,{**evidence.model_dump(exclude={'value'}),'checked_at':at})

def project(entity,at=None):
    for item in entity['items']:
        item['value'] = {k[6:]:v['value'] for k,v in item['fields'].items() if k.startswith('value.')}
        item['flags'] = {k[5:]:v['value'] for k,v in item['fields'].items() if k.startswith('flag.')}
        item['flag_details']={}
        for key,state in item['fields'].items():
            if not key.startswith('flag.'):continue
            evidence=state.get('verification',{})
            expires=evidence.get('expires_at')
            applicable=not state.get('value_binding') or state['value_binding']==flag_binding(item,key[5:])
            if not applicable:item['flags'][key[5:]]=None
            item['flag_details'][key[5:]]={'value':state['value'] if applicable else None,'last_result':state['value'],'applicable':applicable,'source_id':state['source_id'],'checked_at':evidence.get('checked_at',state['observed_at']),'received_at':state['received_at'],'expires_at':expires,'stale':bool(expires and timestamp(expires)<=timestamp(at or now())), 'method':evidence.get('method'),'reference':evidence.get('reference'),'observation_id':state['id']}

    identity = next((x for x in entity['items'] if x['kind']=='identity'), None)
    entity['name'] = (identity or {}).get('value', {}).get('name', 'Sem nome informado')
    return entity

"""Pure, bounded, lossless adapters for the two catalogued legacy source indices.

No network, database or clock is used here. Unproven semantics remain pending;
the canonical store decides how to apply facts and records reception separately.
"""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import math
import re
import unicodedata

from .domain import NORMALIZER_VERSION, normalize

ADAPTER_VERSION='legacy-sources-2026-09-08.1'
SOURCES={'pessoas','pessoas_serasa'}
MAX_RECORD_BYTES=2*1024*1024
MAX_LEAVES=20000
MAX_DEPTH=64


class ExactDecimal(Decimal):
    """Use as json.loads(parse_float=ExactDecimal) to retain a decimal token."""
    def __new__(cls,value):
        if not isinstance(value,str) or not re.fullmatch(r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?',value):
            raise ValueError('Token decimal JSON inválido')
        obj=super().__new__(cls,value)
        obj.json_lexeme=value
        return obj


def exact_json(value,sort_keys=True):
    """JSON ASCII text retaining supplied Decimal precision, NUL and surrogates.

This is an exact representation of the available value, not a claim that the
original source bytes or integer/string escape spellings were retained.
"""
    if value is None:return 'null'
    if type(value) is bool:return 'true' if value else 'false'
    if isinstance(value,Decimal):
        if not value.is_finite():raise ValueError('Número não finito')
        return value.json_lexeme if isinstance(value,ExactDecimal) else str(value)
    if type(value) is int:return str(value)
    if type(value) is float:
        if not math.isfinite(value):raise ValueError('Número não finito')
        return repr(value)
    if isinstance(value,str):return json.dumps(value,ensure_ascii=True)
    if isinstance(value,list):return '['+','.join(exact_json(child,sort_keys) for child in value)+']'
    if isinstance(value,dict):
        if any(type(key) is not str for key in value):raise ValueError('Chaves JSON precisam ser textuais')
        keys=sorted(value) if sort_keys else value
        return '{'+','.join(json.dumps(key,ensure_ascii=True)+':'+exact_json(value[key],sort_keys) for key in keys)+'}'
    raise ValueError('Valor não representável em JSON')


def input_type(value):
    if value is None:return 'null'
    if type(value) is bool:return 'boolean'
    if isinstance(value,str):return 'text'
    if isinstance(value,Decimal):return 'decimal'
    if type(value) is int:return 'integer'
    if type(value) is float:return 'float'
    if isinstance(value,dict):return 'object' if value else 'empty_object'
    if isinstance(value,list):return 'array' if value else 'empty_array'
    raise ValueError('Tipo JSON não suportado')


def _typed(value):
    if isinstance(value,dict):return ['object',[[key,_typed(value[key])] for key in sorted(value)]]
    if isinstance(value,list):return ['array',[_typed(child) for child in value]]
    return [input_type(value),exact_json(value)]


def hash_value(value):
    return hashlib.sha256(exact_json(_typed(value)).encode('ascii')).hexdigest()


def _id(*parts):return hashlib.sha256(exact_json(list(parts)).encode('ascii')).hexdigest()
def pointer(parts):return ''.join('/'+str(part).replace('~','~0').replace('/','~1') for part in parts)


def _walk(value,parts=(),depth=0):
    if depth>MAX_DEPTH:raise ValueError('Registro excede profundidade permitida; nenhuma folha foi descartada')
    if isinstance(value,dict) and value:
        for key in sorted(value):
            if type(key) is not str:raise ValueError('Chave de objeto não textual')
            yield from _walk(value[key],parts+(key,),depth+1)
    elif isinstance(value,list) and value:
        for index,child in enumerate(value):yield from _walk(child,parts+(index,),depth+1)
    else:yield parts,value


def _containers(value,parts=()):
    if isinstance(value,(dict,list)):
        yield {'source_path':pointer(parts),'type':'object' if isinstance(value,dict) else 'array','length':len(value)}
        children=value.items() if isinstance(value,dict) else enumerate(value)
        for key,child in children:yield from _containers(child,parts+(key,))


def _safe_text(value):return isinstance(value,str) and '\x00' not in value and not any(0xD800<=ord(char)<=0xDFFF for char in value)
def _text(value):return unicodedata.normalize('NFC',value).strip()

ADDRESS_PESSOAS={'BAIRRO':'neighborhood','CEP':'postal_code','CIDADE':'city','COMPLEMENTO':'complement',
                 'LOGRADOURO':'street','NUMERO':'number','TIPO_ENDERECO':'usage','UF':'state'}
ADDRESS_SERASA={'bairro':'neighborhood','cep':'postal_code','cidade':'city','classificacao':'classification',
                'complemento':'complement','estado':'state','nomeLogradouro':'street','numero':'number',
                'tipoLogradouro':'street_type','tituloLogradouro':'street_title'}
DATE_FIELDS_PESSOAS={'SERASA_DATA_NASCIMENTO','SERASA_DATA_OBITO','SERASA_DT_ATUALIZACAO','SERASA_DT_INCLUSAO',
    'SERASA_DT_SIT_CAD','SERASA_NASC','SERASA_data_nascimento','SERASA_data_nascimento_conjuge','SERASA_data_obito',
    'SERASA_dt_atualizacao','SERASA_dt_exportacao','SERASA_dt_inclusao','SERASA_dt_informacao'}
NAME_ALTERNATIVES={'SERASA_NOME','SERASA_NOME_CIVIL','SERASA_nome_civil','SERASA_nome_completo',
    'SERASA_NOME_MEIO','SERASA_NOME_PRIMEIRO','SERASA_NOME_ULTIMO','SERASA_nome_meio','SERASA_nome_primeiro','SERASA_nome_ultimo'}
RELATIONS={'NOME_MAE':'mother','NOME_PAI':'father','SERASA_NOME_MAE':'mother','SERASA_NOME_PAI':'father',
           'SERASA_nome_mae':'mother','SERASA_nome_pai':'father','SERASA_nome_conjuge':'spouse'}


def _date(value,epoch_allowed=False,calendar_only=True):
    if value is None:return value,'missing_value',{}
    try:
        if epoch_allowed and type(value) is int:
            parsed=datetime(1970,1,1,tzinfo=timezone.utc)+timedelta(milliseconds=value)
            return parsed.date().isoformat() if calendar_only else parsed.isoformat(),None,{'rule':'declared_epoch_millis','timezone':'UTC'}
        if epoch_allowed and isinstance(value,Decimal) and value==value.to_integral_value():
            parsed=datetime(1970,1,1,tzinfo=timezone.utc)+timedelta(milliseconds=int(value))
            return parsed.date().isoformat() if calendar_only else parsed.isoformat(),None,{'rule':'declared_epoch_millis','timezone':'UTC'}
        if not _safe_text(value):return value,'unrecognized_date_type',{}
        text=value.strip()
        if re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}',text):
            return date.fromisoformat(text).isoformat(),None,{'rule':'iso_calendar_date','timezone':None}
        if re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})?',text):
            parsed=datetime.fromisoformat(text.replace('Z','+00:00'))
            result=parsed.date().isoformat() if calendar_only else parsed.isoformat()
            return result,None,{'rule':'iso_calendar_from_datetime' if calendar_only else 'iso_datetime','timezone':str(parsed.tzinfo) if parsed.tzinfo else None}
    except (ValueError,OverflowError):return value,'invalid_date',{}
    return value,'ambiguous_date_format',{}


def _address_value(field,value):
    if value is None:return value,'missing_value'
    if field=='number' and type(value) is int:return str(value),None
    if not _safe_text(value):return value,'unrecognized_address_component'
    text=_text(value)
    if not text:return text,'empty_value'
    if field=='postal_code':
        code=re.sub(r'[\s-]','',text)
        return code,None if re.fullmatch(r'[0-9]{8}',code) else 'postal_code_requires_review'
    if field=='state' and re.fullmatch(r'[A-Za-z]{2}',text):return text.upper(),None
    return text,None


def _base_field(parts):return tuple(part for part in parts if not isinstance(part,int))


def _describe(source,external_id,record,parts,value):
    path=pointer(parts);base=_base_field(parts);name=base[-1] if base else ''
    desc={'target_kind':'custom','target_path':'value','item_key':'custom:'+_id(source,path),
          'item_attributes':{'field_id':'source:'+source+':'+path},'normalized_value':deepcopy(value),
          'status':'pending','pending_reason':'unmapped_semantics','notes':[], 'normalization':{}}
    def mapped(kind,target,normalized,key,attrs=None,reason=None,normalization=None):
        desc.update(target_kind=kind,target_path=target,normalized_value=normalized,item_key=key,item_attributes=attrs or {},
                    status='pending' if reason else 'mapped',pending_reason=reason,normalization=normalization or {})
        return desc
    direct=len(base)==1
    own_cpf=base==('CPF',) or (source=='pessoas' and base==('doc','CPF'))
    if own_cpf:
        if not _safe_text(value) or not value.strip():
            desc.update(pending_reason='cpf_requires_text',cpf_claim=True);return desc
        normalized,_=normalize('document',{'type':'CPF','country':'BR','number':value})
        good=normalized['syntax_valid'] is True
        key='document:'+_id('CPF','BR',normalized['number']) if good else 'document_pending:'+_id(source,external_id,path)
        mapped('document','number',normalized['number'],key,{'type':'CPF','country':'BR','syntax_valid':good},None if good else 'invalid_cpf')
        desc.update(cpf_claim=True,cpf_normalized=normalized['number'],cpf_valid=good)
        return desc
    if direct and name=='NOME':
        if any(isinstance(part,int) for part in parts):desc['pending_reason']='multiple_identity_names';return desc
        if _safe_text(value) and value.strip():return mapped('identity','name',_text(value),'identity')
        desc['pending_reason']='name_requires_text';return desc
    if direct and name=='SEXO':
        if any(isinstance(part,int) for part in parts):desc['pending_reason']='multiple_identity_values';return desc
        if _safe_text(value) and value.strip():return mapped('identity','sex',_text(value),'identity')
        desc['pending_reason']='sex_requires_text';return desc
    if direct and ((source=='pessoas' and name in {'DT_NASCIMENTO','DT_OBITO'}) or (source=='pessoas_serasa' and name=='NASC')):
        if any(isinstance(part,int) for part in parts):desc['pending_reason']='multiple_identity_dates';return desc
        normalized,reason,audit=_date(value,epoch_allowed=source=='pessoas_serasa')
        return mapped('identity','death_date' if name=='DT_OBITO' else 'birth_date',normalized,'identity',reason=reason,normalization=audit)
    if name in RELATIONS and (direct or (source=='pessoas' and base[:1]==('doc',))):
        kind=RELATIONS[name]
        if _safe_text(value) and value.strip():
            return mapped('relationship','target_name',_text(value),'relationship:'+_id(kind,_text(value)),{'type':kind},'identity_resolution_required')
        desc['pending_reason']='relationship_target_requires_text';return desc
    if name in NAME_ALTERNATIVES:
        if _safe_text(value):desc['normalized_value']=_text(value)
        desc.update(pending_reason='name_variant_semantics_unverified',candidate_target='identity.name');return desc
    declared_date=(source=='pessoas' and name in DATE_FIELDS_PESSOAS) or (source=='pessoas_serasa' and name=='DT_INCLUSAO_SERV_PB')
    if declared_date:
        normalized,reason,audit=_date(value,epoch_allowed=True,calendar_only=False)
        desc.update(normalized_value=normalized,pending_reason=reason or 'source_date_scope_unverified',normalization=audit)
        return desc
    phone=direct and ((source=='pessoas' and re.fullmatch(r'(?:CELULAR|TEL_FIXO)[1-5]',name)) or (source=='pessoas_serasa' and name=='TELEFONES'))
    if phone:
        if not _safe_text(value):desc['pending_reason']='phone_requires_text';return desc
        normalized,notes=normalize('phone',{'number':value})
        good=normalized.get('canonical_number') is not None
        number=normalized.get('canonical_number') or value
        attrs={key:val for key,val in normalized.items() if key not in {'number','phone_normalization'}}
        key='phone:'+_id(number,normalized.get('extension')) if good else 'phone_pending:'+_id(source,external_id,path)
        mapped('phone','number',number,key,attrs,None if good else 'phone_requires_review',normalized['phone_normalization'])
        desc['notes']=notes;return desc
    email=direct and ((source=='pessoas' and name=='EMAIL') or (source=='pessoas_serasa' and name=='EMAILS'))
    if email:
        if not _safe_text(value):desc['pending_reason']='email_requires_text';return desc
        normalized,_=normalize('email',{'email':value.strip()})
        good=normalized['syntax_valid'] and not any(char in value for char in ',;<>[]"')
        key='email:'+_id(normalized['email']) if good else 'email_pending:'+_id(source,external_id,path)
        return mapped('email','email',normalized['email'],key,{'syntax_valid':bool(good)},None if good else 'email_format_or_collection_requires_review')
    group=None;mapping=None
    if source=='pessoas' and direct and name in ADDRESS_PESSOAS and len(parts)==1:
        group=record;mapping=ADDRESS_PESSOAS
    elif source=='pessoas_serasa' and len(parts) in {2,3} and parts[0]=='ENDERECOS_JSON' and name in ADDRESS_SERASA:
        group=record['ENDERECOS_JSON']
        if len(parts)==3 and isinstance(parts[1],int):group=group[parts[1]]
        elif len(parts)!=2:group=None
        if isinstance(group,dict):mapping=ADDRESS_SERASA
    if mapping:
        normalized_group={target:_address_value(target,group[field])[0] for field,target in mapping.items() if field in group and not isinstance(group[field],(list,dict))}
        normalized,reason=_address_value(mapping[name],value)
        return mapped('address',mapping[name],normalized,'address:'+_id(normalized_group),reason=reason)
    document_fields={'RG':'RG','TITULO_ELEITOR':'TITULO_ELEITOR','SERASA_RG':'RG','SERASA_rg':'RG',
                     'SERASA_TITULO_ELEITOR':'TITULO_ELEITOR','SERASA_titulo_eleitor':'TITULO_ELEITOR'}
    if direct and name in document_fields:
        if not _safe_text(value) or not value.strip():desc['pending_reason']='document_requires_text';return desc
        normalized,_=normalize('document',{'type':document_fields[name],'country':'BR','number':value})
        return mapped('document','number',normalized['number'],'document:'+_id(document_fields[name],'BR',normalized['number']),
                      {'type':document_fields[name],'country':'BR','syntax_valid':None},'document_validation_not_implemented')
    return desc


def map_record(source_id,external_id,record,source_version=None,adapter_version=ADAPTER_VERSION):
    if source_id not in SOURCES:raise ValueError('Fonte não catalogada')
    if not isinstance(external_id,str) or not external_id or len(external_id)>2048:raise ValueError('ID de origem deve ser textual e limitado')
    if not isinstance(record,dict):raise ValueError('Registro deve ser um objeto')
    if not isinstance(adapter_version,str) or not adapter_version or len(adapter_version)>160:raise ValueError('Versão de adaptador inválida')
    rows=list(_walk(record))
    if len(rows)>MAX_LEAVES:raise ValueError('Registro excede limite de folhas; nenhuma folha foi descartada')
    if len(exact_json(record).encode('ascii'))>MAX_RECORD_BYTES:raise ValueError('Registro excede limite de bytes; nenhuma folha foi descartada')
    record_hash=hash_value(record);facts=[]
    for parts,value in rows:
        path=pointer(parts);description=_describe(source_id,external_id,record,parts,value)
        normalized=description.pop('normalized_value')
        fact={'id':'fact_'+_id(source_id,external_id,source_version,record_hash,adapter_version,NORMALIZER_VERSION,path),
              'source_id':source_id,'source_record_id':external_id,'source_version':deepcopy(source_version),
              'source_path':path,'input_value':deepcopy(value),'input_type':input_type(value),'input_json':exact_json(value),
              'input_encoding':'source_decimal_lexeme' if isinstance(value,ExactDecimal) else 'canonical_value',
              'input_lexeme_available':isinstance(value,ExactDecimal),'input_hash':hash_value(value),
              'normalized_value':normalized,'normalized_type':input_type(normalized),'normalized_json':exact_json(normalized),
              'adapter_version':adapter_version,'normalizer_version':NORMALIZER_VERSION,'observed_at':None,'source_updated_at':None,'flags':{},**description}
        facts.append(fact)
    cpf_facts=[fact for fact in facts if fact.get('cpf_claim')]
    claims=[fact for fact in cpf_facts if fact['input_value'] is not None and fact['input_value']!='' and fact['input_type'] not in {'empty_array','empty_object'}]
    unique={fact.get('cpf_normalized') for fact in claims if fact.get('cpf_valid')}
    eligible=bool(claims) and len(unique)==1 and all(fact.get('cpf_valid') for fact in claims)
    candidate={'type':'CPF','country':'BR','value':next(iter(unique)),'evidence_paths':[fact['source_path'] for fact in claims]} if eligible else None
    if len(unique)>1 or (unique and not eligible):
        for fact in cpf_facts:fact.update(status='pending',pending_reason='conflicting_cpf_claims')
    output={'source_id':source_id,'source_record_id':external_id,'source_version':deepcopy(source_version),
            'record_hash':record_hash,'adapter_version':adapter_version,'normalizer_version':NORMALIZER_VERSION,'entity_type':'person',
            'identity_candidate':candidate,'identity_status':'candidate' if candidate else 'pending',
            'facts':facts,'containers':list(_containers(record)),'input_leaf_count':len(facts)}
    output['coverage']=reconcile_coverage(record,output)
    return output


def reconcile_coverage(record,mapped):
    expected={pointer(parts):(input_type(value),exact_json(value),hash_value(value)) for parts,value in _walk(record)}
    actual={};duplicates=[];invalid_metadata=[]
    for fact in mapped['facts']:
        path=fact['source_path']
        if path in actual:duplicates.append(path)
        value=fact['input_value'];actual[path]=(fact['input_type'],exact_json(value),hash_value(value))
        if fact.get('input_json')!=exact_json(value) or fact.get('input_hash')!=hash_value(value):invalid_metadata.append(path)
    missing=sorted(set(expected)-set(actual));extra=sorted(set(actual)-set(expected))
    changed=sorted(path for path in expected.keys()&actual.keys() if expected[path]!=actual[path])
    expected_containers=sorted(_containers(record),key=lambda row:row['source_path'])
    actual_containers=sorted(mapped.get('containers',[]),key=lambda row:row['source_path'])
    containers_match=hash_value(expected_containers)==hash_value(actual_containers)
    return {'passed':not any([missing,extra,changed,duplicates,invalid_metadata]) and containers_match,
            'missing':missing,'extra':extra,'changed':changed,'duplicates':duplicates,'invalid_metadata':invalid_metadata,
            'containers_match':containers_match,'expected_fields':len(expected),'preserved_fields':len(actual)}

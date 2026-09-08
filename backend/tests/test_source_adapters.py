from copy import deepcopy
from decimal import Decimal
import csv
import json
from pathlib import Path

import pytest

from bigbase.source_adapters import (
    ADAPTER_VERSION, ExactDecimal, exact_json, hash_value, map_record,
    reconcile_coverage,
)


def fact(mapped,path):return next(row for row in mapped['facts'] if row['source_path']==path)


def test_all_source_inventory_paths_have_a_destination_including_containers():
    records={'pessoas':{},'pessoas_serasa':{}}
    inventory=list(csv.DictReader((Path(__file__).parents[2]/'docs/mapeamento-inicial-campos.csv').open()))
    for row in sorted(inventory,key=lambda row:row['field'].count('.')):
        parts=row['field'].split('.');target=records[row['source_id']]
        for part in parts[:-1]:target=target.setdefault(part,{})
        target[parts[-1]]={'object':{},'boolean':False,'integer':0,'long':0,'float':Decimal('0.123456789012345678901234567890'),'date':'2000-01-02'}.get(row['elasticsearch_type'],'Valor Sintético')
    for source,record in records.items():
        mapped=map_record(source,'synthetic-inventory',record)
        assert mapped['coverage']['passed']
        paths={row['source_path'] for row in mapped['facts']+mapped['containers']}
        for row in inventory:
            if row['source_id']==source:assert '/'+row['field'].replace('.','/') in paths
        assert all(row['target_kind'] and row['target_path'] and row['item_key'] for row in mapped['facts'])


def test_typed_values_empty_collections_and_repeated_positions_are_preserved():
    record={'unknown':{'null':None,'false':False,'zero':0,'empty':'','object':{},'array':[],
                       'repeated':['same','same'],'unicode':'Ação 🚀','slash/key~':{'0':False}}}
    mapped=map_record('pessoas','synthetic-shapes',record)
    assert mapped['coverage']['passed']
    assert fact(mapped,'/unknown/null')['input_type']=='null'
    assert fact(mapped,'/unknown/false')['input_type']=='boolean'
    assert fact(mapped,'/unknown/zero')['input_type']=='integer'
    assert fact(mapped,'/unknown/empty')['input_json']=='""'
    assert fact(mapped,'/unknown/object')['input_type']=='empty_object'
    assert fact(mapped,'/unknown/array')['input_type']=='empty_array'
    assert fact(mapped,'/unknown/slash~1key~0/0')['input_value'] is False
    assert fact(mapped,'/unknown/repeated/0')['id']!=fact(mapped,'/unknown/repeated/1')['id']
    assert all(row['status']=='pending' for row in mapped['facts'])


def test_decimal_tokens_nul_surrogates_and_deepcopy_remain_exact():
    raw='{"RENDA":1.23000e+5,"fraction":0.12345678901234567890123456789,"negative_zero":-0.00,"a\\u0000b":{"\\ud800":"x\\u0000y\\udfff"}}'
    record=json.loads(raw,parse_float=ExactDecimal)
    assert deepcopy(record['RENDA']).json_lexeme=='1.23000e+5'
    mapped=map_record('pessoas_serasa','id\x00\ud800',record,source_version='version\x00')
    assert mapped['coverage']['passed']
    assert fact(mapped,'/RENDA')['input_json']=='1.23000e+5'
    assert fact(mapped,'/RENDA')['input_value'].json_lexeme=='1.23000e+5'
    assert fact(mapped,'/RENDA')['input_encoding']=='source_decimal_lexeme'
    assert fact(mapped,'/fraction')['normalized_json']=='0.12345678901234567890123456789'
    assert fact(mapped,'/negative_zero')['input_json']=='-0.00'
    unusual=fact(mapped,'/a\x00b/\ud800')
    assert unusual['input_json']=='"x\\u0000y\\udfff"'
    assert unusual['input_value']=='x\x00y\udfff'
    encoded=exact_json(mapped)
    assert encoded.isascii() and '\x00' not in encoded
    assert json.loads(encoded,parse_float=ExactDecimal)['source_record_id']=='id\x00\ud800'


def test_decimal_without_lexeme_is_exact_but_does_not_claim_original_spelling():
    mapped=map_record('pessoas_serasa','synthetic-decimal',{'RENDA':Decimal('1.23456789012345678901234567890123456789')})
    row=fact(mapped,'/RENDA')
    assert row['input_json']=='1.23456789012345678901234567890123456789'
    assert row['input_lexeme_available'] is False
    assert row['input_encoding']=='canonical_value'
    assert hash_value(False)!=hash_value(0)
    assert hash_value(Decimal('1.0'))!=hash_value(1)


@pytest.mark.parametrize('source',['pessoas','pessoas_serasa'])
def test_valid_cpf_is_only_an_identity_candidate_with_traceable_evidence(source):
    mapped=map_record(source,'synthetic-cpf',{'CPF':'529.982.247-25','NOME':'Pessoa Sintética'})
    assert mapped['identity_candidate']=={'type':'CPF','country':'BR','value':'52998224725','evidence_paths':['/CPF']}
    row=fact(mapped,'/CPF')
    assert row['target_kind']=='document' and row['normalized_value']=='52998224725'
    assert row['item_attributes']=={'type':'CPF','country':'BR','syntax_valid':True}
    assert row['flags']=={} and row['observed_at'] is None


@pytest.mark.parametrize('cpf',['000.000.000-00','111.111.111-11','52998224724',52998224725,False,None,''])
def test_invalid_or_non_text_cpf_never_produces_identity_candidate(cpf):
    mapped=map_record('pessoas','synthetic-invalid',{'CPF':cpf})
    assert mapped['identity_candidate'] is None
    assert fact(mapped,'/CPF')['input_json']==exact_json(cpf)
    assert mapped['coverage']['passed']


@pytest.mark.parametrize('other',['111.444.777-35','000.000.000-00',False])
def test_conflicting_nested_cpf_prevents_identity_grouping(other):
    mapped=map_record('pessoas','synthetic-conflict',{'CPF':'52998224725','doc':{'CPF':other}})
    assert mapped['identity_candidate'] is None
    assert fact(mapped,'/CPF')['pending_reason']=='conflicting_cpf_claims'
    assert mapped['coverage']['passed']


def test_repeated_same_cpf_is_not_conflict_and_homonyms_do_not_create_identity():
    mapped=map_record('pessoas','synthetic-repeat',{'CPF':'52998224725','doc':{'CPF':'529.982.247-25'}})
    assert mapped['identity_candidate']['value']=='52998224725'
    assert mapped['identity_candidate']['evidence_paths']==['/CPF','/doc/CPF']
    first=map_record('pessoas','synthetic-one',{'NOME':'Mesmo Nome Sintético','CELULAR1':'+5511998765432'})
    second=map_record('pessoas','synthetic-two',{'NOME':'Mesmo Nome Sintético','CELULAR1':'+5511998765432'})
    assert first['identity_candidate'] is None and second['identity_candidate'] is None
    assert first['source_record_id']!=second['source_record_id']
    assert {row['id'] for row in first['facts']}.isdisjoint(row['id'] for row in second['facts'])


def test_name_variants_are_not_silently_equated_and_parents_remain_unresolved():
    mapped=map_record('pessoas','synthetic-names',{'NOME':'  Pessoa Sintética  ','SERASA_NOME':'Outro Nome',
        'SERASA_nome_completo':'Terceiro Nome','NOME_MAE':'Mãe Sintética','SERASA_nome_pai':'Pai Sintético'})
    assert fact(mapped,'/NOME')['normalized_value']=='Pessoa Sintética'
    assert fact(mapped,'/NOME')['input_value']=='  Pessoa Sintética  '
    a=fact(mapped,'/SERASA_NOME');b=fact(mapped,'/SERASA_nome_completo')
    assert a['status']==b['status']=='pending' and a['item_key']!=b['item_key']
    mother=fact(mapped,'/NOME_MAE')
    assert mother['target_kind']=='relationship' and mother['item_attributes']['type']=='mother'
    assert mother['pending_reason']=='identity_resolution_required' and 'target_id' not in mother['item_attributes']


def test_dates_are_normalized_without_inventing_source_observation_times():
    mapped=map_record('pessoas','synthetic-dates',{'DT_NASCIMENTO':'2000-02-29','DT_OBITO':'03/04/2020',
        'SERASA_dt_atualizacao':'2020-01-02T03:04:05','SERASA_DT_INCLUSAO':0,'SERASA_NASC':'2001-01-02'})
    assert fact(mapped,'/DT_NASCIMENTO')['normalized_value']=='2000-02-29'
    assert fact(mapped,'/DT_OBITO')['pending_reason']=='ambiguous_date_format'
    assert fact(mapped,'/SERASA_dt_atualizacao')['pending_reason']=='source_date_scope_unverified'
    assert fact(mapped,'/SERASA_dt_atualizacao')['normalization']['timezone'] is None
    assert fact(mapped,'/SERASA_DT_INCLUSAO')['normalized_value']=='1970-01-01T00:00:00+00:00'
    assert all(row['observed_at'] is None and row['source_updated_at'] is None for row in mapped['facts'])
    epoch=map_record('pessoas_serasa','synthetic-epoch',{'NASC':Decimal('0')})
    assert fact(epoch,'/NASC')['normalized_value']=='1970-01-01'


@pytest.mark.parametrize('value',['2000-02-30','03/04/2000',False,[],{}])
def test_ambiguous_dates_are_retained_pending(value):
    mapped=map_record('pessoas','synthetic-date-pending',{'DT_NASCIMENTO':value})
    row=fact(mapped,'/DT_NASCIMENTO')
    assert row['status']=='pending' and row['input_json']==exact_json(value)
    assert mapped['coverage']['passed']


@pytest.mark.parametrize(('field','values'),[('SEXO',['M','F']),('NASC',['2000-01-01','2001-02-02'])])
def test_multiple_identity_values_stay_pending_instead_of_choosing_last(field,values):
    mapped=map_record('pessoas_serasa','synthetic-identity-array',{field:values})
    assert all(row['status']=='pending' and row['target_kind']=='custom' for row in mapped['facts'])
    assert [row['input_value'] for row in mapped['facts']]==values
    assert mapped['coverage']['passed']


def test_multiple_addresses_group_their_own_components_without_crossing_items():
    record={'ENDERECOS_JSON':[{'nomeLogradouro':'Rua Árvore','numero':0,'cidade':'Cidade Alfa','cep':'01234-567','estado':'sp'},
                              {'nomeLogradouro':'Rua Beta','numero':'9','cidade':'Cidade Beta','cep':'22222-000'}]}
    mapped=map_record('pessoas_serasa','synthetic-address',record)
    first=[row for row in mapped['facts'] if row['source_path'].startswith('/ENDERECOS_JSON/0/')]
    second=[row for row in mapped['facts'] if row['source_path'].startswith('/ENDERECOS_JSON/1/')]
    assert len({row['item_key'] for row in first})==len({row['item_key'] for row in second})==1
    assert first[0]['item_key']!=second[0]['item_key']
    assert fact(mapped,'/ENDERECOS_JSON/0/cep')['normalized_value']=='01234567'
    assert fact(mapped,'/ENDERECOS_JSON/0/numero')['normalized_value']=='0'
    assert type(fact(mapped,'/ENDERECOS_JSON/0/numero')['input_value']) is int
    assert fact(mapped,'/ENDERECOS_JSON/0/estado')['normalized_value']=='SP'


def test_flat_address_components_share_one_item_and_unknown_components_survive():
    mapped=map_record('pessoas','synthetic-flat-address',{'LOGRADOURO':'Rua Sintética','NUMERO':'001','CEP':'01234-567',
        'CIDADE':'Cidade Sintética','BAIRRO':'Centro','EXTRA_ADDRESS':{'observação':'Texto original'}})
    address=[row for row in mapped['facts'] if row['target_kind']=='address']
    assert len(address)==5 and len({row['item_key'] for row in address})==1
    assert fact(mapped,'/EXTRA_ADDRESS/observação')['status']=='pending'
    assert mapped['coverage']['passed']


def test_phone_arrays_normalize_without_trusting_column_label_or_creating_flags():
    mapped=map_record('pessoas','synthetic-phones',{'CELULAR1':'+551133334444','TEL_FIXO1':'+5511998765432',
        'CELULAR2':'+551187654321','CELULAR3':'1187654321','CELULAR4':False})
    assert fact(mapped,'/CELULAR1')['item_attributes']['classification']=='fixed'
    assert fact(mapped,'/TEL_FIXO1')['item_attributes']['classification']=='mobile'
    historical=fact(mapped,'/CELULAR2')
    assert historical['normalized_value']=='+5511987654321'
    assert historical['normalization']['changed_digits'] is True
    assert historical['flags']=={} and historical['source_updated_at'] is None
    assert fact(mapped,'/CELULAR3')['status']=='pending'
    assert fact(mapped,'/CELULAR3')['normalized_value']=='1187654321'
    assert fact(mapped,'/CELULAR4')['input_value'] is False
    array=map_record('pessoas_serasa','synthetic-phone-array',{'TELEFONES':['+551133334444','+5511998765432','+551133334444']})
    assert fact(array,'/TELEFONES/0')['item_key']==fact(array,'/TELEFONES/2')['item_key']
    assert fact(array,'/TELEFONES/0')['id']!=fact(array,'/TELEFONES/2')['id']


def test_email_arrays_and_unknown_aggregate_strings_are_preserved():
    mapped=map_record('pessoas_serasa','synthetic-email',{'EMAILS':['User+Tag@EXAMPLE.INVALID','first@example.invalid,second@example.invalid',None]})
    assert fact(mapped,'/EMAILS/0')['normalized_value']=='User+Tag@example.invalid'
    assert fact(mapped,'/EMAILS/0')['flags']=={}
    assert fact(mapped,'/EMAILS/1')['status']=='pending'
    assert fact(mapped,'/EMAILS/1')['input_value']=='first@example.invalid,second@example.invalid'
    assert fact(mapped,'/EMAILS/2')['input_value'] is None


def test_reprocessing_is_deterministic_and_versions_produce_new_fact_ids():
    record={'NOME':'Pessoa Sintética','CPF':'52998224725','new':Decimal('1.2300')}
    first=map_record('pessoas','synthetic-id',record,source_version='1:1')
    same=map_record('pessoas','synthetic-id',dict(reversed(list(record.items()))),source_version='1:1')
    assert exact_json(first)==exact_json(same)
    revised=map_record('pessoas','synthetic-id',record,source_version='1:1',adapter_version=ADAPTER_VERSION+'-revised')
    new_version=map_record('pessoas','synthetic-id',record,source_version='2:1')
    assert revised['record_hash']==first['record_hash']
    assert {row['id'] for row in revised['facts']}.isdisjoint(row['id'] for row in first['facts'])
    assert {row['id'] for row in new_version['facts']}.isdisjoint(row['id'] for row in first['facts'])
    assert [row['item_key'] for row in revised['facts']]==[row['item_key'] for row in first['facts']]


@pytest.mark.parametrize('change',['missing','duplicate','false_to_zero','literal','container_type'])
def test_reconciliation_detects_loss_type_change_and_ambiguous_container_shape(change):
    record={'a':[False],'b':{'0':0}}
    mapped=map_record('pessoas','synthetic-coverage',record)
    if change=='missing':mapped['facts'].pop()
    elif change=='duplicate':mapped['facts'].append(deepcopy(mapped['facts'][0]))
    elif change=='false_to_zero':mapped['facts'][0]['input_value']=0
    elif change=='literal':mapped['facts'][0]['input_json']='0'
    else:next(row for row in mapped['containers'] if row['source_path']=='/a')['type']='object'
    assert reconcile_coverage(record,mapped)['passed'] is False


@pytest.mark.parametrize('record',[{'x':float('nan')},{'x':Decimal('Infinity')},{'x':object()}, {1:'not-json-key'}])
def test_non_json_values_reject_whole_record_without_silent_conversion(record):
    with pytest.raises((ValueError,TypeError)):map_record('pessoas','synthetic-bad',record)


def test_record_limits_raise_instead_of_truncating(monkeypatch):
    import bigbase.source_adapters as adapters
    monkeypatch.setattr(adapters,'MAX_LEAVES',2)
    with pytest.raises(ValueError):map_record('pessoas','synthetic-many',{'values':[1,2,3]})
    monkeypatch.setattr(adapters,'MAX_LEAVES',20000)
    monkeypatch.setattr(adapters,'MAX_RECORD_BYTES',20)
    with pytest.raises(ValueError):map_record('pessoas','synthetic-large',{'value':'x'*30})

"""Deterministic fictional records; never reads source datasets."""
from bigbase.source_adapters import ExactDecimal


def record(profile,index=0):
    basic={'CPF':'00000000000','NOME':'CADASTRO SINTETICO '+str(index),'NASC':'2000-01-01','SEXO':'NAO INFORMADO',
           'EMAILS':['pessoa'+str(index)+'@example.invalid'],'TELEFONES':['00000000'],
           'empty_list':[],'empty_object':{},'unknown_null':None,'unknown_false':False,'unknown_zero':0}
    if profile=='sparse':return basic
    basic['ENDERECOS_JSON']=[{'nomeLogradouro':'LOGRADOURO SINTETICO '+str(i),'numero':i,'bairro':'BAIRRO SINTETICO',
        'cidade':'CIDADE SINTETICA','estado':'ZZ','cep':'00000000','complemento':None,'classificacao':'TESTE'} for i in range(3 if profile=='nested' else 8)]
    basic['unknown']={
        'amount':ExactDecimal('12345678901234567890.1234500e-3'),'negative_zero':ExactDecimal('-0.00'),
        'flags':[False,0,None,True],'unicode':'acento ç 😀','unsafe\x00key\ud800':'value\x00\udfff',
        'array':[{'nested':['x',None,{},[]]} for _ in range(4)],
    }
    basic['measurements']=[{'kind':'SYNTHETIC','value':ExactDecimal(str(index+i)+'.2300e+2'),'valid':i%3==0,
                            'source_date':None,'note':'NOTA SINTETICA SEM VINCULO COM PESSOA REAL '+str(index+i)}
                           for i in range(10 if profile=='nested' else 60)]
    return basic


def add_event_metadata(mapped,index=0):
    for position,fact in enumerate(mapped['facts']):
        fact.update(operation_id='synthetic-operation-'+str(index),actor_id='synthetic-actor',received_at='2026-09-09T00:00:00+00:00')
        if position%7==0:
            fact['flags']={'valid':{'value':[None,False,True][position%3],'observed_at':'2026-09-08T00:00:00Z',
                                    'source_updated_at':None,'source_id':'synthetic-confirmation'}}
            fact['observed_at']='2026-09-08T00:00:00Z'
        if position%11==0:fact['source_updated_at']='2026-09-07T00:00:00Z'
    return mapped

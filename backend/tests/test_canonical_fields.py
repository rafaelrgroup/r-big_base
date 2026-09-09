"""Canonical field schema proofs using only run-owned synthetic stores."""
from copy import deepcopy
from decimal import Decimal
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor

import pytest

from bigbase.canonical_fields import FIELD_CONTRACT
from bigbase.canonical_store import decode, digest
from bigbase.catalogs import create_definition, update_definition, FieldDefinitionInput
from test_canonical_enrichment import writable, payload, send, counts
from test_canonical_http import env, key_for
from test_canonical_store import store


def definition(writable, kind='integer', **kwargs):
    _, app, _, user = writable
    with app.state.store.transaction() as c:
        return create_definition(app.state.store, c, FieldDefinitionInput(name='Sintético', type=kind, **kwargs), user['id'])


def update(writable, field, **changes):
    _, app, _, user = writable
    with app.state.store.transaction() as c:
        return update_definition(app.state.store, c, field['id'], changes, str(field['version']), user['id'])


def custom(field, value, key='custom-1', **metadata):
    return {'kind': 'custom', 'key': key,
            'custom_field': {'contract': FIELD_CONTRACT, 'field_id': field['id'], 'version': field.get('version')},
            'fields': [{'path': 'value', 'value': value, **metadata}]}


@pytest.mark.parametrize('collection', ['people', 'companies'])
@pytest.mark.parametrize('kind,value,options', [
    ('text','',[]), ('integer',0,[]), ('integer',123456789012345678901234567890,[]),
    ('decimal',Decimal('12345678901234567890.12345678901234567890'),[]),
    ('decimal','0.00000000000000000000001',[]), ('boolean',False,[]), ('date','2024-02-29',[]),
    ('enum','A',['A','B']), ('url','https://example.invalid/path',[]), ('integer',None,[]),
])
def test_types_exact_proof_sources_and_multiple_alternatives(writable,store,collection,kind,value,options):
    client, _, _, user = writable; field=definition(writable,kind,options=options,multiple=False)
    item=custom(field,value,observed_at='2026-02-01T01:00:00+01:00',flags={'valid':{'value':False}})
    body=payload(items=[item,custom(field,value,key='alternative')]);before=counts(store)
    response=send(client,body,collection=collection);assert response.status_code==200,response.text
    rows=store.page_history(response.json()['id'])['items'];assert len(rows)==3
    for row in rows:
        m=row['metadata'];assert m['field_id']==field['id'] and m['field_definition_version']==1
        assert m['field_definition']['multiple'] is False and m['canonical_search_state']=='pending'
        assert digest(m['field_definition'])==m['field_definition_sha256']
        assert row['actor_id']==user['id'] and row['source_id']=='manual'
    assert type(rows[0]['input_value']) is type(value) and rows[0]['input_value']==value
    assert rows[0]['normalized_value']==value and rows[0]['observed_at']=='2026-02-01T00:00:00+00:00'
    assert rows[0]['metadata']['field_input_dates']['observed_at'].endswith('+01:00')
    assert counts(store)['outbox']==before['outbox']+1


@pytest.mark.parametrize('kind,value,options', [
    ('text',0,[]),('integer',False,[]),('integer',Decimal('1.1'),[]),('integer','0',[]),
    ('decimal',False,[]),('boolean',0,[]),('date','2025-02-29',[]),('enum','C',['A']),
    ('url','https://user:pass@example.invalid',[]),('url','javascript:alert(1)',[]),
    ('text',['a'],[]),('text',{},[]),('reference',{'entity_type':'person','id':str(uuid4())},[]),
])
def test_invalid_types_atomic_useful_error(writable,store,kind,value,options):
    client,*_=writable;field=definition(writable,kind,options=options);before=counts(store)
    response=send(client,payload(items=[payload()['items'][0],custom(field,value)]))
    assert response.status_code==422,response.text
    assert 'Valor incompatível' in response.text and counts(store)==before


def test_unknown_structures_literal_extras_and_explicit_adoption(writable,store):
    client,*_=writable;field={'id':'not-registered'}
    body=payload(items=[custom(field,{'n':None,'b':False,'z':0,'empty':[]}),payload()['items'][0]])
    owner=send(client,body).json()['id'];rows=store.page_history(owner)['items']
    unknown=[r for r in rows if r['metadata'].get('custom_field')]
    assert len(unknown)==4 and all(r['metadata']['classification_state']=='pending' and r['metadata']['field_definition'] is None for r in unknown)
    before=counts(store);bad=deepcopy(body);bad['items'][0]['custom_field']['version']=1
    assert send(client,bad).status_code==422 and counts(store)==before
    field=definition(writable,'text',id=field['id']);body.update(expected_version=1,items=[custom(field,'classified')])
    assert send(client,body).status_code==200
    assert len(store.page_history(owner)['items'])==6 and rows==store.page_history(owner)['items'][:5]


def test_catalog_version_active_scope_replay_snapshot_and_rollback(writable,store):
    client,*_=writable;field=definition(writable,scope='person');body=payload(items=[custom(field,0)]);key=uuid4().hex
    first=send(client,body,key);assert first.status_code==200;owner=first.json()['id'];before=counts(store)
    assert send(client,body,collection='companies').status_code==422
    renamed=update(writable,field,name='Novo nome');disabled=update(writable,renamed,active=False)
    assert send(client,body,key).json()=={**first.json(),'replayed':True}
    assert send(client,payload(items=[custom(field,1)])).status_code==422
    assert send(client,payload(items=[custom(disabled,1)])).status_code==422
    assert counts(store)==before
    assert store.page_history(owner)['items'][0]['metadata']['field_definition']['name']=='Sintético'
    enabled=update(writable,disabled,active=True);body.update(expected_version=1,items=[custom(enabled,1)])
    assert send(client,body).status_code==200
    assert [r['metadata']['field_definition_version'] for r in store.page_history(owner)['items']]==[1,4]
    with pytest.raises(Exception) as exc:update(writable,enabled,type='text')
    assert exc.value.status_code==409


@pytest.mark.parametrize('bypass',['literal','patch','different_field','legacy_opt_in'])
def test_binding_cannot_be_changed_or_bypassed(writable,store,bypass):
    client,*_=writable;field=definition(writable);item=custom(field,0)
    if bypass=='legacy_opt_in':item.pop('custom_field')
    body=payload(items=[item]);owner=send(client,body).json()['id'];before=counts(store)
    target=store.page_fields(owner)['items'][0]
    if bypass=='patch':
        response=client.patch(f"/api/v1/canonical/people/{owner}/items/{target['item_id']}/value",json={'source_id':'manual','expected_version':1,'field_path':'value','value':'bad'},headers={'Idempotency-Key':uuid4().hex})
    else:
        item=custom(definition(writable) if bypass=='different_field' else field,1)
        if bypass=='literal':item.pop('custom_field')
        body.update(expected_version=1,items=[item]);response=send(client,body)
    assert response.status_code==422,response.text
    assert counts(store)==before


def test_references_resolve_only_canonical_database_and_preserve_structure(writable,store):
    client,app,*_=writable;target=send(client,payload()).json()['id'];field=definition(writable,'reference')
    body=payload(items=[custom(field,{'entity_type':'person','id':target})]);response=send(client,body)
    assert response.status_code==200,response.text
    rows=store.page_history(response.json()['id'])['items'];assert {r['field_path']:r['input_value'] for r in rows}=={'value/entity_type':'person','value/id':target}
    assert all(r['metadata']['field_definition']['type']=='reference' for r in rows)
    before=counts(store);body['items'][0]['fields'][0]['value']['entity_type']='company'
    assert send(client,body).status_code==422 and counts(store)==before
    local_id=str(uuid4())
    with app.state.store.transaction() as c:app.state.store.put(c,'entity',{'id':local_id,'entity_type':'person'})
    body['items'][0]['fields'][0]['value']={'entity_type':'person','id':local_id}
    assert send(client,body).status_code==422 and counts(store)==before


def test_late_future_unknown_dates_flags_and_concurrency(writable,store):
    client,*_=writable;field=definition(writable);body=payload(items=[custom(field,0,observed_at='2026-02-01T00:00:00Z',flags={'valid':{'value':True}})])
    owner=send(client,body).json()['id'];initial=store.page_fields(owner)['items']
    for version,date in [(1,'2025-01-01T00:00:00Z'),(2,'2099-01-01T00:00:00Z'),(3,None)]:
        body.update(expected_version=version,items=[custom(field,version,observed_at=date)])
        assert send(client,body).status_code==200
    stable = lambda rows: [{k:v for k,v in row.items() if k != 'freshness_evaluated_at'} for row in rows]
    assert stable(store.page_fields(owner)['items'])==stable(initial)
    body.update(expected_version=4,items=[custom(field,10,observed_at='2026-04-01T00:00:00Z')])
    with ThreadPoolExecutor(max_workers=2) as pool:responses=list(pool.map(lambda _:send(client,body),range(2)))
    assert sorted(r.status_code for r in responses)==[200,409]
    states=store.page_fields(owner)['items'];assert next(r for r in states if r['dimension']=='value')['value']==10
    assert all(r['value'] is None and r['applicable'] is False for r in states if r['dimension']=='flag:valid')
    history=store.page_history(owner)['items'];assert len(history)==6
    target=history[0]
    validation={'source_id':'manual','expected_version':5,'field_path':'value','value_observation_id':target['observation_id'],'flags':{'valid':{'value':False,'reason':'Revisão sintética'}}}
    response=client.patch(f"/api/v1/canonical/people/{owner}/items/{target['item_id']}/flags",json=validation,headers={'Idempotency-Key':uuid4().hex})
    assert response.status_code==200,response.text
    assert store.page_history(owner)['items'][-1]['metadata']['field_definition']==target['metadata']['field_definition']


def test_permissions_sources_and_lost_response(writable,store,monkeypatch):
    client,app,reads,user=writable;field=definition(writable);body=payload(items=[custom(field,0)]);before=counts(store)
    for scopes,sources in [(['read'],['manual']),(['enrich'],['other'])]:
        token,_=key_for(app,user,scopes=scopes,sources=sources)
        assert send(client,body,headers={'X-API-Key':token}).status_code==403
    assert counts(store)==before
    key=uuid4().hex;original=reads.repository.apply_enrichment
    def lost(*a,**kw):original(*a,**kw);raise ValueError('synthetic response lost')
    monkeypatch.setattr(reads.repository,'apply_enrichment',lost)
    assert send(client,body,key).status_code==503
    monkeypatch.setattr(reads.repository,'apply_enrichment',original)
    response=send(client,body,key);assert response.status_code==200 and response.json()['replayed']
    after=counts(store);body['items'][0]['fields'][0]['value']=2
    assert send(client,body,key).status_code==409 and counts(store)==after


@pytest.mark.parametrize('change',[
    lambda i:i['custom_field'].update(version=True),lambda i:i['custom_field'].update(version=0),
    lambda i:i['custom_field'].update(contract='future'),lambda i:i.update(kind='identity'),
    lambda i:i['fields'][0].update(path='other'),lambda i:i['fields'].append({'path':'extra','value':0}),
    lambda i:i['custom_field'].update(field_definition={'active':True}),
])
def test_explicit_contract_rejects_invalid_atomic(writable,store,change):
    client,*_=writable;item=custom(definition(writable),0);change(item);before=counts(store)
    assert send(client,payload(items=[item])).status_code==422 and counts(store)==before

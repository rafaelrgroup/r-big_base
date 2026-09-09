"""Synthetic PostgreSQL accounts: literal identifiers and explicit platform context."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal
from uuid import uuid4

import pytest
from bigbase.canonical_username import USERNAME_CONTRACT
from bigbase.canonical_enrichment import prepare_enrichment, CONTRACT_VERSION
from bigbase.canonical_store import CanonicalError, VersionConflict
from bigbase.domain import NORMALIZER_VERSION, normalize
from test_canonical_enrichment import writable, payload, send, counts
from test_canonical_http import env, key_for
from test_canonical_store import store


def account(value='@Synthetic.One', key='account-1', platform='Instagram'):
    return {'kind': 'username', 'key': key, 'username_normalization': {'contract': USERNAME_CONTRACT},
            'fields': [{'path': 'username', 'value': value, 'observed_at': '2026-02-01T00:00:00Z'},
                       {'path': 'platform', 'value': platform}]}


@pytest.mark.parametrize(('value', 'platform'), [
    ('@Synthetic.One','Instagram'), ('Mixed.Case+Plus','Telegram'), ('','Other'),
    ('  @Keep Spaces  ','Custom Platform'), ('https://example.invalid/Case','LinkedIn'),
    ('123456789012345678901234567890','WhatsApp'), ('Üser.名','Straße'),
    ('same','X'), ('same','Twitter'), ('same',' instagram '),
])
def test_existing_rule_only_changes_platform(value, platform):
    body=payload(items=[account(value, platform=platform)]); original=deepcopy(body)
    result=prepare_enrichment(body,'person'); user, context=result['facts']
    expected,notes=normalize('username',{'username':value,'platform':platform})
    assert body==original and result['adapter_version']==USERNAME_CONTRACT
    assert user['input_value']==user['normalized_value']==value
    assert context['input_value']==platform and context['normalized_value']==expected['platform']
    assert user['status']=='unknown' and context['status']=='normalized'
    for fact in (user,context):
        assert fact['normalization']==NORMALIZER_VERSION and fact['username_contract']==USERNAME_CONTRACT
        assert fact['username_normalization']['syntax_valid'] is None and fact['normalization_notes']==notes
        assert not fact['flags']
    assert user['context_fields']['platform']=={'value':platform}
    assert len(result['facts'])==2 and not result['containers']


@pytest.mark.parametrize('field', ['username','platform'])
@pytest.mark.parametrize('value', [None, False, 0, Decimal('1.2'), [], {}, {'nested':'literal'}])
def test_nontext_explicit_rejection_keeps_literal_contract(field,value):
    item=account();next(f for f in item['fields'] if f['path']==field)['value']=value
    body=payload(items=[item])
    with pytest.raises(CanonicalError):prepare_enrichment(body,'person')
    item.pop('username_normalization'); result=prepare_enrichment(body,'person')
    assert result['adapter_version']==CONTRACT_VERSION
    assert all('username_contract' not in f for f in result['facts'])
    fact=next(f for f in result['facts'] if f['target_path'].startswith(field))
    assert fact['normalized_value']==(value['nested'] if isinstance(value,dict) and value else value)


@pytest.mark.parametrize('mutate',[
    lambda i:i.update(username_normalization=None),
    lambda i:i.update(username_normalization={'contract':'future'}),
    lambda i:i.update(username_normalization={'contract':USERNAME_CONTRACT,'valid':True}),
    lambda i:i.update(kind='phone'),
    lambda i:i['fields'].pop(0),
    lambda i:i['fields'].pop(1),
    lambda i:i['fields'][1].update(value='  '),
    lambda i:i['fields'][1].update(value=''),
    lambda i:i['fields'][0].update(value='x'*2049),
    lambda i:i['fields'][1].update(value='x'*2049),
])
def test_invalid_contract_http_atomic(writable,store,mutate):
    client,*_=writable;item=account();mutate(item);before=counts(store)
    assert send(client,payload(items=[account(key='first-valid'),item])).status_code==422
    assert counts(store)==before


@pytest.mark.parametrize('collection',['people','companies'])
def test_multiple_accounts_dates_origins_literal_components_replay(writable,store,collection):
    client,_,_,user=writable
    first=account();first['fields'][0].update(source_updated_at='2026-02-01T01:00:00+01:00',flags={'ownership_confirmed':{'value':True}})
    first['fields'][1].update(observed_at='2026-01-01T00:00:00Z',flags={'valid':{'value':True}})
    first['fields'] += [{'path':'url','value':' HTTPS://EXAMPLE.INVALID/@Case ', 'observed_at':'2025-01-01T00:00:00Z'},
                        {'path':'external_id','value':'00000000000000000000000000001'},
                        {'path':'extra','value':{'zero':0,'false':False,'null':None}}]
    body=payload(items=[first,account(key='second',platform='Telegram'),account('@Second',key='third'),account(key='fourth',platform='WhatsApp')]);key=uuid4().hex;before=counts(store)
    response=send(client,body,key,collection);assert response.status_code==200,response.text
    receipt=response.json();owner=receipt['id'];rows=store.page_history(owner)['items']
    assert receipt['observations_created']==15 and len(store.get_entity(owner)['items'])==4
    assert response.headers['cache-control']=='no-store'
    assert counts(store)['outbox']==before['outbox']+1 and counts(store)['identity_keys']==before['identity_keys']+1
    assert all(r['actor_id']==user['id'] and r['source_id']=='manual' for r in rows)
    assert rows[0]['metadata']['username_input_dates']['source_updated_at'].endswith('+01:00')
    assert rows[0]['source_updated_at']=='2026-02-01T00:00:00+00:00'
    assert rows[0]['source_path']=='/items/0/fields/0/value'
    assert rows[0]['metadata']['context_fields']['platform']['observed_at']=='2026-01-01T00:00:00Z'
    assert rows[1]['applied'] and rows[3]['pending_reason']=='value_mismatch'
    assert rows[4]['normalized_value']==' HTTPS://EXAMPLE.INVALID/@Case '
    assert rows[5]['normalized_value']=='00000000000000000000000000001'
    extra={r['field_path']:r['normalized_value'] for r in rows[6:9]}
    assert extra=={'extra/zero':0,'extra/false':False,'extra/null':None}
    assert type(extra['extra/zero']) is int and extra['extra/false'] is False and extra['extra/null'] is None
    assert not any(r['dimension']=='flag:is_whatsapp' for r in rows)
    after=counts(store);assert send(client,body,key,collection).json()=={**receipt,'replayed':True} and counts(store)==after
    changed=deepcopy(body);changed['items'][0]['fields'][0]['value']='different'
    assert send(client,changed,key,collection).status_code==409 and counts(store)==after
    other=deepcopy(body);other['source_record_id']=uuid4().hex
    assert send(client,other,collection=collection).json()['id']!=owner


@pytest.mark.parametrize('flag_value',[True,False,None])
def test_new_literal_username_does_not_inherit_flag_and_other_fields_untouched(writable,store,flag_value):
    client,*_=writable;item=account();item['fields'][0]['flags']={'ownership_confirmed':{'value':flag_value}}
    item['fields'].append({'path':'url','value':'https://example.invalid/Keep'})
    body=payload(items=[item]);owner=send(client,body).json()['id']
    fields=store.page_fields(owner)['items'];field=next(r for r in fields if r['field_path']=='username' and r['dimension']=='value')
    flag=next(r for r in fields if r['dimension']=='flag:ownership_confirmed');assert flag['applicable'] and flag['value'] is flag_value
    patch={'source_id':'manual','expected_version':1,'field_path':'username','value':' @Literal.NEW ', 'observed_at':'2026-04-01T00:00:00Z'}
    response=client.patch(f"/api/v1/canonical/people/{owner}/items/{field['item_id']}/value",json=patch,headers={'Idempotency-Key':uuid4().hex})
    assert response.status_code==200,response.text
    after=store.page_fields(owner)['items'];newflag=next(r for r in after if r['dimension']==flag['dimension'])
    assert newflag['observation_id']==flag['observation_id'] and not newflag['applicable']
    assert [r for r in after if r['field_path'] in ('platform','url')]==[r for r in fields if r['field_path'] in ('platform','url')]
    assert store.page_history(owner)['items'][-1]['normalized_value']==' @Literal.NEW '


@pytest.mark.parametrize('route',['normalized','literal','patch'])
@pytest.mark.parametrize('initial_date',[None,'2026-01-01T00:00:00Z','2099-01-01T00:00:00Z'])
def test_platform_change_cannot_move_account_flags_even_future_platform(writable,store,route,initial_date):
    client,*_=writable;item=account();item['fields'][1]['observed_at']=initial_date
    item['fields'][0]['flags']={'ownership_confirmed':{'value':True}}
    body=payload(items=[item]);owner=send(client,body).json()['id'];before=counts(store)
    if route=='patch':
        target=next(r for r in store.page_fields(owner)['items'] if r['field_path']=='username')
        patch={'source_id':'manual','expected_version':1,'field_path':'platform','value':'telegram','observed_at':'2026-04-01T00:00:00Z'}
        response=client.patch(f"/api/v1/canonical/people/{owner}/items/{target['item_id']}/value",json=patch,headers={'Idempotency-Key':uuid4().hex})
    else:
        changed=deepcopy(body);changed['expected_version']=1;changed['items'][0]['fields'][1]['value']='Telegram'
        if route=='literal':changed['items'][0].pop('username_normalization')
        response=send(client,changed)
    assert response.status_code==422,response.text
    assert counts(store)==before


def test_legacy_conflict_is_rejected_but_matching_context_can_opt_in(writable,store):
    client,*_=writable;item=account(platform='Instagram');item.pop('username_normalization');body=payload(items=[item])
    owner=send(client,body).json()['id'];before=counts(store)
    body.update(expected_version=1,items=[account(platform='Telegram')]);assert send(client,body).status_code==422 and counts(store)==before
    body['items']=[account(platform='INSTAGRAM')];assert send(client,body).status_code==200
    body.update(expected_version=2,items=[account(platform='instagram')]);assert send(client,body).status_code==200
    assert len(store.get_entity(owner)['items'])==1


def test_late_future_missing_dates_and_absent_fields(writable,store):
    client,*_=writable;item=account();item['fields'][1]['observed_at']='2026-01-01T00:00:00Z'
    item['fields'].append({'path':'external_id','value':'00001'})
    body=payload(items=[item]);owner=send(client,body).json()['id'];before=store.page_fields(owner)['items']
    for version,date in [(1,'2025-01-01T00:00:00Z'),(2,'2099-01-01T00:00:00Z'),(3,None)]:
        item=account('late-'+str(version));item['fields'][0]['observed_at']=date;item['fields'][1]['observed_at']='2025-01-01T00:00:00Z'
        body.update(expected_version=version,items=[item]);assert send(client,body).status_code==200
    assert store.page_fields(owner)['items']==before
    assert len(store.page_history(owner)['items'])==9
    body.update(expected_version=4,items=[{'kind':'username','key':'account-1','fields':[{'path':'external_id','value':None,'observed_at':'2026-04-01T00:00:00Z'}]}])
    assert send(client,body).json()['observations_created']==1
    assert [r for r in store.page_fields(owner)['items'] if r['field_path']!='external_id']==[r for r in before if r['field_path']!='external_id']


def test_permission_sources_revocation_and_replay(writable,store):
    client,app,_,user=writable;body=payload(items=[account()]);before=counts(store)
    token,_=key_for(app,user,scopes=['enrich'],sources=['other']);assert send(client,body,headers={'X-API-Key':token}).status_code==403
    token,key=key_for(app,user,scopes=['enrich'],sources=['manual']);body['items'][0]['fields'][0]['flags']={'ownership_confirmed':{'value':True}}
    assert send(client,body,headers={'X-API-Key':token}).status_code==403 and counts(store)==before
    body['items'][0]['fields'][0].pop('flags');request_key=uuid4().hex;response=send(client,body,request_key,headers={'X-API-Key':token});assert response.status_code==200
    assert store.page_history(response.json()['id'])['items'][0]['metadata']['api_key_id']==key['public_id']
    with app.state.store.transaction() as c:key['active']=False;app.state.store.put(c,'api_key',key)
    after=counts(store);assert send(client,body,request_key,headers={'X-API-Key':token}).status_code==401 and counts(store)==after


def test_concurrent_replay_conflict_and_rollback(writable,store,monkeypatch):
    client,_,_,user=writable;prepared=prepare_enrichment(payload(items=[account()]),'person');key=uuid4().hex
    def write(request_key):
        try:return store.apply_enrichment(prepared,actor_id=user['id'],request_key=request_key,expected_version=0)
        except VersionConflict:return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(write,[key,key]))
    assert sorted(r['replayed'] for r in results)==[False,True]
    prepared=prepare_enrichment(payload(items=[account()]),'person')
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(write,[uuid4().hex,uuid4().hex]))
    assert results.count('conflict')==1
    real=store._observe
    def fail(*args,**kwargs):real(*args,**kwargs);raise CanonicalError('Synthetic rollback after username observation')
    monkeypatch.setattr(store,'_observe',fail);before=counts(store)
    assert send(client,payload(items=[account()])).status_code==422 and counts(store)==before


def test_literal_reason_cannot_shadow_platform_binding(writable,store):
    client,*_=writable;item=account();item.pop('username_normalization')
    item['fields'][1]['reason']='Review '+USERNAME_CONTRACT
    body=payload(items=[item]);owner=send(client,body).json()['id']
    body.update(expected_version=1,items=[account()]);assert send(client,body).status_code==200
    item=account(platform='telegram');item.pop('username_normalization');body.update(expected_version=2,items=[item]);before=counts(store)
    assert send(client,body).status_code==422 and counts(store)==before
    assert store.get_entity(owner)['version']==2

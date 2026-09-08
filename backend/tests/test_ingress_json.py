import json
import math
import sys

import pyotp
import pytest
from fastapi.testclient import TestClient

from bigbase.api import create_app
from bigbase.ingress_json import IngressJSONError, load_preserving_json


@pytest.mark.parametrize('literal',[
    '1.234567890123456789','9007199254740993.0','0.1000000000000000001',
    '1e309','-1e309','1e-400','-1e-400','4.9e-324',
])
def test_decimal_guard_rejects_rounding_overflow_and_underflow(literal):
    with pytest.raises(IngressJSONError) as exc:load_preserving_json(literal)
    assert exc.value.code=='json_number_precision'
    assert literal not in str(exc.value)


@pytest.mark.parametrize('literal',['NaN','Infinity','-Infinity'])
def test_json_constants_are_rejected_without_echo(literal):
    with pytest.raises(IngressJSONError) as exc:load_preserving_json(literal)
    assert exc.value.code=='json_non_finite'
    assert literal not in str(exc.value)


@pytest.mark.parametrize('literal',['0.1','1.2300','1e2','1.2345678901234567','5e-324','-0.0'])
def test_decimal_values_that_round_trip_remain_numbers(literal):
    actual=load_preserving_json(literal)
    assert type(actual) is float
    assert actual==json.loads(literal)
    if literal=='-0.0':assert math.copysign(1,actual)==-1


def test_integer_parser_remains_arbitrary_precision_and_strings_stay_exact():
    literal='9'*1000
    assert load_preserving_json(literal)==int(literal)
    payload='{"exact":"1.234567890123456789","zero":0,"false":false,"empty":null}'
    parsed=load_preserving_json(payload)
    assert parsed['exact']=='1.234567890123456789'
    assert type(parsed['zero']) is int and parsed['zero']==0
    assert parsed['false'] is False and parsed['empty'] is None


@pytest.mark.parametrize('payload',[
    '{"private_key":"first","private_key":"second"}',
    '{"nested":[{"private_key":1,"private_key":1}]}',
    '{"private_key":1,"private_\\u006bey":2}',
])
def test_duplicate_keys_are_rejected_including_nested_and_escaped_keys(payload):
    with pytest.raises(IngressJSONError) as exc:load_preserving_json(payload)
    assert exc.value.code=='json_duplicate_keys'
    assert 'private_key' not in str(exc.value)


def test_identical_key_names_in_separate_objects_are_allowed():
    assert load_preserving_json('[{"same":0},{"same":false}]')==[{'same':0},{'same':False}]


@pytest.fixture
def ingress_api(tmp_path):
    app=create_app(tmp_path,testing=True)
    with app.state.store.transaction() as tx:
        user=app.state.security.create_user(tx,'synthetic-ingress','synthetic-password-123','admin')
        secret=app.state.security.secret(user)
    with TestClient(app) as client:
        challenge=client.post('/api/v1/auth/login',json={'username':'synthetic-ingress','password':'synthetic-password-123'}).json()['challenge']
        login=client.post('/api/v1/auth/otp',json={'challenge':challenge,'code':pyotp.TOTP(secret).now()})
        assert login.status_code==200
        client.headers['X-CSRF-Token']=login.json()['csrf']
        yield client,app


def raw_enrichment(value):
    return '{"source_id":"manual","items":[{"kind":"custom","value":{"field_id":"synthetic-json-ingress","value":'+value+'}}]}'


def write_state(app):
    with app.state.store.transaction() as tx:
        return tuple(tx.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in ('objects','events','idempotency'))


@pytest.mark.parametrize(('value','code'),[
    ('1.234567890123456789','json_number_precision'),
    ('1e309','json_number_precision'),('-1e-400','json_number_precision'),
    ('NaN','json_non_finite'),
    ('{"private_key":"private-first","private_key":"private-second"}','json_duplicate_keys'),
])
def test_ingress_rejections_happen_before_any_persistent_write(ingress_api,value,code):
    client,app=ingress_api
    before=write_state(app)
    response=client.post('/api/v1/people/enrich',content=raw_enrichment(value),headers={'Content-Type':'application/json','Idempotency-Key':'synthetic-rejected-ingress'})
    assert response.status_code==422,response.text
    assert response.json()['code']==code and response.json()['request_id']
    assert value not in response.text and 'private_key' not in response.text
    assert write_state(app)==before


def test_rejected_precision_can_be_retried_as_exact_text_using_same_idempotency_key(ingress_api):
    client,app=ingress_api
    literal='1.234567890123456789'
    headers={'Content-Type':'application/json','Idempotency-Key':'synthetic-precision-retry'}
    assert client.post('/api/v1/people/enrich',content=raw_enrichment(literal),headers=headers).status_code==422
    response=client.post('/api/v1/people/enrich',content=raw_enrichment(json.dumps(literal)),headers=headers)
    assert response.status_code==200,response.text
    record=response.json()
    assert record['items'][0]['value']['value']==literal
    assert any(obs['path']=='value.value' and obs['input_value']==literal for obs in record['observations'])


@pytest.mark.parametrize('content_type',['Application/JSON; charset=utf-8','application/vnd.bigbase+json'])
def test_json_media_type_variants_cannot_bypass_guard(ingress_api,content_type):
    client,app=ingress_api
    before=write_state(app)
    response=client.post('/api/v1/people/enrich',content=raw_enrichment('1e999'),headers={'Content-Type':content_type,'Idempotency-Key':'synthetic-media-type'})
    assert response.status_code==422
    assert response.json()['code']=='json_number_precision'
    assert write_state(app)==before


def test_size_limit_and_multipart_upload_remain_intact(ingress_api):
    client,app=ingress_api
    before=write_state(app)
    response=client.post('/api/v1/people/enrich',content=b'x'*(2*1024*1024+1),headers={'Content-Type':'application/json','Idempotency-Key':'synthetic-too-large'})
    assert response.status_code==413
    assert write_state(app)==before
    boundary='synthetic-json-in-boundary'
    multipart='--'+boundary+'\r\nContent-Disposition: form-data; name="file"; filename="synthetic.csv"\r\nContent-Type: text/csv\r\n\r\nPessoa Sintética\r\n--'+boundary+'--\r\n'
    uploaded=client.post('/api/v1/bulk-queries/uploads',content=multipart.encode(),headers={'Content-Type':'multipart/form-data; boundary='+boundary})
    assert uploaded.status_code==201,uploaded.text
    assert uploaded.json()['count']==1


def test_python_integer_digit_limit_returns_422_without_crash_or_write(ingress_api):
    limit=sys.get_int_max_str_digits()
    if not limit:pytest.skip('Python integer digit limit is disabled in this runtime')
    client,app=ingress_api
    before=write_state(app)
    literal='9'*(limit+1)
    response=client.post('/api/v1/people/enrich',content=raw_enrichment(literal),headers={'Content-Type':'application/json','Idempotency-Key':'synthetic-integer-limit'})
    assert response.status_code==422
    assert literal not in response.text
    assert write_state(app)==before

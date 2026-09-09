from decimal import Decimal
import json
import pytest

from bigbase.source_adapters import ExactDecimal
from observation_codec import CodecError, typed_bytes
from wire_codec import dumps, loads


@pytest.mark.parametrize('value', [ExactDecimal('-0.00'), Decimal('1.2300000'),
    {'values': None, 'decimals': [[[], 'lexical', 'fake']], '0': Decimal('0.000')},
    [None, False, 0, -0.0, 'nul\x00surrogate\ud800', ExactDecimal('1.234500e+20')],
    {'a/b~c': [{'1': ExactDecimal('1.23e+40')}, {'value': Decimal('-0.00')}]},
    1234567890123456789012345678901234567890])
def test_exact_roundtrip_and_user_keys_do_not_collide_with_format(value):
    assert typed_bytes(loads(dumps(value))) == typed_bytes(value)


@pytest.mark.parametrize('locations', [
    [[['absent'], 'decimal', '1']],
    [[[True], 'decimal', '1']],
    [[[0], 'decimal', '1']],
    [[['v'], 'decimal', '1'], [['v'], 'decimal', '2']],
    [[['v'], 'unknown', '1']],
    [[['v'], 'decimal', 'NaN']],
    [[['v'], 'lexical', 'Infinity']],
])
def test_invalid_decimal_markers_never_overwrite_an_unrelated_value(locations):
    raw = json.dumps({'values': {'v': None}, 'decimals': locations}).encode()
    with pytest.raises(CodecError): loads(raw)


def test_marker_cannot_replace_non_null_value_and_duplicates_are_rejected():
    with pytest.raises(CodecError):
        loads(b'{"values":{"v":false},"decimals":[[["v"],"decimal","1"]]}')
    with pytest.raises(CodecError):
        loads(b'{"values":{"v":null,"v":null},"decimals":[]}')


def test_depth_and_nonfinite_values_are_refused():
    value = None
    for _ in range(100): value = [value]
    with pytest.raises(CodecError): dumps(value)
    with pytest.raises(CodecError): loads(json.dumps({'values': value, 'decimals': []}).encode())
    with pytest.raises(CodecError): dumps(float('nan'))
    with pytest.raises(CodecError): loads(b'{"values":NaN,"decimals":[]}')

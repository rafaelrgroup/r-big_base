"""Exact JSON wire values; decimal type/lexeme live in explicit side metadata."""
from decimal import Decimal
import json
import math

from bigbase.source_adapters import ExactDecimal
from observation_codec import CodecError, require

MAX_DEPTH = 96


def dumps(value):
    decimals = []
    def walk(node, path):
        require(len(path) <= MAX_DEPTH, 'VALUE_DEPTH_LIMIT')
        if node is None or type(node) in (str, bool, int):
            return node
        if type(node) is float:
            require(math.isfinite(node), 'NONFINITE_NUMBER')
            return node
        if isinstance(node, Decimal):
            require(node.is_finite(), 'NONFINITE_NUMBER')
            lexical = isinstance(node, ExactDecimal)
            text = node.json_lexeme if lexical else str(node)
            if lexical:
                require(ExactDecimal(text).as_tuple() == node.as_tuple(), 'INVALID_DECIMAL_LEXEME')
            decimals.append([list(path), 'lexical' if lexical else 'decimal', text])
            return None
        if type(node) is list:
            return [walk(child, path + (index,)) for index, child in enumerate(node)]
        require(type(node) is dict and all(type(k) is str for k in node), 'UNSUPPORTED_VALUE_TYPE')
        return {key: walk(node[key], path + (key,)) for key in sorted(node)}
    try:
        transformed = walk(value, ())
        return json.dumps({'values': transformed, 'decimals': decimals}, ensure_ascii=True,
                          sort_keys=True, separators=(',', ':'), allow_nan=False).encode('ascii')
    except (ValueError, OverflowError, RecursionError) as error:
        if isinstance(error, CodecError):
            raise
        raise CodecError('WIRE_ENCODING_FAILED') from None


def loads(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'DUPLICATE_WIRE_KEY')
            result[key] = value
        return result
    try:
        envelope = json.loads(raw, object_pairs_hook=pairs,
                              parse_constant=lambda _: (_ for _ in ()).throw(CodecError('NONFINITE_NUMBER')))
    except (ValueError, RecursionError):
        raise CodecError('WIRE_DECODING_FAILED') from None
    require(type(envelope) is dict and set(envelope) == {'values', 'decimals'}
            and type(envelope['decimals']) is list, 'INVALID_WIRE_ENVELOPE')
    result = envelope['values']
    stack = [(result, 0)]
    while stack:
        value, depth = stack.pop()
        require(depth <= MAX_DEPTH, 'VALUE_DEPTH_LIMIT')
        if type(value) in (dict, list):
            stack.extend((v, depth+1) for v in (value.values() if type(value) is dict else value))
        elif type(value) is float:
            require(math.isfinite(value), 'NONFINITE_NUMBER')
    seen = set()
    for row in envelope['decimals']:
        require(type(row) is list and len(row) == 3 and type(row[0]) is list and len(row[0]) <= MAX_DEPTH,
                'INVALID_DECIMAL_LOCATION')
        path, kind, text = row
        require(all(type(k) in (str, int) for k in path) and tuple(path) not in seen
                and kind in ('decimal', 'lexical') and type(text) is str, 'INVALID_DECIMAL_LOCATION')
        seen.add(tuple(path))
        try:
            number = ExactDecimal(text) if kind == 'lexical' else Decimal(text)
            require(number.is_finite(), 'NONFINITE_NUMBER')
            if not path:
                require(result is None, 'DECIMAL_LOCATION_NOT_EMPTY')
                result = number
                continue
            parent = result
            for key in path[:-1]:
                require((type(parent) is dict and type(key) is str and key in parent)
                        or (type(parent) is list and type(key) is int and 0 <= key < len(parent)),
                        'INVALID_DECIMAL_LOCATION')
                parent = parent[key]
            key = path[-1]
            require((type(parent) is dict and type(key) is str and key in parent)
                    or (type(parent) is list and type(key) is int and 0 <= key < len(parent)),
                    'INVALID_DECIMAL_LOCATION')
            require(parent[key] is None, 'DECIMAL_LOCATION_NOT_EMPTY')
            parent[key] = number
        except (ValueError, ArithmeticError, KeyError, IndexError, TypeError):
            raise CodecError('INVALID_DECIMAL_LOCATION') from None
    return result

"""Reject JSON input that the normal decoder would silently change or overwrite.

Decimals are compared by their value, not their spelling: 1.2300 and 1.23 may
share a representation, but a rounded decimal must be supplied as exact text.
Integers retain Python's normal arbitrary-precision parser and its size limit.
"""
import json
import math
from decimal import Decimal, InvalidOperation
from email.message import Message


class IngressJSONError(ValueError):
    def __init__(self,code,message):
        self.code=code
        super().__init__(message)


def is_json_content_type(content_type):
    """Match FastAPI's JSON media types without inspecting multipart boundaries."""
    if not content_type:return False
    message=Message();message['content-type']=content_type
    subtype=message.get_content_subtype()
    return message.get_content_maintype()=='application' and (subtype=='json' or subtype.endswith('+json'))


def _number_precision_error():
    return IngressJSONError('json_number_precision','Precisão do número JSON não pode ser preservada; envie o valor exato como texto em um campo compatível.')


def _preserving_float(literal):
    value=float(literal)
    if not math.isfinite(value):raise _number_precision_error()
    try:
        original=Decimal(literal)
        recovered=Decimal(repr(value))
    except InvalidOperation:
        raise _number_precision_error() from None
    if original!=recovered:raise _number_precision_error()
    return value


def _reject_constant(_value):
    raise IngressJSONError('json_non_finite','JSON não permite números não finitos.')


def _unique_object(pairs):
    result={}
    for key,value in pairs:
        if key in result:
            raise IngressJSONError('json_duplicate_keys','JSON contém chaves repetidas; envie cada chave apenas uma vez em cada objeto.')
        result[key]=value
    return result


def load_preserving_json(body):
    return json.loads(body,parse_float=_preserving_float,parse_constant=_reject_constant,object_pairs_hook=_unique_object)

"""Semântica de filtros para validação local; busca de produção será indexada."""
from fastapi import HTTPException
from datetime import date
from .domain import fold, FLAGS, normalize

FIELDS={'document','phone','name','entity_type','updated_at','id','source_id','kind','number','type','country','email','platform','username','city','state','street','postal_code','birth_date','age','sex','code','target_id','valid','is_whatsapp','ownership_confirmed','deliverable','residence_confirmed','classification','usage','field_id','value'}
OPS={'eq','contains','prefix','in','range','exists','is_null'}

def validate_filter(node,depth=0):
    if depth>8 or not isinstance(node,dict): raise HTTPException(422,'Filtro inválido ou profundo demais')
    if not node: return
    if 'and' in node or 'or' in node:
        key='and' if 'and' in node else 'or'; children=node[key]
        if set(node)!={key} or not isinstance(children,list) or not 1<=len(children)<=50: raise HTTPException(422,'Grupo de filtros inválido')
        for child in children: validate_filter(child,depth+1)
    elif 'item' in node:
        if set(node)!={'item'}: raise HTTPException(422,'Filtro de item inválido')
        validate_filter(node['item'],depth+1)
    else:
        if node.get('field') not in FIELDS or node.get('op','eq') not in OPS or set(node)-{'field','op','value'}: raise HTTPException(422,'Campo ou operador não suportado')
        op=node.get('op','eq'); v=node.get('value')
        if node['field'] in {'document','phone'} and (op not in {'eq','in'} or (op=='eq' and not isinstance(v,str)) or (op=='in' and (not isinstance(v,list) or any(not isinstance(x,str) for x in v)))):raise HTTPException(422,'Documento/telefone exige texto e comparação eq ou in')
        if node['field'] in FLAGS and (op not in {'eq','in','exists','is_null'} or (op=='eq' and v is not None and type(v) is not bool)):raise HTTPException(422,'Flag exige true, false ou null')
        if node['field']=='age' and op in {'eq','in','range'}:
            vals=v if isinstance(v,list) else [v]
            if any(x is not None and (type(x) is not int or not 0<=x<=150) for x in vals):raise HTTPException(422,'Idade deve ser inteira, de 0 a 150')
        if node['field'] in FLAGS and op=='in' and isinstance(v,list) and any(x is not None and type(x) is not bool for x in v):raise HTTPException(422,'Lista de flags exige true, false ou null')
        if op in {'exists','is_null'} and type(v) is not bool:raise HTTPException(422,'Operador exige true ou false')
        if op=='in' and (not isinstance(v,list) or len(v)>1000): raise HTTPException(422,'in exige uma lista de até 1000 valores')
        if op=='range' and (not isinstance(v,list) or len(v)!=2): raise HTTPException(422,'range exige dois limites')
        if op=='range':
            bounds=[x for x in v if x is not None]
            if any(type(x) not in {int,float,str} for x in bounds):raise HTTPException(422,'Intervalo exige limites numéricos ou textuais, sem booleanos')
            if any(isinstance(x,str) for x in bounds) and not all(isinstance(x,str) for x in bounds):raise HTTPException(422,'Os limites do intervalo devem possuir o mesmo tipo')
        if op in {'contains','prefix'} and (not isinstance(v,str) or len(v)<(3 if op=='contains' else 1)): raise HTTPException(422,'Texto de busca curto demais')

def json_equal(left,right):
    """Compare JSON values without Python's bool/int or nested-container coercion."""
    if left is None or right is None:return left is right
    if type(left) is bool or type(right) is bool:return type(left) is type(right) and left is right
    if type(left) in {int,float} and type(right) in {int,float}:return left==right
    if type(left) is not type(right):return False
    if isinstance(left,dict):return left.keys()==right.keys() and all(json_equal(left[k],right[k]) for k in left)
    if isinstance(left,list):return len(left)==len(right) and all(json_equal(a,b) for a,b in zip(left,right))
    return isinstance(left,str) and left==right

def compare(values,op,v):
    if op=='exists': return bool(values)==bool(v)
    if op=='is_null': return any(x is None for x in values) if v is not False else any(x is not None for x in values)
    def one(x):
        if op=='eq': return json_equal(x,v)
        if op=='in': return any(json_equal(x,candidate) for candidate in v)
        if x is None: return False
        if op=='contains': return fold(v) in fold(x)
        if op=='prefix': return fold(x).startswith(fold(v))
        if op=='range':
            if type(x) not in {int,float,str}:return False
            if any(bound is not None and (type(bound) not in {int,float,str} or isinstance(x,str)!=isinstance(bound,str)) for bound in v):return False
            try: return (v[0] is None or x>=v[0]) and (v[1] is None or x<=v[1])
            except TypeError: return False
        return False
    return any(one(x) for x in values)

def matches(entity,node,items=None,include_invalid=False,reference_date=None):
    if not node: return True
    its=items if items is not None else [i for i in entity['items'] if include_invalid or i['flags'].get('valid') is not False]
    if 'and' in node:return all(matches(entity,n,items,include_invalid,reference_date) for n in node['and'])
    if 'or' in node:return any(matches(entity,n,items,include_invalid,reference_date) for n in node['or'])
    if 'item' in node:return any(matches(entity,node['item'],[i],include_invalid,reference_date) for i in its)
    field=node['field']; values=[]
    if field in {'name','entity_type','updated_at','id'} and items is None: values=[entity.get(field)]
    else:
        for i in its:
            if field=='age' and i['kind']=='identity':
                try:
                    born=date.fromisoformat(i['value'].get('birth_date',''));today=reference_date or date.today()
                    values.append(today.year-born.year-((today.month,today.day)<(born.month,born.day)))
                except (ValueError,TypeError):pass
            elif field in {'document','phone'} and i['kind']==field:values.append(i['value'].get('number'))
            elif field=='kind':values.append(i['kind'])
            elif field=='source_id':values.extend(i['sources'])
            elif field in i['value']:values.append(i['value'][field])
            elif field in i['flags']:values.append(i['flags'][field])
            elif field in FLAGS and node.get('op')=='is_null' and (field!='is_whatsapp' or i['kind']=='phone'):values.append(None)
    op=node.get('op','eq');v=node.get('value')
    if field in {'document','phone'}:
        convert=lambda x:normalize(field,{'number':x})[0].get('number')
        v=[convert(x) for x in v] if op=='in' else convert(v)
    if field=='name' and op in {'eq','in'}:
        values=[fold(x) for x in values];v=[fold(x) for x in v] if op=='in' else fold(v)
    return compare(values,op,v)

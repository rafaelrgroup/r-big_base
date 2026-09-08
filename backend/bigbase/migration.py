"""Inventário e reconciliação sem escrita: nunca conecta às bases de origem.

Cada folha tem valor/tipo/caminho preservados. Regras ambíguas permanecem pendentes.
O transporte de carga real é uma etapa distinta, dependente de backup e destino.
"""
import csv
from pathlib import Path
from .domain import fingerprint

def leaves(value,path=''):
    if isinstance(value,dict) and value:
        for key,child in value.items():yield from leaves(child,path+'/'+str(key).replace('~','~0').replace('/','~1'))
    elif isinstance(value,list) and value:
        for index,child in enumerate(value):yield from leaves(child,path+'/'+str(index))
    else:yield path,value

def value_type(value):
    if value is None:return 'null'
    if isinstance(value,bool):return 'boolean'
    if isinstance(value,str):return 'text'
    if isinstance(value,int):return 'integer'
    if isinstance(value,float):return 'decimal'
    return 'empty_object' if isinstance(value,dict) else 'empty_array'

def rule(source,path):
    field=path.removeprefix('/')
    direct={'CPF':'document.number','NOME':'identity.name','SEXO':'identity.sex'}
    if field in direct:return direct[field],'mapped'
    if field=='NOME_MAE':return 'relationship.pending_mother_name','pending_identity_resolution'
    if field in {'NASC','DT_NASCIMENTO'}:return 'identity.birth_date','pending_date_validation'
    if any(word in field.upper() for word in ['CELULAR','TELEFONE','FONE','EMAIL']):return 'custom.original_contact','pending_contact_parse'
    return 'custom.original_field','pending_mapping'

def inspect_record(source,external_id,record,source_version=None):
    if source not in {'pessoas','pessoas_serasa'}:raise ValueError('Fonte não catalogada')
    if not isinstance(record,dict):raise ValueError('Registro deve ser um objeto')
    atoms=[]
    for path,value in leaves(record):
        target,status=rule(source,path)
        atoms.append({'source_id':source,'external_id':str(external_id),'source_version':source_version,'source_path':path,'input_value':value,'input_type':value_type(value),'input_sha256':fingerprint(value),'target':target,'status':status})
    return {'source_id':source,'external_id':str(external_id),'record_sha256':fingerprint(record),'atoms':atoms,'input_leaf_count':len(atoms)}

def reconcile(record,atoms):
    expected={path:(value_type(value),fingerprint(value)) for path,value in leaves(record)}
    actual={};duplicates=[]
    for atom in atoms:
        path=atom['source_path']
        if path in actual:duplicates.append(path)
        actual[path]=(atom['input_type'],fingerprint(atom['input_value']))
    missing=sorted(set(expected)-set(actual));extra=sorted(set(actual)-set(expected))
    changed=sorted(k for k in expected.keys()&actual.keys() if expected[k]!=actual[k])
    return {'passed':not any([missing,extra,changed,duplicates]),'missing':missing,'extra':extra,'changed':changed,'duplicates':duplicates,'expected_fields':len(expected),'preserved_fields':len(actual)}

def catalog_inventory(input_path,output_path):
    with open(input_path,newline='',encoding='utf-8') as src,open(output_path,'w',newline='',encoding='utf-8') as dst:
        writer=csv.DictWriter(dst,fieldnames=['source_id','field','elasticsearch_type','target','status']);writer.writeheader()
        for row in csv.DictReader(src):
            target,status=rule(row['indice'],'/'+row['campo'].replace('.','/'))
            writer.writerow({'source_id':row['indice'],'field':row['campo'],'elasticsearch_type':row['tipo'],'target':target,'status':status})

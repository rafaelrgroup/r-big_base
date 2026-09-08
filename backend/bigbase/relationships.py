"""Resolução de vínculos local, sem fundir pessoas por nome ou contato."""
from .domain import normalize, item_key

INVERSE={'mother':'child','father':'child','child':'parent','sibling':'sibling','spouse':'spouse','possible_relative':'possible_relative','mãe':'filho/filha','pai':'filho/filha','filho':'pai/mãe','filha':'pai/mãe','irmão':'irmão/irmã','cônjuge':'cônjuge'}

def resolve(item,owner_id,entities):
    value=item['value'];target=value.get('target_id')
    if target:
        if target==owner_id:return {'status':'self_reference','target_id':None}
        other=next((e for e in entities if e['id']==target),None)
        return {'status':'resolved' if other else 'pending_target','target_id':target if other else None,'target_type':other['entity_type'] if other else None,'method':'explicit_id'}
    if value.get('target_document'):
        document=normalize('document',{'number':value['target_document'],'type':value.get('target_document_type','CPF'),'country':value.get('target_country','BR')})[0]
        if document.get('syntax_valid') is not True:return {'status':'pending_document_validation','target_id':None}
        key=item_key('document',document)
        owners={e['id']:e for e in entities for i in e['items'] if i['kind']=='document' and i['key']==key and i['value'].get('syntax_valid') is True and i['flags'].get('valid') is not False}
        if owner_id in owners:return {'status':'self_reference','target_id':None}
        if len(owners)==1:
            other=next(iter(owners.values()))
            return {'status':'resolved','target_id':other['id'],'target_type':other['entity_type'],'method':'consistent_document'}
        return {'status':'ambiguous_document' if owners else 'pending_target','target_id':None}
    return {'status':'pending_identity','target_id':None}

def connections(entity,entities):
    outgoing=[];incoming=[]
    for owner in entities:
        for item in owner['items']:
            if item['kind']!='relationship':continue
            resolution=resolve(item,owner['id'],entities)
            record={'owner_id':owner['id'],'owner_name':owner['name'],'item':item,'resolution':resolution,'role':item['value'].get('type'),'observations':[o for o in owner['observations'] if o['item_id']==item['id']]}
            if owner['id']==entity['id']:outgoing.append(record)
            if resolution.get('target_id')==entity['id']:
                incoming.append({**record,'inverse_role':INVERSE.get(item['value'].get('type')),'derived':True})
    return {'entity_id':entity['id'],'outgoing':outgoing,'incoming':incoming}

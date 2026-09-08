from copy import deepcopy
import pytest
from bigbase.domain import ItemInput, add_observation, record_flags, project

def item_with_current_name():
    entity={'items':[],'observations':[]}
    item={'id':'synthetic-item','kind':'identity','fields':{},'sources':[]}
    entity['items'].append(item)
    add_observation(entity,item,'value.name','Nome Atual Sintético','source-current','2026-03-01T00:00:00Z','actor','original','', '2026-04-01T00:00:00Z')
    return entity,item

@pytest.mark.parametrize('evidence',[False,True])
def test_old_input_confirmation_never_validates_current_value(evidence):
    entity,item=item_with_current_name()
    inp=ItemInput(kind='identity',value={'name':'Nome Antigo Sintético'},**({'flag_evidence':{'valid':{'value':True,'checked_at':'2026-03-10T00:00:00Z'}}} if evidence else {'flags':{'valid':True}}))
    add_observation(entity,item,'value.name',inp.value['name'],'old-source','2026-01-01T00:00:00Z','actor','late','', '2026-04-01T00:00:00Z')
    record_flags(entity,item,inp,'old-source','2026-01-01T00:00:00Z','actor','late','', '2026-04-01T00:00:00Z',confirmed_values=inp.value)
    project(entity)
    assert entity['name']=='Nome Atual Sintético'
    assert item['flags'].get('valid') is None
    assert entity['observations'][-1]['applied'] is False
    assert entity['observations'][-1]['pending_reason']=='value_mismatch'


def test_confirmation_for_another_value_does_not_replace_current_confirmation():
    entity,item=item_with_current_name()
    first=ItemInput(kind='identity',value={},flags={'valid':False})
    record_flags(entity,item,first,'source-current','2026-03-01T00:00:00Z','actor','first','', '2026-04-01T00:00:00Z')
    state=deepcopy(item['fields']['flag.valid'])
    incoming=ItemInput(kind='identity',value={'name':'Outro Nome Sintético'},flag_evidence={'valid':{'value':True,'checked_at':'2026-03-30T00:00:00Z'}})
    record_flags(entity,item,incoming,'other-source','2026-03-30T00:00:00Z','actor','second','', '2026-04-01T00:00:00Z',confirmed_values=incoming.value)
    project(entity)
    assert item['flags']['valid'] is False
    assert item['fields']['flag.valid']==state
    assert len(entity['observations'])==3

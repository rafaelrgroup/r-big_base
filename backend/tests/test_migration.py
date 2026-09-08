import copy
import pytest
from bigbase.migration import inspect_record,reconcile

RECORD={'CPF':'00001234567','NOME':'Pessoa Sintética','unknown':{'a/b':False,'empty':[],'nested':[None,0,'',{'~key':'ação'}]},'phones':['123','123']}

def test_every_leaf_and_position_preserved():
    result=inspect_record('pessoas','synthetic-id',RECORD)
    assert reconcile(RECORD,result['atoms'])['passed']
    paths={a['source_path'] for a in result['atoms']}
    assert '/unknown/a~1b' in paths and '/unknown/nested/3/~0key' in paths
    assert '/phones/0' in paths and '/phones/1' in paths
    assert any(a['input_value']=='00001234567' for a in result['atoms'])

@pytest.mark.parametrize('change',['remove','overwrite','duplicate','type_change'])
def test_reconciliation_detects_loss_and_change(change):
    atoms=copy.deepcopy(inspect_record('pessoas_serasa','synthetic-id',RECORD)['atoms'])
    if change=='remove':atoms.pop()
    elif change=='overwrite':atoms[0]['input_value']='changed'
    elif change=='duplicate':atoms.append(copy.deepcopy(atoms[0]))
    else:atoms[0]['input_value']=1234567
    assert not reconcile(RECORD,atoms)['passed']

def test_unknown_fields_have_explicit_destination():
    plan=inspect_record('pessoas','synthetic-id',RECORD)
    assert all(a['target'] and a['status'] for a in plan['atoms'])
    assert any(a['status']=='pending_mapping' for a in plan['atoms'])

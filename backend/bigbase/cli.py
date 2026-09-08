import argparse
import getpass
import os
from pathlib import Path
from .api import create_app
from .domain import uid, now, normalize, item_key, add_observation, project

def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['create-admin','seed-demo']);p.add_argument('--data',default='var');p.add_argument('--username',default='admin');args=p.parse_args()
    app=create_app(args.data,testing=True);s=app.state.store
    if args.action=='create-admin':
        password=getpass.getpass('Senha inicial (mínimo 12 caracteres): ')
        if password!=getpass.getpass('Repita a senha: '):raise SystemExit('Senhas diferentes')
        with s.transaction() as c:
            if s.all(c,'user'):raise SystemExit('Bootstrap já executado. Crie usuários pelo administrador.')
            app.state.security.create_user(c,args.username,password,'admin')
        print('Administrador criado. Configure OTP no primeiro login.')
    else:
        with s.transaction() as c:
            if s.all(c,'entity'):raise SystemExit('Seed permitido somente em ambiente vazio.')
            for n,name in enumerate(['Pessoa Sintética Aurora','Pessoa Sintética Horizonte','Pessoa Sintética Lagoa','Empresa Sintética Prisma']):
                e={'id':uid(),'entity_type':'company' if n==3 else 'person','version':1,'items':[],'observations':[],'created_at':now(),'updated_at':now()}
                for kind,value in [('identity',{'name':name}),('email',{'email':f'exemplo{n}@example.invalid'}),('address',{'country':'BR','city':'Cidade de Teste','street':'Rua Sintética','number':str(n+1),'postal_code':'00000000'})]:
                    v,notes=normalize(kind,value);it={'id':uid(),'kind':kind,'key':item_key(kind,v),'fields':{},'sources':[],'version':1,'notes':notes};e['items'].append(it)
                    for k,x in v.items():add_observation(e,it,'value.'+k,x,'manual',now(),'synthetic-seed',uid(),'Dado sintético de demonstração',now(),value.get(k,x))
                s.put(c,'entity',project(e))
        print('Quatro cadastros explicitamente sintéticos criados.')
if __name__=='__main__':main()

import { chromium, expect as baseExpect } from '@playwright/test';
const expect=baseExpect.configure({timeout:60000});
import { readFileSync, writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
process.chdir(fileURLToPath(new URL('.',import.meta.url)));
const credentials=JSON.parse(readFileSync('../var/browser-test/credentials.json','utf8'));
function totp(secret,at=null){
 return execFileSync('../.venv/bin/python',['-c','import json,sys,time,pyotp;p=json.load(sys.stdin);print(pyotp.TOTP(p["secret"]).at(p["at"] if p["at"] is not None else time.time()))'],{input:JSON.stringify({secret,at}),encoding:'utf8'}).trim();
}
function syntheticAdminGrant(expire=false){
 // This fixture-only helper expires a grant; it never resets replay counters or
 // touches the application's principal var/development.sqlite3 database.
 return JSON.parse(execFileSync('../.venv/bin/python',['-c',`
import json,sqlite3,sys
from pathlib import Path
from datetime import datetime,timedelta,timezone
root=Path('..').resolve()/'var'/'browser-test'
assert root.resolve()==root and not root.is_symlink()
db=root/'development.sqlite3'
assert db.is_file() and not db.is_symlink()
with sqlite3.connect(str(db),timeout=10) as c:
 c.execute('BEGIN IMMEDIATE')
 users=[json.loads(row[0]) for row in c.execute("SELECT body FROM objects WHERE kind='user'")]
 admin=next(u for u in users if u['username']=='browser-test' and u['role']=='admin')
 sessions=[json.loads(row[0]) for row in c.execute("SELECT body FROM objects WHERE kind='session'")]
 owned=[s for s in sessions if s['user_id']==admin['id']]
 assert owned
 if json.load(sys.stdin)['expire']:
  for session in owned:
   session['totp_verified_at']=(datetime.now(timezone.utc)-timedelta(minutes=6)).isoformat()
   c.execute("UPDATE objects SET body=? WHERE kind='session' AND id=?",(json.dumps(session),session['id']))
 print(json.dumps({'last_step':admin['last_otp_step']}))
`],{input:JSON.stringify({expire}),encoding:'utf8'}));
}
async function freshAdminCode(){
 const last=syntheticAdminGrant().last_step;
 const delay=Math.max(0,((last+1)*30-Math.floor(Date.now()/1000)+1)*1000);
 if(delay>35000)throw Error('Relógio do fixture TOTP fora da janela esperada');
 if(delay)await new Promise(resolve=>setTimeout(resolve,delay));
 return totp(credentials.secret);
}
const browser=await chromium.launch({headless:true,args:['--disable-dev-shm-usage']});
const context=await browser.newContext({viewport:{width:1440,height:1050}});
let page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
context.setDefaultTimeout(60000);
const checks=[];const recordCheck=checks.push.bind(checks);checks.push=(...values)=>{for(const value of values)console.log('CHECK: '+value);return recordCheck(...values)};writeFileSync('../var/browser-test/browser-report.json',JSON.stringify({status:'running',checks,at:new Date().toISOString()}));const testName='Pessoa Sintética Navegador '+Date.now();
try{
 await page.goto('http://127.0.0.1:18767/');
 await page.getByLabel('Usuário',{exact:true}).fill(credentials.username);
 await page.getByLabel('Senha',{exact:true}).fill(credentials.password);
 await page.getByRole('button',{name:'Continuar',exact:true}).click();
 await page.getByLabel('Código de seis dígitos').waitFor();
 const code=totp(credentials.secret);
 await page.getByLabel('Código de seis dígitos').fill(code);
 await page.getByRole('button',{name:'Confirmar acesso',exact:true}).click();
 const saved=page.getByRole('button',{name:'Guardei meus códigos'});
 await saved.waitFor({timeout:10000});await saved.click();checks.push('OTP e primeiro acesso completos pelo navegador');
 const fieldName='Contador sintético '+Date.now();
 await page.getByRole('button',{name:'Administração',exact:true}).click();
 await page.getByRole('button',{name:'Campos adicionais',exact:true}).click();
 await page.getByRole('button',{name:'Adicionar',exact:true}).click();
 await page.locator('dialog').getByLabel('Nome',{exact:true}).fill(fieldName);
 await page.locator('dialog').getByLabel('Tipo',{exact:true}).selectOption('integer');
 await page.locator('dialog').getByLabel('Permitir vários valores').uncheck();
 await page.locator('dialog').getByRole('button',{name:'Criar',exact:true}).click();
 await page.getByText(fieldName,{exact:true}).waitFor();
 await page.getByRole('button',{name:'Pessoas',exact:true}).click();
 await page.getByRole('button',{name:'Novo cadastro',exact:true}).click();
 const modal=page.locator('dialog');await modal.getByLabel('Nome',{exact:true}).fill(testName);
 await modal.getByLabel('Motivo / observação').fill('Teste automatizado com dados sintéticos');
 await modal.getByRole('button',{name:'Salvar informação'}).click();
 await page.getByRole('heading',{name:testName,exact:true}).waitFor();checks.push('Cadastro criado pelo formulário');
 await page.getByRole('button',{name:'Agregar informação',exact:true}).click();
 await modal.getByLabel('Tipo de informação').selectOption('custom');
 await modal.getByLabel('Campo cadastrado').selectOption({label:fieldName});
 await modal.getByLabel('Valor do campo',{exact:true}).fill('0');
 await modal.getByRole('button',{name:'Salvar informação'}).click();
 await page.locator('.item-card').filter({hasText:fieldName}).locator('dd').filter({hasText:/^0/}).waitFor();
 checks.push('Campo inteiro criado e zero preservado no cadastro pelo painel');
 await page.getByRole('button',{name:'Agregar informação',exact:true}).click();
 await modal.getByLabel('Tipo de informação').selectOption('custom');
 await modal.getByLabel('Campo cadastrado').selectOption({label:fieldName});
 await modal.getByLabel('Valor do campo',{exact:true}).fill('9007199254740993');
 await modal.getByRole('button',{name:'Salvar informação'}).click();
 await page.locator('.item-card').filter({hasText:fieldName}).locator('dd').filter({hasText:/^9007199254740993/}).waitFor();
 checks.push('Inteiro acima da precisão JavaScript enviado e exibido sem arredondamento');
 await page.getByRole('button',{name:'Agregar informação',exact:true}).click();
 await modal.getByLabel('Tipo de informação').selectOption('email');
 await modal.getByLabel('Email',{exact:true}).fill('teste-navegador@example.invalid');
 await modal.getByRole('button',{name:'Salvar informação'}).click();
 await page.locator('dd').filter({hasText:'teste-navegador@example.invalid'}).first().waitFor();checks.push('Enriquecimento de email com histórico');
 await page.getByRole('button',{name:'Agregar informação',exact:true}).click();
 await modal.getByLabel('Tipo de informação').selectOption('phone');
 await modal.getByLabel('Número',{exact:true}).fill('(11) 8876-5432');
 await modal.getByLabel('País',{exact:true}).fill('BR');
 await modal.getByLabel('Uso',{exact:true}).selectOption('residential');
 await modal.getByRole('button',{name:'Salvar informação'}).click();
 const phoneCard=page.locator('.item-card').filter({hasText:'Como o telefone foi tratado'});
 await phoneCard.locator('dd').filter({hasText:/^\+5511988765432/}).first().waitFor();
 await phoneCard.getByText('Como o telefone foi tratado',{exact:true}).click();
 await phoneCard.getByText('Nono dígito acrescentado com regra histórica',{exact:true}).waitFor();
 await phoneCard.locator('dd').filter({hasText:/^Residencial/}).waitFor();
 if(await phoneCard.locator('select').filter({has:page.locator('option[value="true"]')}).first().inputValue()!=='null')throw Error('Normalização fabricou validade');
 checks.push('Telefone legado tratado com entrada preservada e validade desconhecida no painel');
 await page.getByRole('button',{name:/Histórico \(/}).click();
 await page.getByText('value.email',{exact:true}).first().waitFor();
 await page.getByRole('button',{name:'Fechar ficha',exact:true}).click();
 await page.getByRole('button',{name:'Consulta em massa',exact:true}).click();
 await page.getByLabel('Nome do trabalho').fill('Consulta sintética do navegador');
 await page.getByLabel('Enviar lista CSV ou XLSX').setInputFiles({name:'consulta.csv',mimeType:'text/csv',buffer:Buffer.from(testName+'\nPessoa Não Encontrada\n')});
 await page.getByText(/Lista recebida: 2 entradas/).waitFor();
 await page.getByRole('button',{name:'Preparar consulta',exact:true}).click();
 const download=page.getByRole('link',{name:'Baixar XLSX completo'}).first();await download.waitFor({timeout:20000});
 const [file]=await Promise.all([page.waitForEvent('download'),download.click()]);await file.saveAs('../var/browser-test/browser-result.xlsx');checks.push('Consulta em massa preparada e XLSX baixado');
 await page.getByRole('button',{name:'Pessoas',exact:true}).click();
 await page.getByRole('button',{name:/Filtros/}).first().click();
 await page.getByRole('button',{name:'Adicionar grupo',exact:true}).first().click();
 const group=page.locator('.filter-group').first();
 await group.getByLabel('Combinação dos filtros').selectOption('item');
 await group.getByLabel('Campo',{exact:true}).selectOption('email');
 await group.getByLabel('Valor do filtro').fill('teste-navegador@example.invalid');
 await page.waitForTimeout(700);
 await page.getByText(testName,{exact:true}).first().waitFor();
 checks.push('Grupo de filtros do mesmo item aplicado no painel');
 await page.getByRole('button',{name:'Salvar pesquisa',exact:true}).click();
 await page.getByLabel('Nome da pesquisa').fill('Pesquisa guardada '+testName);
 await page.getByRole('button',{name:'Salvar critérios',exact:true}).click();
 await page.getByRole('button',{name:'Pesquisas salvas',exact:true}).click();
 const savedSearch=page.locator('article').filter({hasText:'Pesquisa guardada '+testName});
 await savedSearch.getByRole('button',{name:'Executar pesquisa',exact:true}).click();
 await page.getByText(testName,{exact:true}).first().waitFor();
 checks.push('Pesquisa salva e executada com os mesmos filtros');
 // A separate synthetic cohort spans two pages and has repeated city values.
 const sortPrefix='Ordenação Sintética '+Date.now();
 await page.evaluate(async prefix=>{
   const auth=await(await fetch('/api/v1/auth/me')).json();
   for(let i=0;i<30;i++){
     const request={method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':auth.csrf,'Idempotency-Key':crypto.randomUUID()},body:JSON.stringify({source_id:'manual',observed_at:new Date().toISOString(),items:[{kind:'identity',value:{name:prefix+' '+String(i).padStart(2,'0')}},{kind:'address',value:{city:['Alfa','Beta','Gama'][i%3],postal_code:'01234567'}}]})};
     let response;
     for(let attempt=0;attempt<8;attempt++){
       response=await fetch('/api/v1/people/enrich',request);
       if(response.status!==429)break;
       await new Promise(resolve=>setTimeout(resolve,Math.max(1,Number(response.headers.get('Retry-After'))||1)*1000));
     }
     if(!response.ok)throw Error('Falha ao preparar coorte sintética de ordenação: '+response.status);
     await new Promise(resolve=>setTimeout(resolve,250));
   }
 },sortPrefix);
 // Remove the previous same-item filter and isolate this cohort.
 await page.getByRole('button',{name:'Remover grupo',exact:true}).first().click();
 await page.getByLabel('Pesquisar por nome').fill(sortPrefix);
 await expect(page.locator('.pagination')).toContainText('1–25 de 30');
 await page.getByRole('button',{name:'Próxima',exact:true}).click();
 await expect(page.locator('.pagination')).toContainText('26–30 de 30');
 await page.locator('tbody input[type=checkbox]').first().check();
 await expect(page.locator('.list-info')).toContainText('1 selecionados');
 await page.getByRole('button',{name:'Adicionar ordenação',exact:true}).click();
 await expect(page.locator('.pagination')).toContainText('1–25 de 30');
 await expect(page.locator('.list-info')).not.toContainText('selecionados');
 await page.getByLabel('Campo de ordenação 2',{exact:true}).selectOption('city');
 await page.getByLabel('Direção de ordenação 2',{exact:true}).selectOption('desc');
 await page.getByLabel('Escolha de valor 2',{exact:true}).selectOption('min');
 await page.getByLabel('Escolha de valor 2',{exact:true}).selectOption('max');
 await page.getByRole('button',{name:'Subir prioridade 2',exact:true}).click();
 await expect(page.getByLabel('Campo de ordenação 1',{exact:true})).toHaveValue('city');
 await page.getByRole('button',{name:'Remover ordenação 2',exact:true}).click();
 await page.getByRole('button',{name:'Adicionar ordenação',exact:true}).click();
 await expect(page.getByLabel('Campo de ordenação 2',{exact:true})).toHaveValue('name');
 const multiSort=[{field:'city',direction:'desc',mode:'max'},{field:'name',direction:'asc'}];
 await page.getByRole('button',{name:'Salvar pesquisa',exact:true}).click();
 await page.getByLabel('Nome da pesquisa').fill(sortPrefix);
 await page.getByRole('button',{name:'Salvar critérios',exact:true}).click();
 await page.getByRole('button',{name:'Pesquisas salvas',exact:true}).click();
 const orderedSearch=page.locator('article').filter({hasText:sortPrefix});
 const reopenedResponse=page.waitForResponse(r=>r.url().endsWith('/people/search')&&JSON.stringify(r.request().postDataJSON()?.sorts)===JSON.stringify(multiSort));
 await orderedSearch.getByRole('button',{name:'Executar pesquisa',exact:true}).click();
 const orderedPage=await(await reopenedResponse).json();
 await expect(page.getByLabel('Campo de ordenação 1',{exact:true})).toHaveValue('city');
 await expect(page.getByLabel('Escolha de valor 1',{exact:true})).toHaveValue('max');
 const completeOrder=await page.evaluate(async ({prefix,sorts})=>{
   const auth=await(await fetch('/api/v1/auth/me')).json();
   return await(await fetch('/api/v1/people/search',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':auth.csrf},body:JSON.stringify({filters:{field:'name',op:'prefix',value:prefix},sorts,limit:100})})).json();
 },{prefix:sortPrefix,sorts:multiSort});
 if(completeOrder.total!==30||JSON.stringify(orderedPage.items.map(e=>e.id))!==JSON.stringify(completeOrder.items.slice(0,25).map(e=>e.id)))throw Error('Ordem da página reaberta diverge da API');
 const names=completeOrder.items.map(e=>e.name);
 const expectedNames=Array.from({length:30},(_,i)=>i).sort((a,b)=>(b%3)-(a%3)||a-b).map(i=>sortPrefix+' '+String(i).padStart(2,'0'));
 if(JSON.stringify(names)!==JSON.stringify(expectedNames))throw Error('Prioridades múltiplas não respeitadas');
 checks.push('Ordenação múltipla: edição, min/max, prioridades, remoção, página/seleção reiniciadas e pesquisa reaberta');
 await page.getByRole('button',{name:'Exportar',exact:true}).click();
 await expect(page.getByLabel('Campo de ordenação 1',{exact:true})).toHaveValue('city');
 await page.getByLabel('Nome do trabalho').fill(sortPrefix);
 const orderedJobResponse=page.waitForResponse(r=>r.url().endsWith('/bulk-queries')&&r.request().method()==='POST');
 await page.getByRole('button',{name:'Preparar consulta',exact:true}).click();
 const orderedJob=await(await orderedJobResponse).json();
 if(JSON.stringify(orderedJob.applied_sort)!==JSON.stringify(completeOrder.applied_sort))throw Error('Contrato de exportação diferente da pesquisa');
 const orderedDownload=page.locator('.job').filter({hasText:sortPrefix}).getByRole('link',{name:'Baixar XLSX completo'});
 await orderedDownload.waitFor({timeout:20000});
 const [orderedFile]=await Promise.all([page.waitForEvent('download'),orderedDownload.click()]);
 await orderedFile.saveAs('../var/browser-test/sorting-result.xlsx');
 execFileSync('../.venv/bin/python',['-c',"import json,sys;from openpyxl import load_workbook;w=load_workbook(sys.argv[1],read_only=True);ids=[r[0] for r in w['Cadastros'].iter_rows(min_row=2,values_only=True)];assert ids==json.loads(sys.argv[2]);assert w['Ordenacao']['D2'].value=='city';assert w['Ordenacao']['E2'].value=='desc';w.close()",'../var/browser-test/sorting-result.xlsx',JSON.stringify(completeOrder.items.map(e=>e.id))]);
 checks.push('Exportação da pesquisa mantém os mesmos 30 IDs em ordem no XLSX baixado');
 // An old saved search must send the legacy contract unchanged until explicitly edited.
 await page.evaluate(async prefix=>{
   const auth=await(await fetch('/api/v1/auth/me')).json();
   const response=await fetch('/api/v1/saved-searches',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':auth.csrf,'Idempotency-Key':crypto.randomUUID()},body:JSON.stringify({name:prefix+' legada',filters:{field:'name',op:'prefix',value:prefix},sort:'name',direction:'desc'})});
   if(!response.ok)throw Error('Falha na pesquisa legada sintética');
 },sortPrefix);
 await page.getByRole('button',{name:'Pesquisas salvas',exact:true}).click();
 const legacyResponse=page.waitForResponse(r=>r.url().endsWith('/people/search')&&r.request().postDataJSON()?.sort==='name');
 await page.locator('article').filter({hasText:sortPrefix+' legada'}).getByRole('button',{name:'Executar pesquisa',exact:true}).click();
 const legacyPage=await(await legacyResponse).json();
 if(legacyPage.applied_sort.contract!=='legacy'||legacyPage.applied_sort.criteria.at(-1).direction!=='desc')throw Error('Desempate legado alterado');
 await page.getByText(/Pesquisa antiga:/).waitFor();
 await page.getByRole('button',{name:'Editar com ordenação múltipla',exact:true}).click();
 await expect(page.getByLabel('Direção de ordenação 1',{exact:true})).toHaveValue('desc');
 checks.push('Pesquisa antiga conserva desempate decrescente e requer edição explícita para novo contrato');

 await page.screenshot({path:'../var/browser-test/panel-desktop.png',fullPage:true});
 await page.evaluate(async()=>{await navigator.serviceWorker.ready});
 await page.reload();await page.getByRole('button',{name:'Pessoas',exact:true}).waitFor();
 const cached=await page.evaluate(async()=>{const keys=await caches.keys();return (await Promise.all(keys.map(async k=>(await(await caches.open(k)).keys()).map(r=>new URL(r.url).pathname)))).flat()});
 if(cached.some(p=>p.startsWith('/api/')))throw Error('Resposta privada no cache offline');
 checks.push('Service worker ativo sem respostas da API no cache');
 await page.setViewportSize({width:390,height:844});
 await page.getByRole('button',{name:'Abrir menu'}).click();await page.getByRole('button',{name:'Empresas',exact:true}).click();
 await page.waitForFunction(()=>document.querySelector('.sidebar').getBoundingClientRect().right<=1);
 const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth+1);if(overflow)throw Error('Layout extrapolou viewport móvel');
 await page.screenshot({path:'../var/browser-test/panel-mobile.png',fullPage:true});checks.push('Navegação móvel sem overflow horizontal da página');
 await page.setViewportSize({width:1440,height:1050});
 await page.getByRole('button',{name:'Administração',exact:true}).click();
 await page.getByRole('button',{name:'Usuários',exact:true}).click();
 syntheticAdminGrant(true);
 const invitedUsername='invited-browser-'+Date.now();
 await page.getByRole('button',{name:'Adicionar',exact:true}).click();
 await page.locator('dialog').getByLabel('Usuário',{exact:true}).fill(invitedUsername);
 await page.locator('dialog').getByRole('button',{name:'Criar',exact:true}).click();
 await page.getByRole('heading',{name:'Confirme sua identidade',exact:true}).waitFor();
 await page.getByRole('button',{name:'Cancelar confirmação',exact:true}).click();
 await expect(page.locator('dialog').getByLabel('Usuário',{exact:true})).toHaveValue(invitedUsername);
 let users=await(await context.request.get('http://127.0.0.1:18767/api/v1/admin/users')).json();
 if(users.items.some(u=>u.username===invitedUsername))throw Error('Cancelar TOTP criou o usuário');
 await page.locator('dialog').getByRole('button',{name:'Criar',exact:true}).click();
 await page.getByLabel('Código atual do autenticador',{exact:true}).fill(code);
 const replayResponse=page.waitForResponse(r=>r.url().endsWith('/auth/step-up')&&r.request().method()==='POST');
 await page.getByRole('button',{name:'Confirmar e continuar',exact:true}).click();
 if((await replayResponse).status()!==401)throw Error('Código já usado foi aceito como nova confirmação');
 await expect(page.getByLabel('Código atual do autenticador',{exact:true})).toHaveValue('');
 await page.locator('dialog [role="alert"]').getByText(/inválido ou já utilizado/).waitFor();
 await page.getByLabel('Código atual do autenticador',{exact:true}).fill(await freshAdminCode());
 await page.getByRole('button',{name:'Confirmar e continuar',exact:true}).click();
 await page.getByLabel('Link de ativação').waitFor();
 const invitation=await page.getByLabel('Link de ativação').inputValue();
 users=await(await context.request.get('http://127.0.0.1:18767/api/v1/admin/users')).json();
 if(users.items.filter(u=>u.username===invitedUsername).length!==1)throw Error('Retomada do convite não criou exatamente um usuário');
 checks.push('TOTP recente: expiração, cancelamento, replay rejeitado e convite retomado uma vez');
 // Retain the administrator session in its context, releasing its renderer before opening another user's page.
 await page.close();
 const invitedContext=await browser.newContext();invitedContext.setDefaultTimeout(60000);const invited=await invitedContext.newPage();
 invited.on('pageerror',e=>errors.push(e.message));
 await invited.goto(invitation);
 await invited.getByRole('heading',{name:'Ative seu acesso',exact:true}).waitFor();
 await invited.getByLabel('Senha',{exact:true}).fill('synthetic-invited-password-123');
 const activated=invited.waitForResponse(r=>r.url().endsWith('/auth/activate')&&r.request().method()==='POST');
 await invited.getByRole('button',{name:'Continuar',exact:true}).click();
 const activation=await(await activated).json();
 const secret=new URL(activation.otp_uri).searchParams.get('secret');
 const invitedCode=totp(secret);
 await invited.getByLabel('Código de seis dígitos').fill(invitedCode);
 await invited.getByRole('button',{name:'Confirmar acesso',exact:true}).click();
 await invited.getByRole('button',{name:'Guardei meus códigos'}).click();
 await invited.getByRole('button',{name:'Pessoas',exact:true}).waitFor();
 if(await invited.getByRole('button',{name:'Administração',exact:true}).count())throw Error('Administração exposta a usuário normal');
 await invited.getByRole('button',{name:'Minhas chaves',exact:true}).click();
 await invited.getByText('Consulte e renove as chaves das suas integrações. A criação inicial é feita por um administrador.',{exact:true}).waitFor();
 if(await invited.locator('tbody tr').count())throw Error('Chaves de terceiros expostas ao usuário normal');
 if(new URL(invited.url()).hash)throw Error('Convite não removido da barra de endereço');
 await invited.close();checks.push('Convite, senha própria, OTP e acesso normal sem administração');
 page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
 await page.goto('http://127.0.0.1:18767/');
 await page.getByRole('button',{name:'Administração',exact:true}).click();
 await page.getByRole('button',{name:'Usuários',exact:true}).click();
 const userRow=page.locator('tr').filter({hasText:invitedUsername});
 await userRow.getByRole('button',{name:'Desativar acesso',exact:true}).click();
 await page.getByRole('button',{name:'Confirmar desativação',exact:true}).click();
 await userRow.getByText('Inativo',{exact:true}).waitFor();
 if((await invitedContext.request.get('http://127.0.0.1:18767/api/v1/auth/me')).status()!==401)throw Error('Desativação manteve sessão do convidado');
 await userRow.getByRole('button',{name:'Ativar acesso',exact:true}).click();
 await page.getByRole('button',{name:'Confirmar ativação',exact:true}).click();
 await userRow.getByText('Ativo',{exact:true}).waitFor();
 if((await invitedContext.request.get('http://127.0.0.1:18767/api/v1/auth/me')).status()!==401)throw Error('Reativação ressuscitou sessão revogada');
 await invitedContext.close();
 checks.push('Administrador desativa e reativa usuário; sessão revogada continua inválida');
 await page.getByRole('button',{name:'Chaves de API',exact:true}).click();
 const keyName='Chave sintética de rotação '+Date.now();
 await page.getByRole('button',{name:'Adicionar',exact:true}).click();
 await page.locator('dialog').getByLabel('Nome',{exact:true}).fill(keyName);
 await page.getByLabel('Novo código do autenticador',{exact:true}).fill(await freshAdminCode());
 const initialKeyResponse=page.waitForResponse(r=>r.url().endsWith('/admin/api-keys')&&r.request().method()==='POST');
 await page.locator('dialog').getByRole('button',{name:'Criar',exact:true}).click();
 const initialKey=await(await initialKeyResponse).json();
 await page.getByLabel('Segredo da nova chave',{exact:true}).waitFor();
 await page.locator('dialog').getByRole('button',{name:'Fechar',exact:true}).click();
 const parentKeyRow=page.locator('tr').filter({has:page.getByText(initialKey.id,{exact:true})});
 await page.getByRole('button',{name:'Chaves de API',exact:true}).click();
 await parentKeyRow.getByRole('button',{name:'Renovar chave',exact:true}).click();
 await page.getByLabel('Transição da chave anterior',{exact:true}).selectOption('900');
 await page.getByLabel('Novo código para renovar a chave',{exact:true}).fill(await freshAdminCode());
 let lostRotation=null;
 const rotationPattern='**/api/v1/admin/api-keys/*/rotate';
 await page.route(rotationPattern,async route=>{
  if(lostRotation===null && route.request().postDataJSON()?.otp){
   const response=await route.fetch();
   if(response.status()!==201){await route.fulfill({response});return}
   lostRotation=await response.json();
   await route.abort('failed');
  }else await route.continue();
 });
 await page.getByRole('button',{name:'Confirmar renovação',exact:true}).click();
 await page.getByRole('button',{name:'Recuperar resultado',exact:true}).waitFor();
 if(!lostRotation)throw Error('Ensaio não confirmou commit da rotação antes de interromper a resposta');
 const recoveredRotationResponse=page.waitForResponse(r=>r.url().endsWith('/'+initialKey.id+'/rotate')&&r.request().method()==='POST');
 await page.getByRole('button',{name:'Recuperar resultado',exact:true}).click();
 const recoveredRotation=await(await recoveredRotationResponse).json();
 if(recoveredRotation.id!==lostRotation.id||recoveredRotation.key!==lostRotation.key)throw Error('Recuperação criou chave diferente');
 await page.getByLabel('Segredo da nova chave',{exact:true}).waitFor();
 if(await page.getByLabel('Segredo da nova chave',{exact:true}).inputValue()!==lostRotation.key)throw Error('Segredo recuperado divergiu da operação original');
 await page.unroute(rotationPattern);
 for(const token of [initialKey.key,lostRotation.key]){
  const usable=await context.request.get('http://127.0.0.1:18767/api/v1/stats',{headers:{'X-API-Key':token}});
  if(usable.status()!==200)throw Error('Chave fora da transição esperada');
 }
 await page.locator('dialog').getByRole('button',{name:'Fechar',exact:true}).click();
 const rotationKeys=await(await context.request.get('http://127.0.0.1:18767/api/v1/admin/api-keys')).json();
 if(rotationKeys.items.filter(k=>k.rotation_root_id===initialKey.id).length!==2)throw Error('Rotação/repetição produziu mais de uma sucessora');
 await parentKeyRow.getByRole('button',{name:'Revogar cadeia',exact:true}).click();
 const revocationResponse=page.waitForResponse(r=>r.url().endsWith('/admin/api-keys/'+initialKey.id)&&r.request().method()==='PATCH');
 await page.getByRole('button',{name:'Confirmar revogação',exact:true}).click();
 const revoked=await revocationResponse;
 if(revoked.status()!==200)throw Error('Revogação da cadeia recusada: HTTP '+revoked.status());
 if(revoked.request().headers()['content-type']?.includes('application/json')&&!revoked.request().postData())throw Error('Revogação enviou cabeçalho JSON com corpo vazio');
 await expect(parentKeyRow.getByRole('button',{name:'Revogar cadeia',exact:true})).toBeDisabled();
 for(const token of [initialKey.key,lostRotation.key]){
  if((await context.request.get('http://127.0.0.1:18767/api/v1/stats',{headers:{'X-API-Key':token}})).status()!==401)throw Error('Revogação não cortou toda a cadeia');
 }
 checks.push('Rotação de chave: novo OTP, resposta perdida recuperada sem duplicação, transição e revogação da cadeia');
 await page.getByRole('button',{name:'Campos adicionais',exact:true}).click();
 await page.locator('tr').filter({hasText:fieldName}).getByRole('button',{name:'Editar campo'}).click();
 await page.getByLabel('Nome do campo').fill(fieldName+' revisado');
 await page.getByLabel('Campo ativo para novos dados').uncheck();
 await page.getByRole('button',{name:'Salvar definição'}).click();
 await page.locator('tr').filter({hasText:fieldName+' revisado'}).getByText('Inativo',{exact:true}).waitFor();
 checks.push('Definição editada e desativada com controle de versão');
 const importName='Importação sintética '+Date.now();
 const importedName='Pessoa Sintética Importada '+Date.now();
 const externalId='browser-import-'+Date.now();
 await page.getByRole('button',{name:'Importações',exact:true}).click();
 await page.getByLabel('Nome da importação',{exact:true}).fill(importName);
 const importEntries=[
  {entity_type:'person',source_id:'manual',external_id:externalId,items:[{kind:'identity',value:{name:importedName}}]},
  {entity_type:'person',source_id:'manual',items:[]},
  {entity_type:'person',source_id:'manual',external_id:externalId,items:[{kind:'email',value:{email:'importado@example.invalid'},flags:{valid:false}}]}
 ];
 await page.getByLabel('Arquivo JSON ou JSONL',{exact:true}).setInputFiles({name:'cadastros.jsonl',mimeType:'application/x-ndjson',buffer:Buffer.from(importEntries.map(x=>JSON.stringify(x)).join('\n'))});
 await page.getByRole('button',{name:'Iniciar importação',exact:true}).click();
 const importJob=page.locator('article').filter({hasText:importName});
 await importJob.getByText('Concluída com erros',{exact:true}).waitFor({timeout:20000});
 await importJob.getByRole('button',{name:'Ver resultados',exact:true}).click();
 const importResults=page.getByRole('region',{name:'Resultados das entradas da importação'});
 await expect(importResults.getByText('Incorporado',{exact:true})).toHaveCount(2);
 const incorporatedRows=importResults.locator('tbody tr').filter({hasText:'Incorporado'});
 if(await incorporatedRows.first().locator('td').nth(2).innerText()!==await incorporatedRows.last().locator('td').nth(2).innerText())throw Error('Importação não agregou pelo identificador de origem');
 await importResults.getByText('Falhou',{exact:true}).waitFor();
 await page.screenshot({path:'../var/browser-test/imports-desktop.png',fullPage:true});
 await page.setViewportSize({width:390,height:844});
 await page.waitForFunction(()=>document.querySelector('.sidebar').getBoundingClientRect().right<=1);
 if(await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth+1))throw Error('Importações extrapolaram viewport móvel');
 await page.screenshot({path:'../var/browser-test/imports-mobile.png',fullPage:true});
 await page.setViewportSize({width:1440,height:1050});
 checks.push('Importação JSONL com erro isolado, enriquecimento de cadastro e resultados paginados no painel responsivo');
 // Canonical data is prepared only in this run's private PostgreSQL schema.
 const canonicalFixture=JSON.parse(readFileSync('../var/browser-test/canonical-fixture.json','utf8'));
 await page.getByRole('button',{name:'Consulta canônica',exact:true}).click();
 await page.getByLabel('Origem canônica',{exact:true}).fill(canonicalFixture.source_id);
 await page.getByLabel('ID na origem',{exact:true}).fill('person-1');
 await page.getByRole('button',{name:'Abrir ficha canônica',exact:true}).click();
 const canonical=page.getByRole('region',{name:'Ficha canônica',exact:true});
 await expect(canonical.getByText(canonicalFixture.person_id,{exact:true})).toBeVisible();
 await expect(canonical.getByRole('region',{name:'Itens canônicos'}).locator('article')).toHaveCount(20);
 await canonical.getByRole('button',{name:'Próximos itens',exact:true}).click();
 await expect(canonical.getByRole('region',{name:'Itens canônicos'}).locator('article')).toHaveCount(5);
 await canonical.getByRole('button',{name:'Histórico do item',exact:true}).first().click();
 await expect(canonical.getByRole('region',{name:'Histórico canônico'}).locator('article')).not.toHaveCount(0);
 await canonical.getByRole('button',{name:'Histórico da ficha',exact:true}).click();
 const canonicalHistory=canonical.getByRole('region',{name:'Histórico canônico'});
 await expect(canonicalHistory.locator('article')).toHaveCount(20);
 await expect(canonicalHistory.getByText('12345678901234567890.12345678901234567890',{exact:true})).toHaveCount(2);
 const historyValues=await canonicalHistory.locator('dd').allTextContents();
 for(const value of ['null','false','0'])if(!historyValues.includes(value))throw Error('Valor canônico perdeu triestado/zero: '+value);
 await canonicalHistory.getByRole('button',{name:'Próximas observações',exact:true}).click();
 await expect(canonicalHistory.locator('article')).toHaveCount(7);
 await canonical.getByRole('button',{name:'Campos da ficha',exact:true}).click();
 await expect(canonical.getByRole('region',{name:'Campos canônicos'}).locator('article')).toHaveCount(20);
 await page.setViewportSize({width:390,height:844});
 if(await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth+1))throw Error('Ficha canônica extrapolou viewport móvel');
 await page.screenshot({path:'../var/browser-test/canonical-mobile.png',fullPage:true});
 await page.setViewportSize({width:1440,height:1050});
 await page.getByLabel('Tipo de cadastro',{exact:true}).selectOption('companies');
 await expect(page.getByRole('region',{name:'Ficha canônica',exact:true})).toHaveCount(0);
 await page.getByLabel('ID na origem',{exact:true}).fill('company-1');
 await page.getByRole('button',{name:'Abrir ficha canônica',exact:true}).click();
 await expect(page.getByText(canonicalFixture.company_id,{exact:true})).toBeVisible();
 await page.route('**/api/v1/canonical/companies/*/fields',route=>route.fulfill({status:401,contentType:'application/json',body:JSON.stringify({detail:'Sessão sintética indisponível'})}));
 await page.getByRole('button',{name:'Campos da ficha',exact:true}).click();
 await expect(page.getByRole('alert').filter({hasText:'Sessão sintética indisponível'})).toBeVisible();
 await expect(page.getByRole('region',{name:'Ficha canônica',exact:true})).toHaveCount(0);
 await page.unroute('**/api/v1/canonical/companies/*/fields');
 await page.getByLabel('ID na origem',{exact:true}).fill('missing-synthetic');
 await page.getByRole('button',{name:'Abrir ficha canônica',exact:true}).click();
 await expect(page.getByRole('alert').filter({hasText:'Cadastro canônico não encontrado.'})).toBeVisible();
 await expect(page.getByRole('region',{name:'Ficha canônica',exact:true})).toHaveCount(0);
 checks.push('Leitura canônica PostgreSQL pelo painel: pessoa/empresa, corte paginado, origem/histórico, precisão/null/false/zero, responsividade e limpeza ao trocar contexto');
 // A panel build must remain usable until an older local API process is restarted.
 const catalogResponse=await context.request.get('http://127.0.0.1:18767/api/v1/search/catalog');
 if(!catalogResponse.ok())throw Error('Catálogo de preparação indisponível');
 const legacyCatalog=await catalogResponse.json();delete legacyCatalog.sorting;legacyCatalog.version=1;
 await page.route('**/api/v1/search/catalog',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(legacyCatalog)}).catch(error=>errors.push('Interceptação sintética do catálogo: '+error.message)));
 const fallbackResponse=page.waitForResponse(r=>r.url().endsWith('/people/search')&&r.request().postDataJSON()?.sort==='name');
 await page.reload();
 const fallbackRequest=(await fallbackResponse).request().postDataJSON();
 if('sorts' in fallbackRequest||fallbackRequest.direction!=='asc')throw Error('Painel enviou contrato novo ao catálogo legado');
 await page.getByText('Ordenação múltipla indisponível no servidor conectado.',{exact:true}).waitFor();
 await expect(page.getByRole('button',{name:'Editar com ordenação múltipla',exact:true})).toBeDisabled();
 await page.unrouteAll({behavior:'wait'});
 checks.push('Painel permanece utilizável com catálogo antigo até reinício administrativo, sem enviar sorts ignorável');
 const logoutResponse=page.waitForResponse(r=>r.url().endsWith('/auth/logout')&&r.request().method()==='POST');
 await page.getByTitle('Sair',{exact:true}).click();
 const loggedOut=await logoutResponse;
 if(loggedOut.status()!==200)throw Error('Logout recusado: HTTP '+loggedOut.status());
 if(loggedOut.request().headers()['content-type']?.includes('application/json')&&!loggedOut.request().postData())throw Error('Logout enviou cabeçalho JSON com corpo vazio');
 await page.getByLabel('Usuário',{exact:true}).waitFor();
 await page.getByLabel('Senha',{exact:true}).waitFor();
 if((await context.request.get('http://127.0.0.1:18767/api/v1/auth/me')).status()!==401)throw Error('Logout manteve sessão utilizável');
 checks.push('POST de logout sem corpo encerra a sessão e devolve o painel ao login');
 if(errors.length)throw Error(errors.join('\n'));
 writeFileSync('../var/browser-test/browser-report.json',JSON.stringify({status:'passed',checks,at:new Date().toISOString()},null,2));console.log(JSON.stringify({status:'passed',checks}));
}catch(error){writeFileSync('../var/browser-test/browser-report.json',JSON.stringify({status:'failed',checks,at:new Date().toISOString(),error:String(error)},null,2));console.error(error);process.exitCode=1;if(!page.isClosed()){try{await page.screenshot({path:'../var/browser-test/failure.png',fullPage:true,timeout:10000})}catch{console.error('Captura de falha indisponível; o erro original está no relatório.')}}}finally{await browser.close()}

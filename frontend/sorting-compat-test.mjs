import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import ts from 'typescript';

const directory=path.dirname(fileURLToPath(import.meta.url));
const temporary=await mkdtemp(path.join(directory,'.sorting-test-'));
try {
  const source=await readFile(path.join(directory,'src/SortEditor.tsx'),'utf8');
  const compiled=ts.transpileModule(source,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ESNext,jsx:ts.JsxEmit.React},reportDiagnostics:true});
  assert.equal(compiled.diagnostics?.length??0,0);
  const output=path.join(temporary,'SortEditor.js');await writeFile(output,compiled.outputText);
  const {initialSort,sortForServer}=await import(pathToFileURL(output).href);
  const checks=[];
  assert.deepEqual(sortForServer(initialSort(),false),{sort:'name',direction:'asc'});checks.push('Padrão inicial funciona no servidor legado');
  const legacy={sort:'name',direction:'desc'};
  assert.equal(sortForServer(legacy,false),legacy);checks.push('Pesquisa legada preserva direção e contrato');
  const multiple={sorts:[{field:'city',direction:'desc',mode:'max'},{field:'name',direction:'asc'}]};
  assert.equal(sortForServer(multiple,true),multiple);checks.push('Servidor novo recebe todas as prioridades');
  assert.throws(()=>sortForServer(multiple,false),/critérios foram preservados/);checks.push('Servidor antigo não reduz ordenação múltipla silenciosamente');
  assert.throws(()=>sortForServer({sorts:[{field:'name',direction:'desc'}]},false),/critérios foram preservados/);checks.push('Ordenação nova decrescente não troca desempate por legado');
  assert.throws(()=>sortForServer({sorts:[{field:'name',direction:'asc',mode:'max'}]},false),/critérios foram preservados/);checks.push('Modo explícito não é descartado no servidor antigo');
  console.log(JSON.stringify({status:'passed',checks},null,2));
} finally {await rm(temporary,{recursive:true,force:true});}

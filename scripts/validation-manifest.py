"""Registra hashes do código e evidências reais, sem ler credenciais ou cadastros."""
import hashlib
import json
from pathlib import Path
from datetime import datetime,timezone
import xml.etree.ElementTree as ET

root=Path(__file__).resolve().parents[1]
paths=[]
for folder in ['backend/bigbase','backend/tests','frontend/src','frontend/public','scripts','infra']:
    paths.extend(p for p in (root/folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts)
paths.extend(root/p for p in ['frontend/browser-test.mjs','frontend/precision-test.mjs','frontend/imports-input-test.mjs','pyproject.toml','requirements.lock','frontend/package.json','frontend/package-lock.json','frontend/tsconfig.json','frontend/vite.config.ts'])
hashes={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}
code_hash=hashlib.sha256(json.dumps(hashes,sort_keys=True,separators=(',',':')).encode()).hexdigest()
xml=ET.parse(root/'var/validation/backend.xml')
suites=list(xml.getroot().iter('testsuite'))
backend={key:sum(int(s.get(key,'0')) for s in suites) for key in ['tests','failures','errors','skipped']}
browser=json.loads((root/'var/browser-test/browser-report.json').read_text())
precision=json.loads((root/'var/validation/precision.json').read_text())
imports_panel=json.loads((root/'var/validation/imports-panel.json').read_text())
if not backend['tests'] or any(backend[k] for k in ['failures','errors','skipped']):
    raise SystemExit('A rodada de backend precisa passar integralmente, sem testes ignorados.')
if browser.get('status')!='passed' or precision.get('status')!='passed' or imports_panel.get('status')!='passed':
    raise SystemExit('A rodada de navegador e precisão precisa estar aprovada.')
report={'at':datetime.now(timezone.utc).isoformat(),'environment':'isolated-synthetic-development','code_sha256':code_hash,'backend':backend,'browser':browser,'precision':precision,'imports_panel':imports_panel,'frontend_assets':{str(p.relative_to(root/'frontend/dist')):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((root/'frontend/dist').rglob('*')) if p.is_file()},'production_migration_executed':False,'production_load_validated':False,'source_files':hashes}
output=root/'docs/EVIDENCIAS-2026-09-08.json';output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'code_sha256':code_hash,'backend':backend,'browser':browser['status'],'precision':precision['status'],'report':str(output)},ensure_ascii=False))

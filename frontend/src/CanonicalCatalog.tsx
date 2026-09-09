import React, { useEffect, useRef, useState } from 'react';

type API = (path: string, method?: string, body?: any, headers?: Record<string, string>) => Promise<any>;

export function CanonicalCatalog({ api, canAdmin }: { api: API; canAdmin: boolean }) {
  const [items, setItems] = useState<any[]>([]), [after, setAfter] = useState<string | null>(null);
  const [selected, setSelected] = useState<any>(null), [history, setHistory] = useState<any[]>([]);
  const [historyAfter, setHistoryAfter] = useState<number | null>(null);
  const [name, setName] = useState(''), [id, setId] = useState(''), [type, setType] = useState('text');
  const [scope, setScope] = useState('both'), [multiple, setMultiple] = useState(true), [options, setOptions] = useState('');
  const [active, setActive] = useState(true), [busy, setBusy] = useState(false), [error, setError] = useState('');
  const [result, setResult] = useState(''), [otp, setOtp] = useState(''), [needsOTP, setNeedsOTP] = useState(false);
  const pending = useRef<{ signature: string; key: string } | null>(null);
  const retry = useRef<(() => Promise<void>) | null>(null);
  const live = useRef(true);
  useEffect(() => { live.current = true; return () => { live.current = false; retry.current = null; }; }, []);
  async function list(cursor = '') {
    const response = await api('/canonical/fields?limit=50&after=' + encodeURIComponent(cursor));
    if (live.current) { setItems(response.items); setAfter(response.next_after); }
  }
  async function run(action: () => Promise<void>) {
    setBusy(true); setError('');
    try { await action(); }
    catch (e: any) { if (live.current) setError(e.message); }
    finally { if (live.current) setBusy(false); }
  }
  useEffect(() => { run(() => list()); }, []);
  function choose(field: any) {
    setSelected(field); setName(field?.name || ''); setActive(field?.active ?? true);
    setHistory([]); setHistoryAfter(null); setResult(''); setError(''); pending.current = null;
  }
  async function showHistory(cursor = 0) {
    const response = await api('/canonical/fields/' + encodeURIComponent(selected.id) + '/history?limit=20&after=' + cursor);
    if (live.current) { setHistory(old => cursor ? [...old, ...response.items] : response.items); setHistoryAfter(response.next_after); }
  }
  async function save(event: React.FormEvent) {
    event.preventDefault(); setResult('');
    const path = '/canonical/fields' + (selected ? '/' + encodeURIComponent(selected.id) : '');
    const body = selected ? { name, active } : { ...(id ? { id } : {}), name, type, scope, multiple, options: type === 'enum' ? options.split('\n') : [] };
    const headers: Record<string, string> = selected ? { 'If-Match': String(selected.version) } : {};
    const signature = JSON.stringify([path, body, headers]);
    if (pending.current?.signature !== signature) pending.current = { signature, key: crypto.randomUUID() };
    headers['Idempotency-Key'] = pending.current!.key;
    const action = async () => {
      try {
        const receipt = await api(path, selected ? 'PATCH' : 'POST', body, headers);
        if (!live.current) return;
        setSelected(receipt.definition); setName(receipt.definition.name); setActive(receipt.definition.active);
        setHistory([]); setHistoryAfter(null);
        setResult('Definição registrada · versão ' + receipt.definition.version + (receipt.replayed ? ' · repetição confirmada' : '') + ' · comprovante ' + receipt.operation_id);
        retry.current = null; setNeedsOTP(false); await list();
      } catch (e: any) {
        if (e.code === 'RECENT_TOTP_REQUIRED' && live.current) { retry.current = action; setNeedsOTP(true); }
        else throw e;
      }
    };
    await run(action);
  }
  async function confirm(event: React.FormEvent) {
    event.preventDefault(); const code = otp; setOtp('');
    await run(async () => { await api('/auth/step-up', 'POST', { code }); if (live.current && retry.current) await retry.current(); });
  }
  return <section aria-label="Catálogo PostgreSQL sintético" className="card">
    <h3>Definições de campos no PostgreSQL sintético</h3>
    <p>Catálogo separado do SQLite local. Versões anteriores e observações permanecem preservadas. Indexação pendente.</p>
    <fieldset disabled={busy || needsOTP}>
      <button type="button" onClick={() => run(() => list())}>Recarregar catálogo PostgreSQL</button>{' '}
      {after && <button type="button" onClick={() => run(() => list(after))}>Próxima página de definições</button>}
      <ul>{items.map(field => <li key={field.id}><button type="button" onClick={() => choose(field)}>{field.name} · {field.type} · v{field.version} · {field.active ? 'ativo' : 'inativo'}</button></li>)}</ul>
      {canAdmin && <button type="button" onClick={() => choose(null)}>Nova definição PostgreSQL</button>}
    </fieldset>
    {canAdmin && !needsOTP && <form onSubmit={save}>
      <fieldset disabled={busy}>
        <legend>{selected ? 'Editar definição PostgreSQL · versão ' + selected.version : 'Criar definição PostgreSQL'}</legend>
        <label>Nome da definição PostgreSQL<input required maxLength={160} value={name} onChange={e => setName(e.target.value)} /></label>
        {!selected && <>
          <label>ID da definição PostgreSQL<input maxLength={160} pattern="[A-Za-z0-9][A-Za-z0-9_.:-]*" value={id} onChange={e => setId(e.target.value)} /></label>
          <label>Tipo da definição PostgreSQL<select value={type} onChange={e => setType(e.target.value)}>{['text','integer','decimal','boolean','date','enum','url','reference'].map(t => <option key={t}>{t}</option>)}</select></label>
          <label>Escopo da definição PostgreSQL<select value={scope} onChange={e => setScope(e.target.value)}><option value="both">Pessoa e empresa</option><option value="person">Pessoa</option><option value="company">Empresa</option></select></label>
          <label><input type="checkbox" checked={multiple} onChange={e => setMultiple(e.target.checked)} />Múltiplos valores previstos</label>
          {type === 'enum' && <label>Opções, uma por linha<textarea required value={options} onChange={e => setOptions(e.target.value)} /></label>}
        </>}
        {selected && <><p>{selected.id} · {selected.type} · {selected.scope}. Mudanças incompatíveis exigem outra definição e migração explícita.</p><label><input type="checkbox" checked={active} onChange={e => setActive(e.target.checked)} />Definição PostgreSQL ativa</label></>}
        <button className="primary" type="submit">Salvar definição PostgreSQL</button>
      </fieldset>
    </form>}
    {needsOTP && <form onSubmit={confirm}><p>Confirme um OTP recente para registrar a alteração administrativa.</p><label>OTP do catálogo<input required inputMode="numeric" autoComplete="one-time-code" pattern="[0-9]{6}" maxLength={6} value={otp} onChange={e => setOtp(e.target.value)} disabled={busy} /></label><button disabled={busy}>Confirmar OTP e salvar definição</button><button type="button" disabled={busy} onClick={() => { retry.current = null; setNeedsOTP(false); setOtp(''); }}>Cancelar confirmação</button></form>}
    {selected && <button type="button" disabled={busy || needsOTP} onClick={() => run(() => showHistory())}>Histórico da definição PostgreSQL</button>}
    {history.map(row => <article key={row.definition.version}><h4>Definição · versão {row.definition.version}</h4><p>{row.definition.name} · {row.definition.active ? 'ativo' : 'inativo'} · {row.definition.type}</p><p>SHA256: <code style={{ overflowWrap: 'anywhere' }}>{row.definition_sha256}</code></p></article>)}
    {historyAfter !== null && <button disabled={busy} onClick={() => run(() => showHistory(historyAfter))}>Mais versões da definição</button>}
    {busy && <p role="status">Consultando catálogo…</p>}{result && <p role="status" style={{ overflowWrap: 'anywhere' }}>{result}</p>}{error && <p role="alert">{error}</p>}
  </section>;
}

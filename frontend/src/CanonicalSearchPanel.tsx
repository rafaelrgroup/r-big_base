import React, { useEffect, useRef, useState } from 'react';
import { displayPreciseValue, parseApiJson, PreciseNumber } from './precision';

type API = (path: string, method?: string, body?: any, headers?: Record<string, string>) => Promise<any>;
type Row = { id: string; kind: string; field: string; op: string; value: string; type: string; valid: string; whatsapp: string };
const kinds: Record<string, string> = { identity: 'Identificação', document: 'Documento', phone: 'Telefone', email: 'Email', address: 'Endereço', username: 'Username', relationship: 'Vínculo', activity: 'Atividade', custom: 'Campo adicional' };
const names: Record<string, string> = { name: 'Nome', legal_name: 'Razão social', trade_name: 'Nome fantasia', birth_date: 'Nascimento', sex: 'Sexo', number: 'Número', type: 'Tipo', country: 'País', state: 'Estado', city: 'Cidade', neighborhood: 'Bairro', street: 'Rua', postal_code: 'CEP', domain: 'Domínio', platform: 'Plataforma', username: 'Username', role: 'Função', target_name: 'Nome relacionado', email: 'Email' };
const initialRow = (): Row => ({ id: crypto.randomUUID(), kind: 'identity', field: 'name', op: 'contains', value: '', type: 'text', valid: '', whatsapp: '' });
const flag = (value: string) => value === 'null' ? null : value === 'true';

export function CanonicalSearchPanel({ api, collection, enabled, fields, coverage, onOpen }: {
  api: API; collection: string; enabled: boolean; fields: Record<string, string[]>; coverage?: string;
  onOpen: (id: string) => Promise<void>;
}) {
  const [rows, setRows] = useState<Row[]>([initialRow()]);
  const [mode, setMode] = useState('all');
  const [includePending, setIncludePending] = useState(false), [includeInvalid, setIncludeInvalid] = useState(false);
  const [result, setResult] = useState<any>(null), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const revision = useRef(0), mounted = useRef(true), releaseCursor = useRef<string | null>(null);
  function release(token: string | null = releaseCursor.current) {
    if (token === releaseCursor.current) releaseCursor.current = null;
    if (token) void api('/canonical-search/close', 'POST', { release_cursor: token }).catch(() => {});
  }
  function reset() { revision.current++; release(); setResult(null); setError(''); setBusy(false); }
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; revision.current++; release(); };
  }, [api]);
  useEffect(() => { if (!enabled) reset(); }, [enabled]);
  function change(id: string, patch: Partial<Row>) { reset(); setRows(current => current.map(row => row.id === id ? { ...row, ...patch } : row)); }
  function criteria() {
    const nodes = rows.map(row => {
      let value: any = row.value;
      if (row.type === 'number') {
        value = parseApiJson(row.value);
        if (typeof value !== 'number' && !(value instanceof PreciseNumber)) throw new Error('Informe um número JSON válido.');
      } else if (row.type === 'boolean') value = row.value === 'true';
      else if (row.type === 'null') value = null;
      if (row.type === 'text' && !row.value.trim()) throw new Error('Preencha o valor de cada filtro.');
      const flags: Record<string, boolean | null> = {};
      if (row.valid !== '') flags.valid = flag(row.valid);
      if (row.whatsapp !== '') flags.is_whatsapp = flag(row.whatsapp);
      return { kind: row.kind, field: row.field, op: row.type === 'text' ? row.op : 'eq', value, ...(Object.keys(flags).length ? { flags } : {}) };
    });
    if (mode === 'same_item') {
      if (nodes.some(node => node.kind !== nodes[0].kind)) throw new Error('Filtros do mesmo item precisam usar o mesmo tipo de informação.');
      return { same_item: { kind: nodes[0].kind, conditions: nodes.map(({ kind, ...node }) => node) } };
    }
    return nodes.length === 1 ? nodes[0] : { [mode]: nodes };
  }
  async function search(cursor?: string) {
    if (!enabled || busy) return;
    if (!cursor) reset();
    const own = ++revision.current; setBusy(true); setError('');
    try {
      const response = await api('/canonical/' + collection + '/search', 'POST', {
        filters: criteria(), page_size: 20, sort: [{ field: 'id', direction: 'asc' }],
        include_pending: includePending, include_invalid: includeInvalid, ...(cursor ? { cursor } : {}),
      });
      if (!mounted.current || own !== revision.current) { release(response.release_cursor); return; }
      releaseCursor.current = response.release_cursor; setResult(response);
    } catch (e: any) {
      if (mounted.current && own === revision.current) { setError(e.message || 'Não foi possível pesquisar.'); setResult(null); release(); }
    } finally { if (mounted.current && own === revision.current) setBusy(false); }
  }
  if (!enabled) return <section className="panel" aria-label="Pesquisa canônica"><h3>Pesquisar cadastros</h3><p role="status">A pesquisa por filtros aguarda a validação do índice. A localização exata continua disponível abaixo.</p></section>;
  return <section className="panel" aria-label="Pesquisa canônica">
    <h3>Pesquisar cadastros</h3>
    {coverage === 'pilot_10000' && <p className="info">A pesquisa cobre os cadastros já indexados do piloto. A migração completa ainda está em andamento.</p>}
    {coverage === 'canonical_snapshot' && <p className="info">A pesquisa cobre os cadastros já indexados da base unificada.</p>}
    <form onSubmit={event => { event.preventDefault(); void search(); }}>
      <label>Combinar filtros<select aria-label="Combinar filtros" value={mode} onChange={event => { reset(); setMode(event.target.value); }}>
        <option value="all">Todos os critérios</option><option value="any">Qualquer critério</option><option value="same_item">Mesmo telefone, endereço ou item</option>
      </select></label>
      {rows.map((row, index) => <fieldset key={row.id} className="form-grid"><legend>Filtro {index + 1}</legend>
        <label>Informação<select aria-label="Informação" value={row.kind} onChange={event => change(row.id, { kind: event.target.value, field: fields[event.target.value]?.[0] || '', whatsapp: '' })}>
          {Object.keys(fields).filter(kind => kind !== 'custom').map(kind => <option key={kind} value={kind}>{kinds[kind] || kind}</option>)}
        </select></label>
        <label>Campo<select aria-label="Campo" value={row.field} onChange={event => change(row.id, { field: event.target.value })}>{(fields[row.kind] || []).map(field => <option key={field} value={field}>{names[field] || field.replaceAll('_', ' ')}</option>)}</select></label>
        <label>Tipo do valor<select aria-label="Tipo do valor" value={row.type} onChange={event => change(row.id, { type: event.target.value, value: event.target.value === 'boolean' ? 'true' : '' })}>
          <option value="text">Texto</option><option value="number">Número exato</option><option value="boolean">Sim ou não</option><option value="null">Sem valor confirmado</option>
        </select></label>
        {row.type === 'text' && <label>Correspondência<select aria-label="Correspondência" value={row.op} onChange={event => change(row.id, { op: event.target.value })}>
          <option value="contains">Contém</option><option value="eq">Exatamente igual</option><option value="ieq">Igual, ignorando acentos</option><option value="prefix">Começa com</option><option value="match">Todas as palavras</option>
        </select></label>}
        {row.type === 'boolean' ? <label>Valor<select aria-label="Valor" value={row.value} onChange={event => change(row.id, { value: event.target.value })}><option value="true">Sim</option><option value="false">Não</option></select></label>
          : row.type !== 'null' && <label>Valor<input aria-label="Valor" required maxLength={8192} value={row.value} onChange={event => change(row.id, { value: event.target.value })} placeholder={row.field === 'email' ? 'exemplo@example.invalid' : undefined} /></label>}
        <label>Validade<select aria-label="Validade" value={row.valid} onChange={event => change(row.id, { valid: event.target.value })}><option value="">Qualquer</option><option value="true">Confirmado válido</option><option value="false">Confirmado inválido</option><option value="null">Não confirmado</option></select></label>
        {row.kind === 'phone' && <label>WhatsApp<select aria-label="WhatsApp" value={row.whatsapp} onChange={event => change(row.id, { whatsapp: event.target.value })}><option value="">Qualquer</option><option value="true">Sim</option><option value="false">Não</option><option value="null">Não confirmado</option></select></label>}
        {rows.length > 1 && <button type="button" onClick={() => { reset(); setRows(current => current.filter(item => item.id !== row.id)); }}>Remover filtro {index + 1}</button>}
      </fieldset>)}
      <div className="toolbar"><button type="button" disabled={rows.length >= 20} onClick={() => { reset(); setRows(current => [...current, initialRow()]); }}>Adicionar filtro</button>
        <label className="check"><input type="checkbox" checked={includePending} onChange={event => { reset(); setIncludePending(event.target.checked); }} />Incluir informações pendentes</label>
        <label className="check"><input type="checkbox" checked={includeInvalid} onChange={event => { reset(); setIncludeInvalid(event.target.checked); }} />Incluir informações invalidadas</label>
        <button className="primary" type="submit" disabled={busy}>Pesquisar cadastros</button>
      </div>
    </form>
    {busy && <p role="status">Consultando…</p>}{error && <p role="alert" className="danger">{error}</p>}
    {result && <section aria-label="Resultados da pesquisa canônica">
      <p>{result.total.relation === 'gte' ? 'Pelo menos ' : ''}{displayPreciseValue(result.total.value)} resultados. Ordem estável por código.</p>
      {!result.items.length && <p>Nenhum cadastro corresponde aos filtros.</p>}
      {result.items.map((hit: any) => <article key={hit.id} className="card"><p className="canonical-id">{hit.id}</p><p>Versão atual {displayPreciseValue(hit.record_version)}{hit.indexing_pending && ' · Atualização da pesquisa pendente'}</p><button disabled={busy} onClick={() => void onOpen(hit.id)}>Abrir ficha deste resultado</button></article>)}
      {result.has_more && <button disabled={busy} onClick={() => void search(result.next_cursor)}>Próxima página de resultados</button>}
      <button disabled={busy} onClick={reset}>Encerrar pesquisa</button>
    </section>}
  </section>;
}

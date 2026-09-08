import React, { useEffect, useRef, useState } from 'react';
import { displayPreciseValue } from './precision';

type Dict = Record<string, any>;
type API = (path: string, method?: string, body?: any) => Promise<any>;
const show = (value: any) => value === undefined ? 'Ausente' : displayPreciseValue(value);
const date = (value: any) => value ? new Date(value).toLocaleString('pt-BR') : 'Data não informada';

export function CanonicalPanel({ api }: { api: API }) {
  const [status, setStatus] = useState<Dict | null>(null);
  const [collection, setCollection] = useState('people');
  const [mode, setMode] = useState('source');
  const [source, setSource] = useState(''), [external, setExternal] = useState('');
  const [id, setId] = useState(''), [country, setCountry] = useState('BR');
  const [documentType, setDocumentType] = useState('CPF'), [document, setDocument] = useState('');
  const [entity, setEntity] = useState<Dict | null>(null), [items, setItems] = useState<Dict | null>(null);
  const [detail, setDetail] = useState<Dict | null>(null);
  const [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const generation = useRef(0);
  useEffect(() => {
    let live = true;
    api('/canonical/status').then(value => { if (live) setStatus(value); }).catch(e => { if (live) setError(e.message); });
    return () => { live = false; generation.current++; };
  }, [api]);
  function clear() { generation.current++; setEntity(null); setItems(null); setDetail(null); setError(''); setBusy(false); }
  async function open(event: React.FormEvent) {
    event.preventDefault(); clear(); const own = generation.current; setBusy(true);
    try {
      const base = '/canonical/' + collection;
      const meta = mode === 'id' ? await api(base + '/' + encodeURIComponent(id)) : await api(base + '/lookup', 'POST',
        mode === 'source' ? { source_id: source, source_record_id: external } : { country, document_type: documentType, value: document });
      const page = await api(base + '/' + meta.id + '/items', 'POST', { limit: 20, cursor: meta.items.cursor });
      if (own === generation.current) { setEntity(meta); setItems(page); }
    } catch (e: any) { if (own === generation.current) { setEntity(null); setItems(null); setDetail(null); setError(e.message); } }
    finally { if (own === generation.current) setBusy(false); }
  }
  async function load(kind: string, cursor: string, itemId?: string) {
    if (!entity) return;
    const own = ++generation.current; setBusy(true); setError(''); setDetail(null);
    if (kind === 'items') setItems(null);
    try {
      const page = await api('/canonical/' + collection + '/' + entity.id + '/' + kind, 'POST',
        { limit: 20, cursor, ...(itemId ? { item_id: itemId } : {}) });
      if (own === generation.current) {
        if (kind === 'items') setItems(page); else setDetail({ kind, itemId, page });
      }
    } catch (e: any) { if (own === generation.current) { setEntity(null); setItems(null); setDetail(null); setError(e.message); } }
    finally { if (own === generation.current) setBusy(false); }
  }
  return <section className="card canonical-panel" aria-label="Consulta canônica">
    <h2>Consulta canônica de ensaio</h2>
    <p>Dados sintéticos em PostgreSQL. A ficha apresenta as coleções por páginas no mesmo corte de versão. A autenticação permanece local, de processo único.</p>
    {error && <p role="alert">{error}</p>}
    {!status && !error && <p role="status">Conferindo disponibilidade…</p>}
    {status && !status.enabled && <p role="status">Leitura canônica sintética não configurada neste serviço.</p>}
    {status?.enabled && <>
      <form onSubmit={open}>
        <div className="form-grid">
          <label>Tipo de cadastro<select aria-label="Tipo de cadastro" value={collection} onChange={e => { clear(); setCollection(e.target.value); }}><option value="people">Pessoa</option><option value="companies">Empresa</option></select></label>
          <label>Localizar por<select aria-label="Localizar por" value={mode} onChange={e => { clear(); setMode(e.target.value); }}><option value="source">Origem e ID externo</option><option value="document">Documento exato</option><option value="id">ID canônico</option></select></label>
          {mode === 'source' && <><label>Origem canônica<input required maxLength={2048} value={source} onChange={e => { clear(); setSource(e.target.value); }} /></label><label>ID na origem<input required maxLength={2048} value={external} onChange={e => { clear(); setExternal(e.target.value); }} /></label></>}
          {mode === 'id' && <label>ID canônico<input required value={id} onChange={e => { clear(); setId(e.target.value); }} /></label>}
          {mode === 'document' && <><label>País do documento<input required value={country} onChange={e => { clear(); setCountry(e.target.value); }} /></label><label>Tipo do documento<input required value={documentType} onChange={e => { clear(); setDocumentType(e.target.value); }} /></label><label>Valor exato do documento<input required value={document} onChange={e => { clear(); setDocument(e.target.value); }} /></label></>}
        </div>
        <button className="primary" disabled={busy} type="submit">Abrir ficha canônica</button>
      </form>
      {busy && <p role="status">Carregando página…</p>}
      {entity && <section aria-label="Ficha canônica">
        <h3>{entity.entity_type === 'person' ? 'Pessoa' : 'Empresa'} · versão {show(entity.version)}</h3>
        <p className="canonical-id">{entity.id}</p>
        <button disabled={busy} onClick={() => load('fields', entity.fields.cursor)}>Campos da ficha</button>{' '}
        <button disabled={busy} onClick={() => load('history', entity.history.cursor)}>Histórico da ficha</button>
        {items && <section aria-label="Itens canônicos">
          <p>Corte da ficha: versão {show(items.snapshot.entity_version)}</p>
          {!items.items.length && <p>Nenhum item neste corte.</p>}
          {items.items.map((item: Dict) => <article key={item.id}>
            <h4>{item.kind} · {show(item.item_key)}</h4>
            <p>Versão {show(item.version)} · Recebido em {date(item.created_at)} · Atualizado em {date(item.updated_at)}</p>
            <button disabled={busy} onClick={() => load('fields', item.fields.cursor, item.id)}>Campos do item</button>{' '}
            <button disabled={busy} onClick={() => load('history', item.history.cursor, item.id)}>Histórico do item</button>
          </article>)}
          {items.has_more && <button disabled={busy} onClick={() => load('items', items.next_cursor)}>Próximos itens</button>}
        </section>}
        {detail && <section aria-label={detail.kind === 'fields' ? 'Campos canônicos' : 'Histórico canônico'}>
          <h3>{detail.kind === 'fields' ? 'Campos e confirmações' : 'Histórico de observações'}</h3>
          <p>Corte da ficha: versão {show(detail.page.snapshot.entity_version)}</p>
          {!detail.page.items.length && <p>Nenhuma observação neste corte.</p>}
          {detail.page.items.map((row: Dict) => <article key={row.observation_id}>
            <h4>{show(row.field_path)} · {row.dimension}</h4>
            <dl>
              <dt>Valor {detail.kind === 'fields' ? 'aplicável' : 'normalizado'}</dt><dd>{show(detail.kind === 'fields' ? row.value : row.normalized_value)}</dd>
              <dt>Entrada original</dt><dd>{show(row.input_value)}</dd>
              <dt>Origem / caminho original</dt><dd>{show(row.source_id)} / {show(row.source_path)}</dd>
              <dt>Estado / aplicada</dt><dd>{row.status} / {show(row.applied)}</dd>
              {detail.kind === 'fields' && <><dt>Confirmação associada ao valor atual</dt><dd>{show(row.applicable)}</dd></>}
              <dt>Data da fonte</dt><dd>{date(row.source_updated_at)}</dd>
              <dt>Observada em</dt><dd>{date(row.observed_at)}</dd>
              <dt>Recebida em</dt><dd>{date(row.received_at)}</dd>
              <dt>Ator / operação</dt><dd>{show(row.actor_id)} / {row.operation_id}</dd>
              <dt>Metadados e evidências</dt><dd>{show(row.metadata)}</dd>
            </dl>
          </article>)}
          {detail.page.has_more && <button disabled={busy} onClick={() => load(detail.kind, detail.page.next_cursor, detail.itemId)}>Próximas observações</button>}
        </section>}
      </section>}
    </>}
  </section>;
}

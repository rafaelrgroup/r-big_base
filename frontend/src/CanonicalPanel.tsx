import React, { useEffect, useRef, useState } from 'react';
import { displayPreciseValue } from './precision';
import { CanonicalEnrichment } from './CanonicalEnrichment';
import { CanonicalValidation } from './CanonicalValidation';
import { CanonicalScalarEdit } from './CanonicalScalarEdit';
import { CanonicalCatalog } from './CanonicalCatalog';
import { CanonicalSearchPanel } from './CanonicalSearchPanel';

type Dict = Record<string, any>;
type API = (path: string, method?: string, body?: any, headers?: Record<string, string>) => Promise<any>;
const show = (value: any) => value === undefined ? 'Ausente' : displayPreciseValue(value);
const date = (value: any) => value ? new Date(value).toLocaleString('pt-BR') : 'Data não informada';

export function CanonicalPanel({ api, deployed = false }: { api: API; deployed?: boolean }) {
  const [status, setStatus] = useState<Dict | null>(null);
  const [collection, setCollection] = useState('people');
  const [mode, setMode] = useState('source');
  const [source, setSource] = useState(''), [external, setExternal] = useState('');
  const [id, setId] = useState(''), [country, setCountry] = useState('BR');
  const [documentType, setDocumentType] = useState('CPF'), [document, setDocument] = useState('');
  const [entity, setEntity] = useState<Dict | null>(null), [items, setItems] = useState<Dict | null>(null);
  const [detail, setDetail] = useState<Dict | null>(null);
  const [scalarEdit, setScalarEdit] = useState<Dict | null>(null);
  const [validation, setValidation] = useState<Dict | null>(null);
  const [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const generation = useRef(0);
  useEffect(() => {
    let live = true;
    const refresh = () => api('/canonical/status').then(value => { if (live) setStatus(value); }).catch(e => {
      if (live) { setError(e.message); setStatus(current => current ? { ...current, search_enabled: false } : null); }
    });
    void refresh(); const timer = setInterval(refresh, 30000);
    return () => { live = false; clearInterval(timer); generation.current++; };
  }, [api]);
  function clear() { generation.current++; setEntity(null); setItems(null); setDetail(null); setValidation(null); setScalarEdit(null); setError(''); setBusy(false); }
  async function open(event: React.FormEvent) {
    event.preventDefault(); clear(); const own = generation.current; setBusy(true);
    try {
      const base = '/canonical/' + collection;
      const meta = mode === 'id' ? await api(base + '/' + encodeURIComponent(id)) : await api(base + '/lookup', 'POST',
        mode === 'source' ? { source_id: source, source_record_id: external } : { country, document_type: documentType, value: document });
      const page = await api(base + '/' + meta.id + '/items', 'POST', { limit: 20, cursor: meta.items.cursor });
      if (own === generation.current) { setEntity(meta); setItems(page); }
    } catch (e: any) { if (own === generation.current) { setEntity(null); setItems(null); setDetail(null); setValidation(null); setScalarEdit(null); setError(e.message); } }
    finally { if (own === generation.current) setBusy(false); }
  }
  async function load(kind: string, cursor: string, itemId?: string) {
    if (!entity) return;
    const own = ++generation.current; setBusy(true); setError(''); setDetail(null); setValidation(null); setScalarEdit(null);
    if (kind === 'items') setItems(null);
    try {
      const page = await api('/canonical/' + collection + '/' + entity.id + '/' + kind, 'POST',
        { limit: 20, cursor, ...(itemId ? { item_id: itemId } : {}) });
      if (own === generation.current) {
        if (kind === 'items') setItems(page); else setDetail({ kind, itemId, page });
      }
    } catch (e: any) { if (own === generation.current) { setEntity(null); setItems(null); setDetail(null); setValidation(null); setScalarEdit(null); setError(e.message); } }
    finally { if (own === generation.current) setBusy(false); }
  }
  return <section className="card canonical-panel" aria-label="Consulta canônica">
    <h2>{deployed ? 'Consulta da base unificada' : 'Consulta canônica de ensaio'}</h2>
    <p>{deployed ? 'Informações e histórico preservados. Todas as páginas de uma consulta mantêm a mesma versão da ficha.' : 'Dados sintéticos em PostgreSQL. A ficha apresenta as coleções por páginas no mesmo corte de versão. A autenticação permanece local, de processo único.'}</p>
    {error && <p role="alert">{error}</p>}
    {!status && !error && <p role="status">Conferindo disponibilidade…</p>}
    {status && !status.enabled && <p role="status">{deployed ? 'A consulta canônica ainda não está disponível neste serviço.' : 'Leitura canônica sintética não configurada neste serviço.'}</p>}
    {status?.enabled && <CanonicalCatalog api={api} deployed={deployed} canAdmin={Boolean(status.writes_enabled && status.can_administer_catalog)} />}
    {status?.enabled && <>
      <CanonicalSearchPanel key={collection} api={api} collection={collection} enabled={status.search_enabled === true}
        fields={status.search_fields || {}} coverage={status.search_coverage}
        onOpen={async owner => {
          clear(); const own = generation.current; setBusy(true);
          try {
            const base = '/canonical/' + collection + '/' + encodeURIComponent(owner);
            const meta = await api(base);
            const page = await api(base + '/items', 'POST', { limit: 20, cursor: meta.items.cursor });
            if (own === generation.current) { setEntity(meta); setItems(page); }
          } catch (e: any) { if (own === generation.current) setError(e.message); }
          finally { if (own === generation.current) setBusy(false); }
        }} />
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
      {status.writes_enabled && status.can_enrich && <CanonicalEnrichment key={collection + ':' + (entity?.id || 'new')} api={api} deployed={deployed} collection={collection} canValidate={status.can_validate}
        entity={entity} source={source} external={external} onWritten={async (owner, writeSource, writeExternal) => {
          const own = ++generation.current;
          setSource(writeSource); setExternal(writeExternal);
          const meta = await api('/canonical/' + collection + '/' + owner);
          const page = await api('/canonical/' + collection + '/' + owner + '/items', 'POST', { limit: 20, cursor: meta.items.cursor });
          if (own === generation.current) { setEntity(meta); setItems(page); setDetail(null); setValidation(null); setScalarEdit(null); }
        }} />}
      {entity && validation && status.writes_enabled && status.can_validate && <CanonicalValidation
        key={collection + ':' + validation.observation_id} api={api} collection={collection} entity={entity} row={validation}
        onCancel={() => setValidation(null)} onWritten={async () => {
          const own = ++generation.current;
          const base = '/canonical/' + collection + '/' + entity.id;
          const meta = await api(base);
          const [page, fields] = await Promise.all([
            api(base + '/items', 'POST', { limit: 20, cursor: meta.items.cursor }),
            api(base + '/fields', 'POST', { limit: 20, cursor: meta.fields.cursor }),
          ]);
          if (own === generation.current) { setEntity(meta); setItems(page); setDetail({ kind: 'fields', page: fields }); setValidation(null); setScalarEdit(null); }
        }} />}
      {entity && scalarEdit && status.writes_enabled && status.can_enrich && <CanonicalScalarEdit
        key={collection + ':' + scalarEdit.observation_id} api={api} collection={collection} entity={entity} row={scalarEdit}
        onCancel={() => setScalarEdit(null)} onWritten={async () => {
          const own = ++generation.current;
          const base = '/canonical/' + collection + '/' + entity.id;
          const meta = await api(base);
          const [page, fields] = await Promise.all([
            api(base + '/items', 'POST', { limit: 20, cursor: meta.items.cursor }),
            api(base + '/fields', 'POST', { limit: 20, cursor: meta.fields.cursor }),
          ]);
          if (own === generation.current) { setEntity(meta); setItems(page); setDetail({ kind: 'fields', page: fields }); setValidation(null); setScalarEdit(null); }
        }} />}
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
              {row.metadata?.custom_field && <>
                <dt>Definição do campo adicional</dt><dd>{show(row.metadata.field_id)} · versão {show(row.metadata.field_definition_version)}</dd>
                <dt>Classificação / indexação canônica</dt><dd>{row.metadata.classification_state === 'defined' ? 'Definido' : 'Pendente de classificação'} / pendente</dd>
                <dt>Definição preservada</dt><dd>{show(row.metadata.field_definition)}</dd>
              </>}
              {row.metadata?.phone_normalization && <>
                <dt>Normalização do telefone</dt><dd>{({ historical_conversion: 'Conversão histórica', canonical: 'Formato canônico', review: 'Revisão necessária' } as Record<string, string>)[row.metadata.phone_normalization.decision]}</dd>
                <dt>Regra / versão</dt><dd>{show(row.metadata.phone_normalization.rule_id)} / {show(row.metadata.normalization)}</dd>
                <dt>Classificação técnica</dt><dd>{show(row.metadata.phone_output?.classification)}</dd>
                <dt>Contexto e resultado da normalização</dt><dd>{show(row.metadata.phone_output)}</dd>
                <dt>Motivo / observações da normalização</dt><dd>{show(row.metadata.phone_normalization.reason)} / {show(row.metadata.normalization_notes)}</dd>
              </>}
              {row.metadata?.username_normalization && <>
                <dt>Normalização de username</dt><dd>{row.metadata.username_normalization.decision === 'platform_normalized' ? 'Plataforma padronizada' : 'Username preservado literalmente'}</dd>
                <dt>Regra / versão de username</dt><dd>{show(row.metadata.username_normalization.rule_id)} / {show(row.metadata.normalization)}</dd>
                <dt>Plataforma nesta operação</dt><dd>{show(row.metadata.username_normalization.platform)}</dd>
              </>}
              {row.metadata?.postal_normalization && <>
                <dt>Normalização postal</dt><dd>{row.metadata.postal_normalization.decision === 'review' ? 'Revisão postal necessária' : 'CEP formatado'}</dd>
                <dt>Regra / versão postal</dt><dd>{show(row.metadata.postal_normalization.rule_id)} / {show(row.metadata.normalization)}</dd>
                <dt>País informado nesta operação</dt><dd>{show(row.metadata.postal_normalization.country)}</dd>
                <dt>Candidato postal</dt><dd>{show(row.metadata.postal_normalization.candidate_value)}</dd>
                <dt>Formato postal reconhecido</dt><dd>{show(row.metadata.postal_normalization.syntax_valid)} — não confirma existência ou residência</dd>
              </>}
              {row.metadata?.email_normalization && <>
                <dt>Normalização do email</dt><dd>{row.metadata.email_normalization.decision === 'review' ? 'Revisão necessária' : 'Domínio normalizado'}</dd>
                <dt>Regra / versão do email</dt><dd>{show(row.metadata.email_normalization.rule_id)} / {show(row.metadata.normalization)}</dd>
                <dt>Parte local preservada</dt><dd>{show(row.metadata.email_normalization.input_local_part)}</dd>
                <dt>Domínio recebido / normalizado</dt><dd>{show(row.metadata.email_normalization.input_domain)} / {show(row.metadata.email_normalization.output_domain)}</dd>
                <dt>Formato básico reconhecido</dt><dd>{show(row.metadata.email_normalization.syntax_valid)} — não confirma entregabilidade ou titularidade</dd>
              </>}
              <dt>Origem / caminho original</dt><dd>{show(row.source_id)} / {show(row.source_path)}</dd>
              <dt>Estado / aplicada</dt><dd>{row.status} / {show(row.applied)}</dd>
              {detail.kind === 'fields' && <><dt>Confirmação associada ao valor atual</dt><dd>{show(row.applicable)}</dd></>}
              {detail.kind === 'fields' && row.dimension.startsWith('flag:') && <>
                <dt>Atualidade da confirmação</dt><dd>{row.stale === null ? 'Vencimento pendente de classificação' : row.stale ? 'Confirmação desatualizada' : row.expires_at ? 'Dentro do prazo informado' : 'Sem vencimento informado'}</dd>
                <dt>Verificada em</dt><dd>{date(row.checked_at)}</dd><dt>Vence em</dt><dd>{date(row.expires_at)}</dd>
              </>}
              <dt>Data da fonte</dt><dd>{date(row.source_updated_at)}</dd>
              <dt>Observada em</dt><dd>{date(row.observed_at)}</dd>
              <dt>Recebida em</dt><dd>{date(row.received_at)}</dd>
              <dt>Ator / operação</dt><dd>{show(row.actor_id)} / {row.operation_id}</dd>
              <dt>Metadados e evidências</dt><dd>{show(row.metadata)}</dd>
            </dl>
            {status.writes_enabled && status.can_enrich && detail.kind === 'fields' && row.dimension === 'value' &&
              !['empty_object', 'empty_array'].includes(row.input_type) &&
              <button disabled={busy} onClick={() => { setValidation(null); setScalarEdit(row); }}>Editar este valor</button>}
            {status.writes_enabled && status.can_validate && row.dimension === 'value' &&
              !['empty_object', 'empty_array'].includes(row.input_type) &&
              <button disabled={busy} onClick={() => { setScalarEdit(null); setValidation(row); }}>Validar este valor</button>}
          </article>)}
          {detail.page.has_more && <button disabled={busy} onClick={() => load(detail.kind, detail.page.next_cursor, detail.itemId)}>Próximas observações</button>}
        </section>}
      </section>}
    </>}
  </section>;
}

import React, { useEffect, useRef, useState } from 'react';
import { parseApiJson, serializeApiJson, PreciseNumber } from './precision';

type API = (path: string, method?: string, body?: any, headers?: Record<string, string>) => Promise<any>;
type Entry = { kind: string; key: string; path: string; type: string; value: string; observed: string; flag: string; confirmation: string; normalization: string; country: string; ddd: string; extension: string; platform: string; fieldId: string; fieldVersion: string; catalogEnvironment: string };
const empty = (): Entry => ({ kind: 'identity', key: 'identity', path: 'name', type: 'text', value: '', observed: '', flag: '', confirmation: 'null', normalization: 'literal', country: '', ddd: '', extension: '', platform: '', fieldId: '', fieldVersion: '', catalogEnvironment: 'local' });

export function CanonicalEnrichment({ api, collection, entity, source, external, onWritten, canValidate, deployed = false }: {
  api: API; collection: string; entity: any; source: string; external: string; onWritten: (id: string, source: string, external: string) => Promise<void>; canValidate: boolean; deployed?: boolean;
}) {
  const [definitions, setDefinitions] = useState<any[]>([]);
  const [pgDefinitions, setPgDefinitions] = useState<any[]>([]);
  const [pgAfter, setPgAfter] = useState<string | null>(null);
  async function reloadPG(cursor = '') {
    try { const response = await api('/canonical/fields?limit=100&after=' + encodeURIComponent(cursor)); if (live.current) { setPgDefinitions(old => cursor ? [...old, ...response.items] : response.items); setPgAfter(response.next_after); setCatalogError(''); } }
    catch { if (live.current) setCatalogError('Catálogo PostgreSQL indisponível. Nenhuma definição local o substitui.'); }
  }
  const [catalogError, setCatalogError] = useState('');
  async function reloadCatalog() {
    try { const response = await api('/admin/fields'); if (live.current) { setDefinitions(response.items); setCatalogError(''); } }
    catch { if (live.current) setCatalogError('Catálogo indisponível. Recarregue antes de vincular uma definição.'); }
  }
  useEffect(() => { if (deployed) reloadPG(); else reloadCatalog(); }, []);
  const newEntry = () => ({ ...empty(), catalogEnvironment: deployed ? 'postgresql' : 'local' });
  const [entries, setEntries] = useState<Entry[]>([newEntry()]);
  const [origin, setOrigin] = useState(source || 'manual'), [reference, setReference] = useState(external);
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [result, setResult] = useState('');
  const pending = useRef<{ signature: string; key: string } | null>(null);
  const live = useRef(true);
  useEffect(() => { live.current = true; return () => { live.current = false; }; }, []);
  function edit(index: number, key: keyof Entry, value: string) {
    setEntries(old => old.map((entry, i) => i === index ? { ...entry, [key]: value } : entry)); setResult('');
  }
  async function submit(event: React.FormEvent) {
    event.preventDefault(); setBusy(true); setError(''); setResult('');
    try {
      const grouped = new Map<string, any>();
      for (const row of entries) {
        const value = row.type === 'text' ? row.value : row.type === 'null' ? null : row.type === 'boolean' ? row.value === 'true' : parseApiJson(row.value);
        if (row.type === 'number' && typeof value !== 'number' && !(value instanceof PreciseNumber))
          throw new Error('Informe um número JSON válido.');
        if (row.type === 'json' && (!value || typeof value !== 'object' || value instanceof PreciseNumber))
          throw new Error('Informe um objeto ou lista JSON.');
        const group = row.kind + ':' + row.key;
        const item = grouped.get(group) || { kind: row.kind, key: row.key, fields: [] };
        item.fields.push({ path: row.path, value, ...(row.observed ? { observed_at: row.observed } : {}),
          ...(row.flag ? { flags: { [row.flag]: { value: row.confirmation === 'null' ? null : row.confirmation === 'true',
            ...(row.observed ? { observed_at: row.observed } : {}) } } } : {}) });
        if (row.kind === 'phone' && row.path === 'number' && row.normalization === 'phone') {
          if (row.type !== 'text') throw new Error('A normalização de telefone exige número textual.');
          item.phone_normalization = { contract: 'canonical-phone-2026-09-09.1' };
          for (const name of ['country', 'ddd', 'extension'] as const) {
            if (row[name]) item.fields.push({ path: name, value: row[name] });
          }
        }
        if (row.kind === 'email' && row.path === 'email' && row.normalization === 'email') {
          if (row.type !== 'text') throw new Error('A normalização de email exige valor textual.');
          item.email_normalization = { contract: 'canonical-email-2026-09-09.1' };
        }
        if (row.kind === 'address' && row.path === 'postal_code' && row.normalization === 'postal') {
          if (row.type !== 'text') throw new Error('A normalização postal exige valor textual.');
          item.postal_normalization = { contract: 'canonical-postal-2026-09-09.1' };
          if (row.country) item.fields.push({ path: 'country', value: row.country });
        }
        if (row.kind === 'username' && row.path === 'username' && row.normalization === 'username') {
          if (row.type !== 'text') throw new Error('A normalização de username exige valor textual.');
          item.username_normalization = { contract: 'canonical-username-2026-09-09.1' };
          if (row.platform) item.fields.push({ path: 'platform', value: row.platform });
        }
        if (row.kind === 'custom' && row.normalization === 'custom') {
          if (row.path !== 'value') throw new Error('Campo adicional vinculado exige o caminho value.');
          const binding = { contract: row.catalogEnvironment === 'postgresql' ? 'canonical-postgresql-field-2026-09-09.1' : 'canonical-custom-field-2026-09-09.1', field_id: row.fieldId, version: row.fieldVersion === '' ? null : Number(row.fieldVersion) };
          if (item.custom_field && serializeApiJson(item.custom_field) !== serializeApiJson(binding)) throw new Error('Use referências distintas para campos adicionais distintos.');
          item.custom_field = binding;
        }
        grouped.set(group, item);
      }
      const body = { source_id: origin, source_record_id: reference, expected_version: entity?.version || 0,
        ...(entity ? { entity_id: entity.id } : {}), items: [...grouped.values()] };
      const signature = collection + serializeApiJson(body);
      if (pending.current?.signature !== signature) pending.current = { signature, key: crypto.randomUUID() };
      const receipt = await api('/canonical/' + collection + '/enrich', 'POST', body, { 'Idempotency-Key': pending.current!.key });
      if (!live.current) return;
      setResult(`Operação registrada na versão ${receipt.record_version}${receipt.replayed ? ' (repetição reconhecida)' : ''}.`);
      try { await onWritten(receipt.id, origin, reference); }
      catch { setError('Escrita registrada. A leitura está indisponível; reabra a ficha.'); }
    } catch (e: any) { setError(e.message); }
    finally { setBusy(false); }
  }
  return <section aria-label="Enriquecimento canônico">
    <h3>{entity ? 'Acrescentar observações à ficha' : deployed ? 'Criar cadastro' : 'Criar cadastro sintético'}</h3>
    <p>{entity ? `A escrita exige a versão ${entity.version} da ficha aberta.` : 'A identidade de origem deve ser nova. Para alterar um cadastro, abra a ficha primeiro.'}{!deployed && ' Use somente dados fictícios.'}</p>
    <form onSubmit={submit}>
      <fieldset disabled={busy}>
        <div className="form-grid">
          <label>Origem da escrita<input required maxLength={120} value={origin} onChange={e => setOrigin(e.target.value)} /></label>
          <label>Referência do cadastro na origem<input required maxLength={2048} value={reference} onChange={e => setReference(e.target.value)} /></label>
        </div>
        {entries.map((row, i) => <fieldset key={i} aria-label={`Observação ${i + 1}`}>
          <legend>Observação {i + 1}</legend><div className="form-grid">
            <label>Grupo<select aria-label="Grupo" value={row.kind} onChange={e => edit(i, 'kind', e.target.value)}>{[['identity','Identificação'],['phone','Telefone'],['email','Email'],['address','Endereço'],['username','Username'],['custom','Campo adicional'],['activity','Atividade']].map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
            <label>Referência do item<input required maxLength={500} value={row.key} onChange={e => edit(i, 'key', e.target.value)} /></label>
            <label>Campo<input required maxLength={500} value={row.path} onChange={e => edit(i, 'path', e.target.value)} /></label>
            <label>Tipo do valor<select aria-label="Tipo do valor" value={row.type} onChange={e => { edit(i, 'type', e.target.value); edit(i, 'value', e.target.value === 'boolean' ? 'false' : ''); }}><option value="text">Texto</option><option value="number">Número exato</option><option value="boolean">Booleano</option><option value="null">Desconhecido (null)</option><option value="json">Objeto ou lista JSON</option></select></label>
            {row.type === 'boolean' ? <label>Valor<select aria-label="Valor" value={row.value} onChange={e => edit(i, 'value', e.target.value)}><option value="false">false</option><option value="true">true</option></select></label> : row.type !== 'null' && <label>Valor<textarea aria-label="Valor" value={row.value} onChange={e => edit(i, 'value', e.target.value)} /></label>}
            <label>Observado em, com fuso horário<input placeholder="2026-01-01T12:00:00Z" value={row.observed} onChange={e => edit(i, 'observed', e.target.value)} /></label>
            {row.kind === 'phone' && row.path === 'number' && <>
              <label>Normalização do telefone<select aria-label="Normalização do telefone" value={row.normalization} onChange={e => edit(i, 'normalization', e.target.value)}><option value="literal">Preservar literalmente</option><option value="phone">Normalizar com histórico</option></select></label>
              {row.normalization === 'phone' && <>
                <label>País do telefone<input placeholder="BR" maxLength={2} value={row.country} onChange={e => edit(i, 'country', e.target.value)} /></label>
                <label>DDD do telefone<input maxLength={2} value={row.ddd} onChange={e => edit(i, 'ddd', e.target.value)} /></label>
                <label>Ramal do telefone<input maxLength={10} value={row.extension} onChange={e => edit(i, 'extension', e.target.value)} /></label>
              </>}
            </>}
            {row.kind === 'email' && row.path === 'email' && <label>Normalização do email<select aria-label="Normalização do email" value={row.normalization} onChange={e => edit(i, 'normalization', e.target.value)}><option value="literal">Preservar literalmente</option><option value="email">Normalizar domínio com histórico</option></select></label>}
            {row.kind === 'address' && row.path === 'postal_code' && <>
              <label>Normalização postal<select aria-label="Normalização postal" value={row.normalization} onChange={e => edit(i, 'normalization', e.target.value)}><option value="literal">Preservar literalmente</option><option value="postal">Normalizar CEP com país explícito</option></select></label>
              {row.normalization === 'postal' && <label>País do código postal<input placeholder="BR, GB…" maxLength={2} pattern="[A-Z]{2}" value={row.country} onChange={e => edit(i, 'country', e.target.value)} /></label>}
            </>}
            {row.kind === 'username' && row.path === 'username' && <>
              <label>Normalização de username<select aria-label="Normalização de username" value={row.normalization} onChange={e => edit(i, 'normalization', e.target.value)}><option value="literal">Preservar literalmente</option><option value="username">Padronizar plataforma com histórico</option></select></label>
              {row.normalization === 'username' && <label>Plataforma da conta<input maxLength={2048} placeholder="Instagram, Telegram…" value={row.platform} onChange={e => edit(i, 'platform', e.target.value)} /></label>}
            </>}
            {row.kind === 'custom' && <>
              <label>Catálogo da definição<select aria-label="Catálogo da definição" value={row.catalogEnvironment} onChange={e => { edit(i, 'catalogEnvironment', e.target.value); edit(i, 'normalization', 'literal'); edit(i, 'fieldId', ''); edit(i, 'fieldVersion', ''); if (e.target.value === 'postgresql') reloadPG(); }}>{!deployed && <option value="local">SQLite local — contrato anterior</option>}<option value="postgresql">{deployed ? 'Catálogo da base unificada' : 'PostgreSQL sintético — transacional'}</option></select></label>
              <label>Definição do campo<select aria-label="Definição do campo" value={row.normalization === 'custom' ? row.fieldId || '__unknown' : ''} onChange={e => {
                const selected = (row.catalogEnvironment === 'postgresql' ? pgDefinitions : definitions).find(d => d.id === e.target.value);
                edit(i, 'normalization', e.target.value ? 'custom' : 'literal');
                edit(i, 'fieldId', selected?.id || ''); edit(i, 'fieldVersion', selected ? String(selected.version) : '');
                if (e.target.value) edit(i, 'path', 'value');
              }}>
                <option value="">Preservar campos livres</option><option value="__unknown">Identificador ainda não cadastrado</option>
                {(row.catalogEnvironment === 'postgresql' ? pgDefinitions : definitions).map(d => <option key={d.id} value={d.id}>{d.name} · {d.type} · versão {d.version}{d.active ? '' : ' · inativo'} · {d.scope}</option>)}
              </select></label>
              {row.normalization === 'custom' && <>
                <label>Identificador da definição<input required maxLength={160} value={row.fieldId} onChange={e => { edit(i, 'fieldId', e.target.value); edit(i, 'fieldVersion', ''); }} /></label>
                <label>Versão da definição<input type="number" min="1" step="1" value={row.fieldVersion} onChange={e => edit(i, 'fieldVersion', e.target.value)} /></label>
                <p>Versão vazia somente para identificador desconhecido. Cada item recebe um valor; use referências diferentes para alternativas. O tipo é validado sem conversão. A definição acompanha o histórico; a indexação canônica está pendente.</p>
              </>}
              <button type="button" onClick={() => row.catalogEnvironment === 'postgresql' ? reloadPG() : reloadCatalog()}>Recarregar definições</button>
              {row.catalogEnvironment === 'postgresql' && pgAfter && <button type="button" onClick={() => reloadPG(pgAfter)}>Carregar mais definições PostgreSQL</button>}
              {catalogError && <p role="alert">{catalogError}</p>}
            </>}
            {canValidate && <label>Confirmação<select aria-label="Confirmação" value={row.flag} onChange={e => edit(i, 'flag', e.target.value)}><option value="">Sem nova confirmação</option><option value="valid">Validade</option><option value="is_whatsapp">WhatsApp</option><option value="ownership_confirmed">Titularidade</option><option value="deliverable">Entregabilidade</option><option value="residence_confirmed">Residência</option></select></label>}
            {row.flag && <label>Resultado da confirmação<select aria-label="Resultado da confirmação" value={row.confirmation} onChange={e => edit(i, 'confirmation', e.target.value)}><option value="null">Desconhecido</option><option value="false">Não</option><option value="true">Sim</option></select></label>}
          </div>{row.kind === 'email' && row.path === 'email' && row.normalization === 'email' && <p>A parte antes do @ é preservada, inclusive maiúsculas, pontos e +. O domínio passa para minúsculas. A entrada e a regra ficam no histórico; o formato não confirma entregabilidade ou titularidade. Confirmações enviadas aqui referem-se ao email original.</p>}{row.kind === 'phone' && row.path === 'number' && row.normalization === 'phone' && <p>País, DDD e ramal preenchidos são observações sem data conhecida. Deixe vazios os componentes que não quer enviar. Para informar datas próprias, use observações separadas no mesmo item. A entrada e a regra ficam no histórico; normalizar não confirma linha ativa, WhatsApp ou titularidade. Confirmações enviadas aqui referem-se ao número original.</p>}
          {row.kind === 'address' && row.path === 'postal_code' && row.normalization === 'postal' && <p>Somente BR explícito permite formatar CEP de oito dígitos. Sem país ou para outros países, o código permanece literal para revisão. País preenchido é uma observação sem data conhecida; para datas próprias, use outra observação no mesmo item. O formato não confirma endereço existente ou residência. Confirmações referem-se ao código original.</p>}
          {row.kind === 'username' && row.path === 'username' && row.normalization === 'username' && <p>A plataforma é obrigatória e passa para minúsculas; username, @, URL e ID externo permanecem literais. Use referências distintas para contas diferentes, inclusive em outra plataforma. A plataforma fica vinculada ao item. O controle auxiliar não atribui data: para datas próprias, envie outra observação no mesmo item. Nenhuma titularidade ou relação com telefone é confirmada.</p>}
          {entries.length > 1 && <button type="button" onClick={() => setEntries(old => old.filter((_, index) => index !== i))}>Remover observação {i + 1}</button>}
        </fieldset>)}
        <p>A mesma referência e grupo identificam um item; referências distintas mantêm múltiplos contatos. A confirmação refere-se ao valor enviado. Datas vazias permanecem desconhecidas.</p>
        <button type="button" disabled={entries.length >= 100} onClick={() => setEntries(old => [...old, newEntry()])}>Adicionar observação</button>{' '}
        <button className="primary" type="submit">Registrar observações canônicas</button>
      </fieldset>
    </form>
    {busy && <p role="status">Registrando observações…</p>}
    {result && <p role="status">{result}</p>}
    {error && <p role="alert">{error}</p>}
  </section>;
}

import React, { useEffect, useRef, useState } from 'react';
import { displayPreciseValue, serializeApiJson } from './precision';

type API = (path: string, method?: string, body?: any, headers?: Record<string, string>) => Promise<any>;
export function CanonicalValidation({ api, collection, entity, row, onWritten, onCancel }: {
  api: API; collection: string; entity: any; row: any; onWritten: () => Promise<void>; onCancel: () => void;
}) {
  const [source, setSource] = useState('manual'), [flag, setFlag] = useState('valid'), [value, setValue] = useState('null');
  const [details, setDetails] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  const pending = useRef<{ signature: string; key: string } | null>(null);
  const live = useRef(true);
  useEffect(() => { live.current = true; return () => { live.current = false; }; }, []);
  async function submit(event: React.FormEvent) {
    event.preventDefault(); setBusy(true); setError('');
    try {
      const evidence = { value: value === 'null' ? null : value === 'true', ...Object.fromEntries(Object.entries(details).filter(([, v]) => v !== '')) };
      const body = { source_id: source, expected_version: entity.version, field_path: row.field_path,
        value_observation_id: row.observation_id, flags: { [flag]: evidence } };
      const url = '/canonical/' + collection + '/' + entity.id + '/items/' + row.item_id + '/flags';
      const signature = url + serializeApiJson(body);
      if (pending.current?.signature !== signature) pending.current = { signature, key: crypto.randomUUID() };
      await api(url, 'PATCH', body, { 'Idempotency-Key': pending.current!.key });
      if (!live.current) return;
      try { await onWritten(); }
      catch { if (live.current) setError('Validação registrada. Reabra a ficha para consultar a nova versão.'); }
    } catch (e: any) { if (live.current) setError(e.message); }
    finally { if (live.current) setBusy(false); }
  }
  return <section aria-label="Validação canônica">
    <h3>Validar observação de valor</h3>
    <p>Campo {row.field_path} · Valor selecionado: {displayPreciseValue(row.normalized_value)} · Versão da ficha: {entity.version}</p>
    <p>A confirmação será associada ao valor desta observação. Uma evidência histórica não confirma um valor diferente na ficha atual.</p>
    <form onSubmit={submit}><fieldset disabled={busy}><div className="form-grid">
      <label>Origem da validação<input required maxLength={120} value={source} onChange={e => setSource(e.target.value)} /></label>
      <label>Flag da validação<select aria-label="Flag da validação" value={flag} onChange={e => setFlag(e.target.value)}>
        <option value="valid">Validade</option><option value="is_whatsapp">WhatsApp</option><option value="ownership_confirmed">Titularidade</option>
        <option value="deliverable">Entregabilidade</option><option value="residence_confirmed">Residência</option>
      </select></label>
      <label>Resultado da validação<select aria-label="Resultado da validação" value={value} onChange={e => setValue(e.target.value)}><option value="null">Desconhecido</option><option value="true">Sim</option><option value="false">Não</option></select></label>
      {[
        ['reason', 'Motivo da validação', ''], ['observed_at', 'Observação da validação, com fuso', '2026-01-01T00:00:00Z'],
        ['source_updated_at', 'Data da fonte da validação, com fuso', '2026-01-01T00:00:00Z'],
        ['checked_at', 'Verificada em, com fuso', '2026-01-01T00:00:00Z'], ['expires_at', 'Vence em, com fuso', '2026-02-01T00:00:00Z'],
        ['method', 'Método da validação', ''], ['reference', 'Referência da validação', ''],
      ].map(([key, label, placeholder]) => <label key={key}>{label}<input value={details[key] || ''} placeholder={placeholder}
        maxLength={key === 'reason' ? 2000 : key === 'method' ? 160 : 1000} required={key === 'reason' && flag === 'valid' && value === 'false'}
        onChange={e => setDetails(old => ({ ...old, [key]: e.target.value }))} /></label>)}
    </div><p>Datas vazias permanecem desconhecidas. O vencimento sinaliza necessidade de revalidação e preserva o resultado.</p>
    <button className="primary" type="submit">Registrar validação canônica</button>{' '}
    <button type="button" onClick={onCancel}>Cancelar validação</button>
    </fieldset></form>
    {busy && <p role="status">Registrando validação…</p>}{error && <p role="alert">{error}</p>}
  </section>;
}

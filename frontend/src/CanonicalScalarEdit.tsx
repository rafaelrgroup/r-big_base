import React, { useEffect, useRef, useState } from 'react';
import { displayPreciseValue, parseApiJson, serializeApiJson, PreciseNumber } from './precision';

type API = (path: string, method?: string, body?: any, headers?: Record<string, string>) => Promise<any>;
export function CanonicalScalarEdit({ api, collection, entity, row, onWritten, onCancel }: {
  api: API; collection: string; entity: any; row: any; onWritten: () => Promise<void>; onCancel: () => void;
}) {
  const [source, setSource] = useState('manual');
  const [literal, setLiteral] = useState(() => serializeApiJson(row.value));
  const [details, setDetails] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  const pending = useRef<{ signature: string; key: string } | null>(null);
  const live = useRef(true);
  useEffect(() => { live.current = true; return () => { live.current = false; }; }, []);
  async function submit(event: React.FormEvent) {
    event.preventDefault(); setBusy(true); setError('');
    try {
      const value = parseApiJson(literal);
      if (value !== null && typeof value === 'object' && !(value instanceof PreciseNumber))
        throw new Error('Informe um valor escalar: texto entre aspas, número, true, false ou null.');
      const body = { source_id: source, expected_version: entity.version, field_path: row.field_path, value,
        ...Object.fromEntries(Object.entries(details).filter(([, v]) => v !== '')) };
      const url = '/canonical/' + collection + '/' + entity.id + '/items/' + row.item_id + '/value';
      const signature = url + serializeApiJson(body);
      if (pending.current?.signature !== signature) pending.current = { signature, key: crypto.randomUUID() };
      await api(url, 'PATCH', body, { 'Idempotency-Key': pending.current!.key });
      if (!live.current) return;
      try { await onWritten(); }
      catch { if (live.current) setError('Valor registrado. Reabra a ficha para consultar a nova versão.'); }
    } catch (e: any) { if (live.current) setError(e.message); }
    finally { if (live.current) setBusy(false); }
  }
  return <section aria-label="Edição escalar canônica">
    <h3>Editar valor do campo</h3>
    <p>Campo {row.field_path} · Valor atual: {displayPreciseValue(row.value)} · Versão da ficha: {entity.version}</p>
    <p>A edição preserva o histórico e as confirmações do valor anterior. Alterar um documento aqui não atualiza a identidade documental usada para localizar o cadastro.</p>
    <form onSubmit={submit}><fieldset disabled={busy}><div className="form-grid">
      <label>Origem da edição<input required maxLength={120} value={source} onChange={e => setSource(e.target.value)} /></label>
      <label>Novo valor JSON<textarea aria-label="Novo valor JSON" required value={literal} onChange={e => setLiteral(e.target.value)} /></label>
      {[
        ['reason', 'Motivo da edição', ''], ['observed_at', 'Observação da edição, com fuso', '2026-01-01T00:00:00Z'],
        ['source_updated_at', 'Data da fonte da edição, com fuso', '2026-01-01T00:00:00Z'],
      ].map(([key, label, placeholder]) => <label key={key}>{label}<input value={details[key] || ''} placeholder={placeholder}
        maxLength={key === 'reason' ? 2000 : 1000} onChange={e => setDetails(old => ({ ...old, [key]: e.target.value }))} /></label>)}
    </div><p>Texto usa aspas, como "Nome fictício". Números mantêm precisão exata; false, zero e null são valores distintos. Datas vazias permanecem desconhecidas.</p>
    <button className="primary" type="submit">Registrar valor canônico</button>{' '}
    <button type="button" onClick={onCancel}>Cancelar edição</button>
    </fieldset></form>
    {busy && <p role="status">Registrando valor…</p>}{error && <p role="alert">{error}</p>}
  </section>;
}

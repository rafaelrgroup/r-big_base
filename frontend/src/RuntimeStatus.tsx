import React from 'react';

type Dict = Record<string, any>;
const phases: Record<string, string> = {
  awaiting_import: 'Aguardando carga', restoring: 'Restaurando o backup',
  restore_requested: 'Restauração solicitada', verifying: 'Verificando os dados',
  importing: 'Importando registros', loading: 'Importando registros',
  indexing: 'Preparando a busca', pilot: 'Piloto de migração',
  completed: 'Etapa concluída', pending: 'Pendente', paused: 'Pausada',
  failed: 'Interrompida para revisão', needs_attention: 'Revisão necessária',
};
const counter = (value: unknown): value is number =>
  typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;

export function RuntimeStatus({ runtime, unavailable, refresh }: {
  runtime: Dict; unavailable: boolean; refresh: () => void;
}) {
  const migration = runtime.migration || {};
  const stale = migration.freshness === 'stale' || migration.freshness === 'clock_skew';
  const complete = runtime.migration_complete === true && !stale;
  const attention = ['needs_attention', 'paused', 'failed'].includes(migration.status);
  const percent = migration.progress_percent;
  const measured = typeof percent === 'number' && Number.isFinite(percent) && percent >= 0 && percent <= 100;
  return <section className="card migration-status" aria-label="Andamento da migração">
    <div className="toolbar"><h2>Preparação da base unificada</h2>
      <button type="button" onClick={refresh}>Atualizar andamento</button></div>
    {unavailable ? <p role="alert">Não foi possível atualizar o andamento. Reconecte e tente novamente.</p> : <>
      <p role="status">{stale ? 'Andamento desatualizado. Aguardando nova publicação do serviço.' :
        attention ? phases[migration.status] : complete ? 'Migração verificada pelo serviço.' :
        phases[migration.phase] || phases[migration.status] || 'O progresso da migração ainda não foi informado pelo serviço.'}</p>
      {counter(migration.processed) && <p>{stale ? 'Última contagem informada de registros de origem: ' : 'Registros de origem processados: '}<strong>{migration.processed.toLocaleString('pt-BR')}</strong>
        {counter(migration.total) ? ' de ' + migration.total.toLocaleString('pt-BR') : ' · total ainda não informado'}.</p>}
      {measured && !stale && <><label htmlFor="migration-progress">Progresso da etapa: {percent.toLocaleString('pt-BR')}%</label>
        <progress id="migration-progress" value={percent} max={100} style={{ width: '100%' }} /></>}
      {!complete && <p className="muted">A carga integral ainda não está confirmada. As consultas mostram apenas os registros já disponíveis.</p>}
    </>}
    <p className="muted">Consulta por documento, origem ou ID canônico. Busca ampla, importação pelo painel e XLSX serão disponibilizados conforme sua ativação.</p>
  </section>;
}

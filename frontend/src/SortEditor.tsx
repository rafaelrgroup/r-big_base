import React from "react";

export type SortCriterion = { field: string; direction: "asc" | "desc"; mode?: "min" | "max" };
export type SortSelection = { sorts: SortCriterion[] } | { sort: string; direction: "asc" | "desc" };
export type SortField = { field: string; label: string; multiple: boolean; status: string };
export const initialSort = (): SortSelection => ({ sorts: [{ field: "name", direction: "asc" }] });
export const legacySortFields: SortField[] = [
  { field: "name", label: "Nome", multiple: false, status: "legacy" },
  { field: "updated_at", label: "Atualização", multiple: false, status: "legacy" },
  { field: "id", label: "ID", multiple: false, status: "legacy" },
];
export function sortForServer(value: SortSelection, supportsMultiple: boolean): SortSelection {
  if (supportsMultiple || !("sorts" in value)) return value;
  // Only the initial default can fall back. Never flatten an explicit multi-sort search.
  if (value.sorts.length === 1 && value.sorts[0].field === "name" && value.sorts[0].direction === "asc" && !value.sorts[0].mode)
    return { sort: "name", direction: "asc" };
  throw new Error("O servidor conectado ainda não oferece esta ordenação múltipla. Os critérios foram preservados.");
}


export function SortEditor({ value, fields, onChange }: {
  value: SortSelection; fields: SortField[]; onChange: (value: SortSelection) => void;
}) {
  if (!("sorts" in value)) return <fieldset className="sort-editor">
    <legend>Ordenação</legend>
    <p>Pesquisa antiga: {fields.find(f => f.field === value.sort)?.label || value.sort}, {value.direction === "asc" ? "crescente" : "decrescente"}. O desempate por ID segue a mesma direção.</p>
    <button type="button" className="secondary" disabled={!fields.some(f => f.status === "ready_local")} onClick={() => onChange({ sorts: [{ field: value.sort, direction: value.direction }] })}>Editar com ordenação múltipla</button>
    {!fields.some(f => f.status === "ready_local") && <small>Ordenação múltipla indisponível no servidor conectado.</small>}
    <small>Ao editar, o desempate final passa a ser ID crescente, salvo escolha explícita.</small>
  </fieldset>;
  const criteria = value.sorts;
  const available = fields.filter(f => f.status === "ready_local");
  function change(index: number, next: SortCriterion) {
    onChange({ sorts: criteria.map((c, i) => i === index ? next : c) });
  }
  function move(index: number, delta: number) {
    const next = [...criteria];
    [next[index], next[index + delta]] = [next[index + delta], next[index]];
    onChange({ sorts: next });
  }
  return <fieldset className="sort-editor">
    <legend>Ordenação</legend>
    {criteria.map((criterion, index) => <div className="sort-row" key={index}>
      <span className="sort-priority" aria-hidden="true">{index + 1}</span>
      <label>Campo
        <select aria-label={`Campo de ordenação ${index + 1}`} value={criterion.field} onChange={e => change(index, { field: e.target.value, direction: criterion.direction })}>
          {available.map(f => <option key={f.field} value={f.field} disabled={criteria.some((c, i) => i !== index && c.field === f.field) || (f.field === "id" && index !== criteria.length - 1)}>{f.label}</option>)}
        </select>
      </label>
      <label>Direção
        <select aria-label={`Direção de ordenação ${index + 1}`} value={criterion.direction} onChange={e => change(index, { ...criterion, direction: e.target.value as "asc" | "desc" })}>
          <option value="asc">Crescente</option><option value="desc">Decrescente</option>
        </select>
      </label>
      {available.find(f => f.field === criterion.field)?.multiple && <label>Vários valores
        <select aria-label={`Escolha de valor ${index + 1}`} value={criterion.mode || "auto"} onChange={e => {
          const { mode: _mode, ...rest } = criterion;
          change(index, e.target.value === "auto" ? rest : { ...rest, mode: e.target.value as "min" | "max" });
        }}>
          <option value="auto">Padrão ({criterion.direction === "asc" ? "menor" : "maior"})</option>
          <option value="min">Menor valor</option><option value="max">Maior valor</option>
        </select>
      </label>}
      <div className="sort-actions">
        <button type="button" className="secondary" aria-label={`Subir prioridade ${index + 1}`} disabled={index === 0 || criterion.field === "id"} onClick={() => move(index, -1)}>↑</button>
        <button type="button" className="secondary" aria-label={`Descer prioridade ${index + 1}`} disabled={index === criteria.length - 1 || criteria[index + 1].field === "id"} onClick={() => move(index, 1)}>↓</button>
        <button type="button" className="text-button" aria-label={`Remover ordenação ${index + 1}`} disabled={criteria.length === 1} onClick={() => onChange({ sorts: criteria.filter((_, i) => i !== index) })}>Remover</button>
      </div>
    </div>)}
    <button type="button" className="secondary" disabled={criteria.length >= 5 || !available.length} onClick={() => {
      const field = available.find(f => !criteria.some(c => c.field === f.field))?.field;
      if (!field) return;
      const next = [...criteria];
      const idIndex = next.findIndex(c => c.field === "id");
      next.splice(idIndex < 0 ? next.length : idIndex, 0, { field, direction: "asc" });
      onChange({ sorts: next });
    }}>Adicionar ordenação</button>
    <small>Prioridades de cima para baixo. Ausentes e datas inválidas ficam no fim. Desempate final por ID crescente quando não escolhido. Valores de itens invalidados seguem a opção de inclusão da consulta.</small>
  </fieldset>;
}

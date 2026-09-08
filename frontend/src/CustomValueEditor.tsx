import React from "react";
import { parseIntegerInput } from "./precision";

type Dict = Record<string, any>;
export const fieldTypes: Record<string, string> = {
  text: "Texto",
  integer: "Número inteiro",
  decimal: "Número decimal",
  boolean: "Sim / não",
  date: "Data",
  enum: "Lista de opções",
  url: "Endereço de página",
  reference: "Referência a cadastro",
};

export function customPayload(form: Dict, definitions: Dict[]) {
  const definition = definitions.find((d) => d.id === form.field_id);
  if (!definition) throw new Error("Escolha um campo cadastrado.");
  let value = form.value;
  if (definition.type === "integer" && value !== null) {
    if (value === undefined) throw new Error("Informe um número inteiro.");
    value = parseIntegerInput(String(value));
  }
  if (value === undefined)
    throw new Error(
      "Informe o valor ou selecione explicitamente não informado.",
    );
  return { field_id: definition.id, value };
}

export function CustomValueEditor({
  form,
  setForm,
  definitions,
  entityType,
}: {
  form: Dict;
  setForm: (v: Dict) => void;
  definitions: Dict[];
  entityType: string;
}) {
  const available = definitions.filter(
    (d) => d.active && [undefined, "both", entityType].includes(d.scope),
  );
  const selected = available.find((d) => d.id === form.field_id);
  const setValue = (value: any) => setForm({ ...form, value });
  return (
    <div className="custom-editor">
      <label>
        Campo cadastrado
        <select
          aria-label="Campo cadastrado"
          required
          value={form.field_id || ""}
          onChange={(e) => setForm({ field_id: e.target.value })}
        >
          <option value="" disabled>
            Escolha o campo
          </option>
          {available.map((d) => (
            <option key={d.id} value={d.id}>
              {d.name}
            </option>
          ))}
        </select>
      </label>
      {!available.length && (
        <p className="info">
          O administrador pode criar campos na área Administração.
        </p>
      )}
      {selected && (
        <>
          <p className="muted">
            {fieldTypes[selected.type]} · versão {selected.version} ·{" "}
            {selected.multiple
              ? "Vários valores permitidos"
              : "Valor principal com alternativas preservadas"}
          </p>
          <label className="check">
            <input
              type="checkbox"
              checked={form.value === null}
              onChange={(e) => setValue(e.target.checked ? null : undefined)}
            />
            Registrar valor não informado
          </label>
          {form.value !== null &&
            (selected.type === "boolean" ? (
              <label>
                Valor do campo
                <select
                  aria-label="Valor do campo"
                  required
                  value={form.value === undefined ? "" : String(form.value)}
                  onChange={(e) => setValue(e.target.value === "true")}
                >
                  <option value="" disabled>
                    Selecione o resultado
                  </option>
                  <option value="true">Sim</option>
                  <option value="false">Não</option>
                </select>
              </label>
            ) : selected.type === "enum" ? (
              <label>
                Valor do campo
                <select
                  aria-label="Valor do campo"
                  required
                  value={form.value ?? ""}
                  onChange={(e) => setValue(e.target.value)}
                >
                  <option value="" disabled>
                    Escolha uma opção
                  </option>
                  {(selected.options || []).map((v: string) => (
                    <option key={v} value={v}>
                      {v}
                    </option>
                  ))}
                </select>
              </label>
            ) : selected.type === "reference" ? (
              <div className="two">
                <label>
                  Tipo de cadastro relacionado
                  <select
                    value={form.value?.entity_type || "person"}
                    onChange={(e) =>
                      setValue({
                        id: form.value?.id || "",
                        entity_type: e.target.value,
                      })
                    }
                  >
                    <option value="person">Pessoa</option>
                    <option value="company">Empresa</option>
                  </select>
                </label>
                <label>
                  ID do cadastro relacionado
                  <input
                    required
                    value={form.value?.id || ""}
                    onChange={(e) =>
                      setValue({
                        entity_type: form.value?.entity_type || "person",
                        id: e.target.value,
                      })
                    }
                  />
                </label>
              </div>
            ) : (
              <label>
                Valor do campo
                <input
                  required
                  type={
                    selected.type === "date"
                      ? "date"
                      : selected.type === "url"
                        ? "url"
                        : "text"
                  }
                  inputMode={
                    ["integer", "decimal"].includes(selected.type)
                      ? "decimal"
                      : undefined
                  }
                  value={form.value ?? ""}
                  onChange={(e) => setValue(e.target.value)}
                />
              </label>
            ))}
        </>
      )}
    </div>
  );
}

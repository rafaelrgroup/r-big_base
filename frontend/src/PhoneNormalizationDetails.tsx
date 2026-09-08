import React from "react";
import { displayPreciseValue } from "./precision";

export function PhoneNormalizationDetails({
  audit,
}: {
  audit: Record<string, any>;
}) {
  const decisions: Record<string, string> = {
    canonical: "Formato padronizado",
    historical_conversion: "Nono dígito acrescentado com regra histórica",
    review: "Precisa de revisão; entrada preservada",
  };
  const rollout =
    typeof audit.rollout_date === "string" &&
    /^\d{4}-\d{2}-\d{2}$/.test(audit.rollout_date)
      ? audit.rollout_date
      : null;
  const references = Array.isArray(audit.sources)
    ? audit.sources.filter((url: unknown) => {
        if (typeof url !== "string") return false;
        try {
          return ["https:", "http:"].includes(new URL(url).protocol);
        } catch {
          return false;
        }
      })
    : [];
  return (
    <details>
      <summary>Como o telefone foi tratado</summary>
      <p>
        {decisions[String(audit.decision)] ||
          displayPreciseValue(audit.decision)}
      </p>
      <p>
        Entrada:{" "}
        <span className="mono">
          {displayPreciseValue(audit.input_components)}
        </span>
      </p>
      <p>
        Resultado:{" "}
        <span className="mono">{displayPreciseValue(audit.output_number)}</span>
      </p>
      {rollout && (
        <p>
          Implantação do nono dígito neste DDD:{" "}
          {rollout.split("-").reverse().join("/")}. Esta é a data da regra; a
          data da informação continua registrada na origem.
        </p>
      )}
      {audit.requires_new_confirmation && (
        <p>
          O número tratado precisa de nova confirmação de validade, titularidade
          e WhatsApp.
        </p>
      )}
      {!!audit.candidates?.length && (
        <p>
          Alternativas para revisão: {displayPreciseValue(audit.candidates)}
        </p>
      )}
      <small>
        Regra: {displayPreciseValue(audit.rule_id) || "Revisão pendente"} ·
        versão {displayPreciseValue(audit.version)}
      </small>
      {!!references.length && (
        <p>
          {references.map((url: string, index: number) => (
            <React.Fragment key={url}>
              {index > 0 && " · "}
              <a href={url} target="_blank" rel="noreferrer">
                Referência {index + 1}
              </a>
            </React.Fragment>
          ))}
        </p>
      )}
    </details>
  );
}

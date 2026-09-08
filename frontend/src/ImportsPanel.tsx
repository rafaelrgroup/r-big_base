import React, { useEffect, useMemo, useRef, useState } from "react";
import { AlertCircle, FileUp, RefreshCw, X } from "lucide-react";
import {
  displayPreciseValue,
  parseApiJson,
  serializeApiJson,
} from "./precision";
import "./imports.css";

type Dict = Record<string, any>;
type Api = (
  path: string,
  method?: string,
  body?: any,
  headers?: Dict,
) => Promise<any>;
type InputProblem = { line?: number; message: string };
const MAX_ENTRIES = 1000;
const MAX_BYTES = 2 * 1024 * 1024;
const PAGE_SIZE = 25;
const ACTIVE = new Set(["pending", "processing"]);
const STATUS: Record<string, string> = {
  pending: "Pendente",
  processing: "Processando",
  completed: "Concluída",
  completed_with_errors: "Concluída com erros",
  failed: "Falhou",
  cancelled: "Cancelada",
  succeeded: "Incorporado",
  not_processed: "Não processado",
};
const EXAMPLE = `[
  {"entity_type":"person","source_id":"ID_DA_FONTE_CADASTRADA","external_id":"exemplo-sintetico-001","items":[{"kind":"identity","value":{"name":"Pessoa Sintética Importação"}},{"kind":"email","value":{"email":"pessoa.sintetica@example.invalid"}}]}
]`;
const message = (error: unknown) =>
  error instanceof Error ? error.message : displayPreciseValue(error);

/** Syntax only: payload validation belongs to the API, independently for every entry. */
export function parseImportEntries(input: string): {
  entries: any[];
  errors: InputProblem[];
  bytes: number;
  format: string;
} {
  const bytes = new TextEncoder().encode(input).byteLength;
  if (bytes > MAX_BYTES)
    return {
      entries: [],
      errors: [
        {
          message: "O conteúdo ultrapassa 2 MiB. Divida-o em arquivos menores.",
        },
      ],
      bytes,
      format: "",
    };
  const text = input.replace(/^\uFEFF/, "");
  if (!text.trim()) return { entries: [], errors: [], bytes, format: "" };
  let entries: any[] = [];
  const errors: InputProblem[] = [];
  const array = text.trimStart().startsWith("[");
  if (array) {
    try {
      const parsed = parseApiJson(text);
      if (!Array.isArray(parsed))
        throw new Error("O arquivo deve conter uma lista JSON.");
      entries = parsed;
    } catch (error) {
      errors.push({
        message: "Lista JSON inválida: " + message(error).slice(0, 300),
      });
    }
  } else {
    text.split(/\r?\n/).forEach((line, index) => {
      if (!line.trim()) return;
      try {
        entries.push(parseApiJson(line));
      } catch (error) {
        errors.push({ line: index + 1, message: message(error).slice(0, 300) });
      }
    });
  }
  if (entries.length > MAX_ENTRIES)
    errors.push({
      message:
        "São permitidas até 1.000 entradas por importação. Divida o conteúdo.",
    });
  if (!entries.length && !errors.length)
    errors.push({ message: "O conteúdo não possui entradas para importar." });
  return { entries, errors, bytes, format: array ? "Lista JSON" : "JSONL" };
}

function Status({ value }: { value: string }) {
  const color = ["completed", "succeeded"].includes(value)
    ? "good"
    : ["failed", "completed_with_errors"].includes(value)
      ? "bad"
      : "neutral";
  return <span className={"badge " + color}>{STATUS[value] || value}</span>;
}

export function ImportsPanel({
  api,
  canEnrich,
}: {
  api: Api;
  canEnrich: boolean;
}) {
  const [name, setName] = useState("");
  const [text, setText] = useState("");
  const [fileName, setFileName] = useState("");
  const [preview, setPreview] = useState({
    source: "",
    ...parseImportEntries(""),
  });
  const [jobs, setJobs] = useState<Dict[]>([]);
  const [loading, setLoading] = useState(true);
  const [refresh, setRefresh] = useState(0);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [offset, setOffset] = useState(0);
  const [detail, setDetail] = useState<Dict | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [sending, setSending] = useState(false);
  const [readingFile, setReadingFile] = useState(false);
  const [fileProblem, setFileProblem] = useState(false);
  const [cancelling, setCancelling] = useState<string | null>(null);
  const [resuming, setResuming] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const mounted = useRef(false);
  const generation = useRef(0);
  const fileRevision = useRef(0);
  const retry = useRef<{ body: string; key: string } | null>(null);
  const resumeRetry = useRef<{ checkpoint: string; key: string } | null>(null);

  useEffect(() => {
    mounted.current = true;
    generation.current++;
    setJobs([]);
    setDetail(null);
    setSelectedId(null);
    setText("");
    setName("");
    setFileName("");
    setError("");
    setNotice("");
    setSending(false);
    setReadingFile(false);
    setFileProblem(false);
    setCancelling(null);
    setResuming(null);
    retry.current = null;
    resumeRetry.current = null;
    return () => {
      mounted.current = false;
      generation.current++;
      fileRevision.current++;
    };
  }, [api, canEnrich]);

  useEffect(() => {
    const timer = window.setTimeout(
      () => setPreview({ source: text, ...parseImportEntries(text) }),
      250,
    );
    return () => window.clearTimeout(timer);
  }, [text]);

  useEffect(() => {
    if (!canEnrich) {
      setJobs([]);
      setLoading(false);
      return;
    }
    let cancelled = false;
    let busy = false;
    const token = generation.current;
    const load = async () => {
      if (busy) return;
      busy = true;
      try {
        const response = await api("/imports");
        if (!cancelled && mounted.current && token === generation.current)
          setJobs(response.items || []);
      } catch (failure) {
        if (!cancelled && mounted.current && token === generation.current)
          setError(message(failure));
      } finally {
        busy = false;
        if (!cancelled && mounted.current && token === generation.current)
          setLoading(false);
      }
    };
    void load();
    const timer = window.setInterval(() => void load(), 3000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [api, canEnrich, refresh]);

  const selectedJobRevision = jobs.find(
    (job) => job.id === selectedId,
  )?.updated_at;
  useEffect(() => setDetail(null), [api, canEnrich, selectedId, offset]);
  useEffect(() => {
    if (!canEnrich || !selectedId) {
      setDetailLoading(false);
      return;
    }
    let cancelled = false;
    let busy = false;
    const token = generation.current;
    setDetailLoading(true);
    const load = async () => {
      if (busy) return;
      busy = true;
      try {
        const response = await api(
          "/imports/" +
            encodeURIComponent(selectedId) +
            "?offset=" +
            offset +
            "&limit=" +
            PAGE_SIZE,
        );
        if (!cancelled && mounted.current && token === generation.current)
          setDetail(response);
      } catch (failure) {
        if (!cancelled && mounted.current && token === generation.current)
          setError(message(failure));
      } finally {
        busy = false;
        if (!cancelled && mounted.current && token === generation.current)
          setDetailLoading(false);
      }
    };
    void load();
    const timer = window.setInterval(() => void load(), 3000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [api, canEnrich, selectedId, offset, refresh, selectedJobRevision]);

  const bodyBytes = useMemo(() => {
    if (preview.errors.length || preview.source !== text) return 0;
    try {
      return new TextEncoder().encode(
        serializeApiJson({ name: name.trim(), entries: preview.entries }),
      ).byteLength;
    } catch {
      return MAX_BYTES + 1;
    }
  }, [preview, name, text]);
  const checking = preview.source !== text;
  const ready =
    canEnrich &&
    !!name.trim() &&
    !checking &&
    !readingFile &&
    !fileProblem &&
    !preview.errors.length &&
    preview.entries.length > 0 &&
    bodyBytes <= MAX_BYTES;

  async function readFile(file?: File) {
    if (!file) return;
    const revision = ++fileRevision.current;
    const token = generation.current;
    setError("");
    setFileProblem(false);
    setReadingFile(true);
    try {
      if (file.size > MAX_BYTES)
        throw new Error(
          "O arquivo ultrapassa 2 MiB. Divida-o antes de importar.",
        );
      const content = new TextDecoder("utf-8", { fatal: true }).decode(
        await file.arrayBuffer(),
      );
      if (
        !mounted.current ||
        token !== generation.current ||
        revision !== fileRevision.current
      )
        return;
      setText(content);
      setFileName(file.name);
      if (!name.trim())
        setName(file.name.replace(/\.(jsonl|ndjson|json)$/i, "").slice(0, 160));
    } catch (failure) {
      if (
        mounted.current &&
        token === generation.current &&
        revision === fileRevision.current
      ) {
        setFileProblem(true);
        setError(
          failure instanceof TypeError
            ? "Não foi possível ler o arquivo como UTF-8. Corrija a codificação para preservar os caracteres."
            : message(failure),
        );
      }
    } finally {
      if (
        mounted.current &&
        token === generation.current &&
        revision === fileRevision.current
      )
        setReadingFile(false);
    }
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!ready || sending) return;
    const token = generation.current;
    setSending(true);
    setError("");
    setNotice("");
    try {
      const body = { name: name.trim(), entries: preview.entries };
      const encoded = serializeApiJson(body);
      if (!retry.current || retry.current.body !== encoded)
        retry.current = { body: encoded, key: crypto.randomUUID() };
      const job = await api("/imports", "POST", body, {
        "Idempotency-Key": retry.current.key,
      });
      if (!mounted.current || token !== generation.current) return;
      setSelectedId(job.id);
      setOffset(0);
      setRefresh((value) => value + 1);
      setNotice(
        "Importação recebida. Acompanhe o processamento e o resultado de cada entrada abaixo.",
      );
    } catch (failure) {
      if (mounted.current && token === generation.current)
        setError(message(failure));
    } finally {
      if (mounted.current && token === generation.current) setSending(false);
    }
  }

  async function cancel(id: string) {
    if (cancelling || resuming) return;
    const token = generation.current;
    setCancelling(id);
    setError("");
    try {
      await api("/imports/" + encodeURIComponent(id) + "/cancel", "POST");
      if (!mounted.current || token !== generation.current) return;
      setNotice(
        "Cancelamento solicitado. Entradas já incorporadas e seus históricos permanecem preservados.",
      );
      setRefresh((value) => value + 1);
    } catch (failure) {
      if (mounted.current && token === generation.current)
        setError(message(failure));
    } finally {
      if (mounted.current && token === generation.current) setCancelling(null);
    }
  }

  async function resume(job: Dict) {
    if (resuming || cancelling) return;
    const token = generation.current;
    setResuming(job.id);
    setError("");
    try {
      const checkpoint = serializeApiJson({
        id: job.id,
        processed: job.processed,
        updated_at: job.updated_at,
        status: job.status,
      });
      if (!resumeRetry.current || resumeRetry.current.checkpoint !== checkpoint)
        resumeRetry.current = { checkpoint, key: crypto.randomUUID() };
      await api(
        "/imports/" + encodeURIComponent(job.id) + "/resume",
        "POST",
        {},
        { "Idempotency-Key": resumeRetry.current.key },
      );
      if (!mounted.current || token !== generation.current) return;
      setSelectedId(job.id);
      setOffset(0);
      setNotice(
        "Retomada solicitada a partir da próxima entrada não processada. Resultados anteriores e seus históricos permanecem preservados.",
      );
      setRefresh((value) => value + 1);
    } catch (failure) {
      if (mounted.current && token === generation.current)
        setError(message(failure));
    } finally {
      if (mounted.current && token === generation.current) setResuming(null);
    }
  }

  if (!canEnrich)
    return (
      <p className="info">
        Seu usuário precisa de permissão para agregar dados e acessar
        importações.
      </p>
    );
  const results: Dict[] = detail?.results || [];
  const selected = detail?.job;
  return (
    <div className="imports-panel">
      {error && (
        <div className="error" role="alert">
          <AlertCircle size={18} aria-hidden="true" />
          <span>{error}</span>
          <button
            className="icon"
            aria-label="Fechar erro da importação"
            onClick={() => setError("")}
          >
            <X size={16} />
          </button>
        </div>
      )}
      {notice && (
        <p className="notice" role="status">
          {notice}
        </p>
      )}
      <div className="imports-layout">
        <section className="card form-card" aria-labelledby="imports-new-title">
          <div className="section-title">
            <FileUp size={23} aria-hidden="true" />
            <h2 id="imports-new-title">Agregar cadastros em lote</h2>
          </div>
          <p className="muted">
            Envie pessoas e empresas com origem e datas em cada entrada. Cada
            registro será validado e terá seu próprio resultado.
          </p>
          <form onSubmit={submit}>
            <label>
              Nome da importação
              <input
                required
                maxLength={160}
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder="Ex.: atualização do sistema de atendimento"
              />
            </label>
            <label>
              Arquivo JSON ou JSONL
              <input
                type="file"
                accept=".json,.jsonl,.ndjson,application/json,application/x-ndjson"
                disabled={readingFile || sending}
                onChange={(event) => {
                  void readFile(event.target.files?.[0]);
                  event.target.value = "";
                }}
              />
            </label>
            {fileName && (
              <p className="muted imports-file">
                Arquivo carregado: {fileName}. Revise o conteúdo antes de
                iniciar.
              </p>
            )}
            <label>
              Conteúdo da importação
              <textarea
                rows={11}
                spellCheck={false}
                value={text}
                onChange={(event) => {
                  fileRevision.current++;
                  setReadingFile(false);
                  setFileProblem(false);
                  setFileName("");
                  setText(event.target.value);
                }}
                placeholder={EXAMPLE}
                aria-describedby="imports-format-help"
              />
            </label>
            <p className="info" id="imports-format-help">
              JSONL: um registro completo por linha. JSON: uma lista de
              registros. Informe o ID de uma fonte cadastrada em{" "}
              <code>source_id</code>. Até 1.000 entradas e 2 MiB por trabalho.
            </p>
            <div className="imports-preview" aria-live="polite">
              {readingFile ? (
                <p>Lendo arquivo…</p>
              ) : checking ? (
                <p>Conferindo o conteúdo…</p>
              ) : !text.trim() ? (
                <p className="muted">
                  Adicione um arquivo ou cole o conteúdo para conferir a
                  quantidade.
                </p>
              ) : (
                <p>
                  <strong>{displayPreciseValue(preview.entries.length)}</strong>{" "}
                  entradas interpretadas · {preview.format} ·{" "}
                  {(preview.bytes / 1024).toFixed(1)} KiB
                </p>
              )}
              {!checking && preview.errors.length > 0 && (
                <div className="imports-input-errors" role="alert">
                  <strong>
                    Corrija {preview.errors.length}{" "}
                    {preview.errors.length === 1 ? "problema" : "problemas"}{" "}
                    antes de enviar:
                  </strong>
                  <ul>
                    {preview.errors.slice(0, 20).map((problem, index) => (
                      <li key={index}>
                        {problem.line ? "Linha " + problem.line + ": " : ""}
                        {problem.message}
                      </li>
                    ))}
                  </ul>
                  {preview.errors.length > 20 && (
                    <p>
                      Há outros {preview.errors.length - 20} erros. Corrija os
                      primeiros e confira novamente.
                    </p>
                  )}
                </div>
              )}
              {!checking && bodyBytes > MAX_BYTES && (
                <p className="error" role="alert">
                  O pedido completo ultrapassa 2 MiB. Reduza a quantidade de
                  entradas.
                </p>
              )}
            </div>
            <button
              className="primary wide"
              disabled={!ready || sending}
              type="submit"
            >
              <FileUp size={17} aria-hidden="true" />
              {sending ? "Enviando importação…" : "Iniciar importação"}
            </button>
            <p className="imports-hint muted">
              A conferência local verifica o formato. Fontes, permissões e
              campos serão validados pela API, entrada por entrada.
            </p>
          </form>
        </section>
        <section className="card" aria-labelledby="imports-jobs-title">
          <div className="toolbar imports-toolbar">
            <h2 id="imports-jobs-title">Suas importações</h2>
            <button
              className="secondary"
              type="button"
              onClick={() => {
                setLoading(true);
                setRefresh((value) => value + 1);
              }}
              aria-label="Atualizar importações"
            >
              <RefreshCw size={16} aria-hidden="true" />
              Atualizar
            </button>
          </div>
          {loading && !jobs.length ? (
            <p className="empty" role="status">
              Carregando importações…
            </p>
          ) : !jobs.length ? (
            <div className="empty">Nenhuma importação recebida ainda.</div>
          ) : (
            jobs.map((job) => {
              const percentage = Math.max(
                0,
                Math.min(100, Number(job.progress_percent || 0)),
              );
              return (
                <article className="job imports-job" key={job.id}>
                  <div className="between">
                    <strong>{displayPreciseValue(job.name)}</strong>
                    <Status value={job.status} />
                  </div>
                  <p className="muted imports-job-id">
                    ID:{" "}
                    <span className="mono">{displayPreciseValue(job.id)}</span>
                  </p>
                  <label className="imports-progress-label">
                    Progresso de {displayPreciseValue(job.name)}
                    <progress value={percentage} max={100} />
                    <span>
                      {percentage.toLocaleString("pt-BR", {
                        maximumFractionDigits: 1,
                      })}
                      % · {displayPreciseValue(job.processed ?? 0)} de{" "}
                      {displayPreciseValue(job.total ?? 0)} processadas
                    </span>
                  </label>
                  <p>
                    {displayPreciseValue(job.succeeded ?? 0)} incorporadas ·{" "}
                    {displayPreciseValue(job.failed_count ?? 0)} com erro
                  </p>
                  {job.error && (
                    <p className="error">
                      {displayPreciseValue(job.error.message ?? job.error)}
                    </p>
                  )}
                  <div className="job-actions">
                    <button
                      type="button"
                      className="secondary"
                      aria-pressed={selectedId === job.id}
                      onClick={() => {
                        setSelectedId(job.id);
                        setOffset(0);
                      }}
                    >
                      Ver resultados
                    </button>
                    {ACTIVE.has(job.status) && (
                      <button
                        type="button"
                        className="text-button"
                        disabled={cancelling !== null || resuming !== null}
                        onClick={() => void cancel(job.id)}
                      >
                        {cancelling === job.id
                          ? "Cancelando…"
                          : "Cancelar importação"}
                      </button>
                    )}
                    {["failed", "cancelled"].includes(job.status) &&
                      job.processed < job.total && (
                        <button
                          type="button"
                          className="secondary"
                          disabled={resuming !== null || cancelling !== null}
                          onClick={() => void resume(job)}
                        >
                          {resuming === job.id
                            ? "Retomando…"
                            : "Retomar importação"}
                        </button>
                      )}
                  </div>
                  {["failed", "cancelled"].includes(job.status) &&
                    job.processed < job.total && (
                      <p className="muted imports-hint">
                        A retomada começa na próxima entrada não processada e
                        preserva os resultados anteriores.
                      </p>
                    )}
                </article>
              );
            })
          )}
        </section>
      </div>
      {selectedId && (
        <section
          className="card imports-details"
          aria-labelledby="imports-results-title"
        >
          <div className="toolbar imports-toolbar">
            <div>
              <h2 id="imports-results-title">Resultado por entrada</h2>
              <p className="muted">
                {selected
                  ? displayPreciseValue(selected.name)
                  : "Carregando trabalho…"}
              </p>
            </div>
            <button
              className="icon"
              aria-label="Fechar resultados da importação"
              onClick={() => setSelectedId(null)}
            >
              <X size={18} />
            </button>
          </div>
          {detailLoading && !detail ? (
            <p className="empty" role="status">
              Carregando resultados…
            </p>
          ) : (
            <>
              <div
                className="table-wrap imports-results"
                role="region"
                aria-label="Resultados das entradas da importação"
                tabIndex={0}
              >
                <table>
                  <thead>
                    <tr>
                      <th>ENTRADA</th>
                      <th>RESULTADO</th>
                      <th>CADASTRO</th>
                      <th>DETALHES</th>
                    </tr>
                  </thead>
                  <tbody>
                    {results.map((result) => (
                      <tr key={String(result.index)}>
                        <td>
                          {displayPreciseValue(
                            result.input_id ?? result.index + 1,
                          )}
                        </td>
                        <td>
                          <Status value={result.status} />
                        </td>
                        <td className="mono">
                          {result.entity_id
                            ? displayPreciseValue(result.entity_id)
                            : "—"}
                        </td>
                        <td>
                          {result.error ? (
                            <>
                              <strong>
                                {displayPreciseValue(
                                  result.error.message ?? result.error,
                                )}
                              </strong>
                              {result.error.code && (
                                <small className="block muted">
                                  Código:{" "}
                                  {displayPreciseValue(result.error.code)}
                                </small>
                              )}
                            </>
                          ) : result.status === "succeeded" ? (
                            "Dados incorporados com origem e histórico."
                          ) : result.status === "not_processed" ? (
                            "Entrada preservada; não foi incorporada."
                          ) : (
                            "Aguardando processamento."
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {!results.length && (
                <p className="empty">Ainda não há resultados nesta página.</p>
              )}
              <div className="pagination imports-pagination">
                <span>
                  {results.length
                    ? "Entradas " +
                      (offset + 1) +
                      "–" +
                      (offset + results.length)
                    : "Nenhuma entrada nesta página"}{" "}
                  · total {displayPreciseValue(detail?.total_results ?? 0)}
                </span>
                <div>
                  <button
                    type="button"
                    className="secondary"
                    disabled={offset === 0 || detailLoading}
                    onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                  >
                    Anterior
                  </button>
                  <button
                    type="button"
                    className="secondary"
                    disabled={
                      !detail ||
                      offset + PAGE_SIZE >= Number(detail.total_results) ||
                      detailLoading
                    }
                    onClick={() => setOffset(offset + PAGE_SIZE)}
                  >
                    Próxima
                  </button>
                </div>
              </div>
              <p className="imports-detail-note muted">
                Resultados exibidos em páginas de 25 entradas. O cancelamento
                mantém os dados já incorporados e seus históricos.
              </p>
            </>
          )}
        </section>
      )}
    </div>
  );
}

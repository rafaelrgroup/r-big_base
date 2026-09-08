import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Search,
  Users,
  Building2,
  Layers3,
  ArrowUpRight,
  Plus,
  X,
  Check,
  ShieldCheck,
  Download,
  Clock,
  LogOut,
  SlidersHorizontal,
  ChevronRight,
  Database,
  History,
  KeyRound,
  Menu,
  Sun,
  Moon,
  RefreshCw,
  FileSpreadsheet,
  AlertCircle,
} from "lucide-react";
import QRCode from "qrcode";
import { SortEditor, initialSort, sortForServer, legacySortFields, type SortSelection, type SortField } from "./SortEditor";
import { ImportsPanel } from "./ImportsPanel";
import { PhoneNormalizationDetails } from "./PhoneNormalizationDetails";
import {
  parseApiJson,
  serializeApiJson,
  displayPreciseValue,
} from "./precision";
import "./style.css";
import {
  CustomValueEditor,
  customPayload,
  fieldTypes,
} from "./CustomValueEditor";

type Dict = Record<string, any>;
const labels: Dict = {
  identity: "Identidade",
  document: "Documento",
  phone: "Telefone",
  email: "Email",
  address: "Endereço",
  username: "Username",
  relationship: "Relação",
  activity: "Atividade",
  custom: "Campo adicional",
  name: "Nome",
  number: "Número",
  type: "Tipo",
  country: "País",
  city: "Cidade",
  state: "UF",
  street: "Rua",
  postal_code: "CEP",
  birth_date: "Nascimento",
  age: "Idade",
  sex: "Sexo",
  platform: "Plataforma",
  classification: "Classificação",
  usage: "Uso",
  syntax_valid: "Formato válido",
  target_id: "ID relacionado",
  target_document: "Documento relacionado",
  target_document_type: "Tipo do documento relacionado",
  target_name: "Nome relacionado (se ainda não identificado)",
  trade_name: "Nome fantasia",
  legal_nature: "Natureza jurídica",
  opened_at: "Data de abertura",
  registration_status: "Situação cadastral",
  company_size: "Porte",
  role: "Papel / atividade",
  from: "Início do vínculo",
  until: "Fim do vínculo",
  code: "CNAE / código",
  field_id: "Campo",
  value: "Valor",
  extension: "Ramal",
  ddd: "DDD",
  canonical_number: "Número padronizado",
  country_calling_code: "Código do país",
  national_number: "Número nacional",
  phone_normalization: "Tratamento do telefone",
};
const forms: Dict = {
  identity: ["name", "birth_date", "sex"],
  document: ["type", "number", "country"],
  phone: ["number", "country", "ddd", "extension", "usage"],
  email: ["email"],
  address: [
    "country",
    "state",
    "city",
    "street",
    "number",
    "postal_code",
    "complement",
  ],
  username: ["platform", "username"],
  relationship: [
    "target_id",
    "target_document",
    "target_document_type",
    "target_name",
    "type",
    "from",
    "until",
  ],
  activity: ["code", "role"],
  custom: ["field_id", "value"],
};
let csrf = "";
let accessGeneration = 0;
class ApiError extends Error {
  constructor(message: string, readonly status: number, readonly code?: string) {
    super(message);
  }
}
async function api(
  path: string,
  method = "GET",
  body?: any,
  headers: Dict = {},
) {
  const generation = accessGeneration;
  const r = await fetch("/api/v1" + path, {
    method,
    credentials: "same-origin",
    headers: {
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      "X-CSRF-Token": csrf,
      ...headers,
    },
    body: body === undefined ? undefined : serializeApiJson(body),
  });
  const j = parseApiJson(await r.text());
  if (generation !== accessGeneration)
    throw new Error("A sessão mudou; atualize a consulta.");
  if (!r.ok)
    throw new ApiError(
      typeof j.detail === "string" ? j.detail :
        typeof j.detail?.message === "string" ? j.detail.message : displayPreciseValue(j.detail),
      r.status,
      typeof j.detail?.code === "string" ? j.detail.code : j.code,
    );
  return j;
}
const date = (v: any) =>
  v ? new Date(v).toLocaleString("pt-BR") : "Data não informada";
const val = (v: any) =>
  v === null || v === undefined
    ? "Não confirmado"
    : typeof v === "boolean"
      ? v
        ? "Sim"
        : "Não"
      : displayPreciseValue(v);
function Badge({ value, label }: { value: any; label?: string }) {
  return (
    <span
      className={
        "badge " +
        (value === true ? "good" : value === false ? "bad" : "neutral")
      }
    >
      {label || val(value)}
    </span>
  );
}
function Login({ onLogin }: { onLogin: (x: Dict) => void }) {
  const [invitation, setInvitation] = useState(
    () => new URLSearchParams(location.hash.slice(1)).get("activate") || "",
  );
  useEffect(() => {
    if (invitation) history.replaceState(null, "", location.pathname);
  }, []);
  const [username, setUser] = useState(""),
    [password, setPass] = useState(""),
    [challenge, setChallenge] = useState<Dict | null>(null),
    [code, setCode] = useState(""),
    [qr, setQr] = useState(""),
    [recovery, setRecovery] = useState(false),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      if (!challenge) {
        const j = await api(
          invitation ? "/auth/activate" : "/auth/login",
          "POST",
          invitation ? { token: invitation, password } : { username, password },
        );
        setInvitation("");
        setPass("");
        setChallenge(j);
        if (j.otp_uri)
          setQr(await QRCode.toDataURL(j.otp_uri, { width: 220, margin: 2 }));
      } else if (recovery) {
        const j = await api("/auth/recovery", "POST", {
          challenge: challenge.challenge,
          code,
        });
        setChallenge(j);
        setCode("");
        setRecovery(false);
        setQr(await QRCode.toDataURL(j.otp_uri, { width: 220, margin: 2 }));
      } else {
        const j = await api("/auth/otp", "POST", {
          challenge: challenge.challenge,
          code,
        });
        csrf = j.csrf;
        onLogin(j);
      }
    } catch (e) {
      setError(String((e as Error).message));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="login">
      <div className="login-aside">
        <div className="brand">
          <span className="mark">B</span> BIG BASE
        </div>
        <h1>
          Informação completa.
          <br />
          História preservada.
        </h1>
        <p>Pessoas, empresas e suas conexões em um cadastro rastreável.</p>
        <div className="login-note">
          <ShieldCheck size={24} />
          <span>Acesso protegido por senha e autenticação em duas etapas.</span>
        </div>
      </div>
      <main className="login-main">
        <form onSubmit={submit}>
          <span className="eyebrow">CADASTRO UNIFICADO</span>
          <h2>
            {challenge
              ? challenge.enrollment_required
                ? "Proteja seu acesso"
                : "Confirme seu acesso"
              : invitation
                ? "Ative seu acesso"
                : "Bem-vindo de volta"}
          </h2>
          <p className="muted">
            {challenge
              ? "Use o Google Authenticator para continuar."
              : invitation
                ? "Defina sua senha. A próxima etapa configura seu autenticador."
                : "Entre com o usuário criado pelo administrador."}
          </p>
          {error && (
            <div role="alert" className="error">
              {error}
            </div>
          )}
          {!challenge ? (
            <>
              {!invitation && (
                <label>
                  Usuário
                  <input
                    autoComplete="username"
                    required
                    value={username}
                    onChange={(e) => setUser(e.target.value)}
                  />
                </label>
              )}
              <label>
                Senha
                <input
                  type="password"
                  autoComplete={
                    invitation ? "new-password" : "current-password"
                  }
                  minLength={invitation ? 12 : undefined}
                  required
                  value={password}
                  onChange={(e) => setPass(e.target.value)}
                />
              </label>
            </>
          ) : (
            <>
              {qr && (
                <div className="qr">
                  <img
                    src={qr}
                    alt="QR code para cadastrar BIG BASE no autenticador"
                  />
                  <p>Escaneie o QR code e informe o primeiro código.</p>
                </div>
              )}
              <label>
                {recovery ? "Código de recuperação" : "Código de seis dígitos"}
                <input
                  className="otp"
                  autoComplete="one-time-code"
                  inputMode={recovery ? "text" : "numeric"}
                  pattern={recovery ? "[a-f0-9]{12}" : "[0-9]{6}"}
                  maxLength={recovery ? 12 : 6}
                  required
                  value={code}
                  onChange={(e) => setCode(e.target.value)}
                />
              </label>
            </>
          )}
          <button className="primary wide" disabled={busy}>
            {busy
              ? "Verificando…"
              : challenge
                ? "Confirmar acesso"
                : "Continuar"}
            <ArrowUpRight size={18} />
          </button>
          {challenge && !challenge.enrollment_required && (
            <button
              type="button"
              className="text-button"
              onClick={() => {
                setRecovery(!recovery);
                setCode("");
              }}
            >
              {recovery ? "Usar autenticador" : "Usar código de recuperação"}
            </button>
          )}
          {challenge && (
            <button
              type="button"
              className="text-button"
              onClick={() => {
                setChallenge(null);
                setCode("");
                setQr("");
              }}
            >
              Voltar ao login
            </button>
          )}
          <p className="development">
            Ambiente isolado de desenvolvimento.
            <br />
            Sem conexão com os cadastros de produção.
          </p>
        </form>
      </main>
    </div>
  );
}
function Modal({
  title,
  children,
  onClose,
  closeDisabled = false,
}: {
  title: string;
  children: React.ReactNode;
  onClose: () => void;
  closeDisabled?: boolean;
}) {
  const ref = React.useRef<HTMLDialogElement>(null);
  useEffect(() => {
    ref.current?.showModal();
  }, []);
  return (
    <dialog ref={ref} onCancel={(e) => { e.preventDefault(); if (!closeDisabled) onClose(); }}>
      <div className="modal-head">
        <h2>{title}</h2>
        <button className="icon" aria-label="Fechar" onClick={onClose} disabled={closeDisabled}>
          <X />
        </button>
      </div>
      {children}
    </dialog>
  );
}
function FilterEditor({
  rows,
  setRows,
  mode,
  setMode,
}: {
  rows: Dict[];
  setRows: (v: Dict[]) => void;
  mode: string;
  setMode: (v: string) => void;
}) {
  const opts = [
    "id",
    "name",
    "document",
    "phone",
    "kind",
    "number",
    "email",
    "city",
    "state",
    "street",
    "postal_code",
    "birth_date",
    "age",
    "sex",
    "platform",
    "username",
    "is_whatsapp",
    "valid",
    "source_id",
    "code",
  ];
  return (
    <div className="filter-editor">
      <div className="between">
        <span className="label">Combinar filtros</span>
        <select
          aria-label="Combinação dos filtros"
          value={mode}
          onChange={(e) => setMode(e.target.value)}
        >
          <option value="and">Todos os critérios (E)</option>
          <option value="or">Qualquer critério (OU)</option>
          <option value="item">Mesmo telefone, endereço ou item</option>
        </select>
      </div>
      {rows.map((r, i) =>
        r.group ? (
          <div className="filter-group" key={i}>
            <div className="between">
              <span className="label">Grupo de critérios</span>
              <button
                type="button"
                className="icon"
                aria-label="Remover grupo"
                onClick={() => setRows(rows.filter((_, n) => n !== i))}
              >
                <X size={17} />
              </button>
            </div>
            <FilterEditor
              rows={r.children}
              mode={r.group}
              setRows={(children) =>
                setRows(rows.map((x, n) => (n === i ? { ...x, children } : x)))
              }
              setMode={(group) =>
                setRows(rows.map((x, n) => (n === i ? { ...x, group } : x)))
              }
            />
          </div>
        ) : (
          <div className="filter-row" key={i}>
            <select
              aria-label="Campo"
              value={r.field}
              onChange={(e) =>
                setRows(
                  rows.map((x, n) =>
                    n === i ? { ...x, field: e.target.value } : x,
                  ),
                )
              }
            >
              {opts.map((k) => (
                <option key={k} value={k}>
                  {labels[k] || k}
                </option>
              ))}
            </select>
            <select
              aria-label="Operador"
              value={r.op}
              onChange={(e) =>
                setRows(
                  rows.map((x, n) =>
                    n === i ? { ...x, op: e.target.value } : x,
                  ),
                )
              }
            >
              <option value="eq">É igual a</option>
              <option value="contains">Contém</option>
              <option value="prefix">Começa com</option>
              <option value="in">Está na lista</option>
              <option value="range">Entre</option>
              <option value="is_null">Desconhecido</option>
            </select>
            <input
              aria-label="Valor do filtro"
              placeholder={
                r.op === "range"
                  ? "mínimo, máximo"
                  : r.op === "in"
                    ? "valor, outro valor"
                    : "Valor"
              }
              value={r.value}
              onChange={(e) =>
                setRows(
                  rows.map((x, n) =>
                    n === i ? { ...x, value: e.target.value } : x,
                  ),
                )
              }
            />
            <button
              type="button"
              className="icon"
              aria-label="Remover filtro"
              onClick={() => setRows(rows.filter((_, n) => n !== i))}
            >
              <X size={17} />
            </button>
          </div>
        ),
      )}
      <button
        type="button"
        className="text-button"
        onClick={() =>
          setRows([...rows, { field: "city", op: "eq", value: "" }])
        }
      >
        <Plus size={16} />
        Adicionar filtro
      </button>
      <button
        type="button"
        className="text-button"
        onClick={() =>
          setRows([
            ...rows,
            { group: "or", children: [{ field: "city", op: "eq", value: "" }] },
          ])
        }
      >
        <Plus size={16} />
        Adicionar grupo
      </button>
    </div>
  );
}
function filtersOf(rows: Dict[], mode: string): Dict {
  const convert = (field: string, input: string): any => {
    const v = input.trim();
    if (field === "age") return v === "" ? null : Number(v);
    if (["valid", "is_whatsapp"].includes(field)) {
      if (["true", "sim"].includes(v.toLowerCase())) return true;
      if (["false", "não", "nao"].includes(v.toLowerCase())) return false;
      if (v.toLowerCase() === "null") return null;
    }
    return v;
  };
  const list: Dict[] = [];
  for (const r of rows) {
    if (r.group) {
      const child = filtersOf(r.children, r.group);
      if (Object.keys(child).length) list.push(child);
    } else if (r.value !== "" || r.op === "is_null") {
      list.push({
        field: r.field,
        op: r.op,
        value:
          r.op === "is_null"
            ? true
            : ["in", "range"].includes(r.op)
              ? r.value.split(",").map((s: string) => convert(r.field, s))
              : convert(r.field, r.value),
      });
    }
  }
  return list.length
    ? mode === "item"
      ? { item: { and: list } }
      : { [mode]: list }
    : {};
}
function editorRow(node: Dict): Dict {
  if (node.item) return { group: "item", children: [editorRow(node.item)] };
  if (node.and || node.or)
    return {
      group: node.and ? "and" : "or",
      children: (node.and || node.or).map(editorRow),
    };
  return {
    field: node.field,
    op: node.op || "eq",
    value: Array.isArray(node.value)
      ? node.value.map((v: any) => (v === null ? "" : String(v))).join(",")
      : node.value === null
        ? "null"
        : String(node.value ?? ""),
  };
}
function App() {
  const [sortSelection, setSortSelection] = useState<SortSelection>(initialSort);
  const [sortFields, setSortFields] = useState<SortField[]>([]);
  function changeSort(value: SortSelection) {
    loadRevision.current++;
    setSortSelection(value);
    setOffset(0);
    setSelected(new Set());
  }
  const [fieldDefinitions, setFieldDefinitions] = useState<Dict[]>([]);
  const loadRevision = React.useRef(0);
  const previousUser = React.useRef<string | null | undefined>(undefined);
  const [saved, setSaved] = useState<Dict[]>([]),
    [includeInvalid, setIncludeInvalid] = useState(false);
  const [relations, setRelations] = useState<Dict | null>(null);
  const [session, setSession] = useState<Dict | null>(null),
    [ready, setReady] = useState(false),
    [view, setView] = useState("people"),
    [stats, setStats] = useState<Dict>({}),
    [rows, setRows] = useState<Dict[]>([]),
    [total, setTotal] = useState(0),
    [offset, setOffset] = useState(0),
    [query, setQuery] = useState(""),
    [filterRows, setFilterRows] = useState<Dict[]>([]),
    [filterMode, setFilterMode] = useState("and"),
    [showFilters, setShowFilters] = useState(false),
    [selected, setSelected] = useState<Set<string>>(new Set()),
    [detail, setDetail] = useState<Dict | null>(null),
    [detailTab, setDetailTab] = useState("items"),
    [modal, setModal] = useState(""),
    [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [busy, setBusy] = useState(false),
    [jobs, setJobs] = useState<Dict[]>([]),
    [sources, setSources] = useState<Dict[]>([]),
    [adminRows, setAdminRows] = useState<Dict[]>([]),
    [adminTab, setAdminTab] = useState("users"),
    [mobile, setMobile] = useState(false),
    [dark, setDark] = useState(localStorage.getItem("theme") === "dark");
  const [form, setForm] = useState<Dict>({}),
    [kind, setKind] = useState("identity"),
    [source, setSource] = useState("manual"),
    [reason, setReason] = useState(""),
    [bulkText, setBulkText] = useState(""),
    [bulkField, setBulkField] = useState("name"),
    [bulkDocumentType, setBulkDocumentType] = useState("CPF"),
    [bulkName, setBulkName] = useState(""),
    [bulkUpload, setBulkUpload] = useState<Dict | null>(null),
    [bulkType, setBulkType] = useState("person"),
    [bulkMode, setBulkMode] = useState("eq"),
    [allRecords, setAllRecords] = useState(false);
  type AdminAction = { name: string; run: () => Promise<void>; generation: number; csrf: string };
  const pendingAdminAction = React.useRef<AdminAction | null>(null);
  const [stepUpOpen, setStepUpOpen] = useState(false);
  const [stepUpCode, setStepUpCode] = useState("");
  const [stepUpError, setStepUpError] = useState("");
  const [stepUpBusy, setStepUpBusy] = useState(false);
  const rotationAttempt = React.useRef<{ id: string; key: string; grace: number; generation: number; csrf: string } | null>(null);
  const [rotationBusy, setRotationBusy] = useState(false);
  const [rotationUncertain, setRotationUncertain] = useState(false);
  const [rotationError, setRotationError] = useState("");
  useEffect(() => {
    pendingAdminAction.current = null;
    setStepUpOpen(false);
    setStepUpCode("");
    setStepUpError("");
    rotationAttempt.current = null;
    setRotationError("");
    setRotationUncertain(false);
    return () => { pendingAdminAction.current = null; rotationAttempt.current = null; };
  }, [session?.user.id, session?.csrf]);
  useEffect(() => {
    api("/auth/me")
      .then((j) => {
        csrf = j.csrf;
        setSession(j);
      })
      .catch(() => {})
      .finally(() => setReady(true));
  }, []);
  useEffect(() => {
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    localStorage.setItem("theme", dark ? "dark" : "light");
  }, [dark]);
  function fail(e: any) {
    setError(e.message || String(e));
    setBusy(false);
  }
  function cancelStepUp() {
    pendingAdminAction.current = null;
    setStepUpOpen(false);
    setStepUpCode("");
    setStepUpError("");
    setStepUpBusy(false);
    setBusy(false);
  }
  async function runAdminAction(name: string, run: () => Promise<void>) {
    const action = { name, run, generation: accessGeneration, csrf };
    setBusy(true);
    setError("");
    try {
      await run();
    } catch (e) {
      if (e instanceof ApiError && e.code === "RECENT_TOTP_REQUIRED" &&
          action.generation === accessGeneration && action.csrf === csrf) {
        pendingAdminAction.current = action;
        setStepUpCode("");
        setStepUpError("");
        setStepUpOpen(true);
      } else fail(e);
    } finally {
      setBusy(false);
    }
  }
  async function confirmStepUp(e: React.FormEvent) {
    e.preventDefault();
    const action = pendingAdminAction.current;
    if (!action || stepUpBusy) return;
    setStepUpBusy(true);
    setStepUpError("");
    const code = stepUpCode;
    setStepUpCode("");
    try {
      await api("/auth/step-up", "POST", { code });
      if (pendingAdminAction.current !== action || action.generation !== accessGeneration || action.csrf !== csrf) return;
      pendingAdminAction.current = null;
      setStepUpOpen(false);
      await runAdminAction(action.name, action.run);
    } catch (e) {
      if (pendingAdminAction.current === action)
        setStepUpError(e instanceof Error ? e.message : String(e));
    } finally {
      setStepUpBusy(false);
    }
  }
  async function submitRotation(recover = false) {
    if (rotationBusy) return;
    const grace = Number(form.grace_seconds);
    if (!rotationAttempt.current || (!recover && !rotationUncertain && rotationAttempt.current.grace !== grace))
      rotationAttempt.current = { id: form.id, key: crypto.randomUUID(), grace, generation: accessGeneration, csrf };
    const attempt = rotationAttempt.current;
    const code = form.otp;
    setForm({ ...form, otp: "" });
    setRotationBusy(true);
    setRotationError("");
    try {
      const result = await api("/admin/api-keys/" + attempt.id + "/rotate", "POST",
        { grace_seconds: attempt.grace, ...(!recover ? { otp: code } : {}) },
        { "Idempotency-Key": attempt.key });
      if (rotationAttempt.current !== attempt || attempt.generation !== accessGeneration || attempt.csrf !== csrf) return;
      rotationAttempt.current = null;
      setRotationUncertain(false);
      setForm(result);
      setModal("key-result");
      await load();
    } catch (e) {
      if (rotationAttempt.current !== attempt) return;
      const uncertain = !(e instanceof ApiError) || e.status >= 500;
      setRotationUncertain(uncertain);
      setRotationError(uncertain ? "A resposta não chegou. Recupere o resultado da mesma operação antes de tentar outra rotação." : e.message);
    } finally {
      setRotationBusy(false);
    }
  }
  function currentFilters() {
    const f: any = filtersOf(filterRows, filterMode);
    if (query.trim())
      return Object.keys(f).length
        ? { and: [f, { field: "name", op: "prefix", value: query.trim() }] }
        : { field: "name", op: "prefix", value: query.trim() };
    return f;
  }
  async function load() {
    if (!session) return;
    const revision = ++loadRevision.current;
    setBusy(true);
    try {
      const [s, src, definitions, catalog] = await Promise.all([
        api("/stats"),
        api("/admin/sources"),
        api("/admin/fields"),
        api("/search/catalog"),
      ]);
      if (revision !== loadRevision.current) return;
      setStats(s);
      setSources(src.items);
      setFieldDefinitions(definitions.items);
      setSortFields(catalog.sorting?.fields || legacySortFields);
      const requestSort = sortForServer(sortSelection, Boolean(catalog.sorting));
      if (requestSort !== sortSelection) setSortSelection(requestSort);
      if (["people", "companies"].includes(view)) {
        const j = await api("/" + view + "/search", "POST", {
          filters: currentFilters(),
          limit: 25,
          offset,
          ...requestSort,
          include_invalid: includeInvalid,
        });
        if (revision !== loadRevision.current) return;
        setRows(j.items);
        setTotal(j.total);
      }
      if (view === "saved") {
        const j = await api("/saved-searches");
        if (revision === loadRevision.current) setSaved(j.items);
      }
      if (view === "bulk") {
        const j = await api("/bulk-queries");
        setJobs(j.items.reverse());
      }
      if (view === "admin" || view === "keys") {
        const j = await api("/admin/" + (view === "keys" ? "api-keys" : adminTab));
        if (revision !== loadRevision.current) return;
        setAdminRows(j.items);
      }
      if (view === "audit") {
        const j = await api("/audit");
        if (revision !== loadRevision.current) return;
        setAdminRows(j.items);
      }
    } catch (e) {
      fail(e);
    } finally {
      if (revision === loadRevision.current) setBusy(false);
    }
  }
  useEffect(() => {
    const id = setTimeout(load, 250);
    return () => {
      clearTimeout(id);
      loadRevision.current++;
    };
  }, [
    session,
    view,
    offset,
    query,
    sortSelection,
    filterRows,
    filterMode,
    includeInvalid,
    adminTab,
  ]);
  useEffect(() => {
    if (view !== "bulk" || !session) return;
    const id = setInterval(
      () =>
        api("/bulk-queries")
          .then((j) => setJobs(j.items.reverse()))
          .catch(fail),
      3000,
    );
    return () => clearInterval(id);
  }, [view, session]);
  useEffect(() => {
    const userId = session?.user.id ?? null;
    if (previousUser.current === undefined) {
      previousUser.current = userId;
      return;
    }
    if (previousUser.current === userId) return;
    previousUser.current = userId;
    accessGeneration++;
    loadRevision.current++;
    setRows([]);
    setDetail(null);
    setRelations(null);
    setJobs([]);
    setAdminRows([]);
    setSaved([]);
    setFieldDefinitions([]);
    setSelected(new Set());
    setForm({});
    setModal("");
    setQuery("");
    setFilterRows([]);
    setFilterMode("and");
    setIncludeInvalid(false);
    setSortSelection(initialSort());
    setBulkText("");
    setBulkUpload(null);
    setNotice("");
    setError("");
    setView("people");
    setOffset(0);
  }, [session?.user.id]);
  const can = (p: string) => session?.user.permissions.includes(p);
  function navigate(v: string) {
    if (v !== view && ["keys", "admin", "audit"].includes(v)) { setAdminRows([]); loadRevision.current++; }
    if (v === "keys") setAdminTab("api-keys");
    setView(v);
    setOffset(0);
    setSelected(new Set());
    setError("");
    setMobile(false);
  }
  function openAdd(existing = false) {
    setKind(existing ? "phone" : "identity");
    setForm({});
    setReason("");
    setModal(existing ? "item" : "entity");
  }
  async function saveItem(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      const typ =
        detail && modal === "item"
          ? detail.entity_type
          : view === "companies"
            ? "company"
            : "person";
      const collection = typ === "company" ? "companies" : "people";
      const body: any = {
        entity_type: typ,
        source_id: source,
        observed_at: new Date().toISOString(),
        reason,
        items: [
          {
            kind,
            value:
              kind === "custom" ? customPayload(form, fieldDefinitions) : form,
          },
        ],
      };
      if (modal === "item") body.entity_id = detail?.id;
      const j = await api("/" + collection + "/enrich", "POST", body, {
        "Idempotency-Key": crypto.randomUUID(),
      });
      setDetail(j);
      setModal("");
      setNotice("Informação registrada com origem e histórico.");
      await load();
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  }
  useEffect(() => {
    if (!detail || detailTab !== "relationships") return;
    let active = true;
    setRelations(null);
    api(
      `/${detail.entity_type === "company" ? "companies" : "people"}/${detail.id}/relationships`,
    )
      .then((j) => {
        if (active) setRelations(j);
      })
      .catch((e) => {
        if (active) fail(e);
      });
    return () => {
      active = false;
    };
  }, [detail?.id, detail?.version, detailTab]);
  async function setFlag(item: Dict, flag: string, value: any) {
    try {
      const collection =
        detail?.entity_type === "company" ? "companies" : "people";
      const j = await api(
        `/${collection}/${detail?.id}/items/${item.id}`,
        "PATCH",
        {
          source_id: source,
          observed_at: new Date().toISOString(),
          reason: "Atualização pelo painel",
          flags: { [flag]: value },
        },
        { "If-Match": String(item.version) },
      );
      setDetail(j);
      setNotice("Verificação registrada. Histórico preservado.");
    } catch (e) {
      fail(e);
    }
  }
  async function startBulk(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      const j = await api(
        "/bulk-queries",
        "POST",
        {
          name: bulkName || "Consulta em massa",
          entity_type: bulkType,
          text: bulkText,
          upload_id: bulkUpload?.id,
          input_field: bulkField,
          document_type: bulkDocumentType,
          match_mode: bulkMode,
          filters: filtersOf(filterRows, filterMode),
          all_records: allRecords,
          ...sortSelection,
          format: "xlsx",
          include_invalid: includeInvalid,
        },
        { "Idempotency-Key": crypto.randomUUID() },
      );
      setNotice("Consulta recebida. Você pode acompanhar abaixo.");
      setJobs([j, ...jobs]);
      setBulkText("");
      setBulkUpload(null);
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  }
  async function createAdmin(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    try {
      const body: any = { ...form };
      if (adminTab === "fields") {
        body.multiple = form.multiple ?? true;
        body.scope = form.scope || "both";
        body.options =
          form.type === "enum"
            ? String(form.optionsText || "")
                .split("\n")
                .map((v) => v.trim())
                .filter(Boolean)
            : [];
        delete body.optionsText;
      }
      if (adminTab === "users") {
        body.permissions = ["read", "enrich", "validate", "export"];
        body.role = "user";
      }
      if (adminTab === "api-keys") {
        body.scopes = ["read", "enrich", "validate", "export"];
        body.sources = [source];
      }
      const submit = async () => {
        const j = await api(
        "/admin/" + (adminTab === "users" ? "invitations" : adminTab),
        "POST",
        body,
      );
      if (j.activation_token) {
        setForm({
          activation_link: location.origin + "/#activate=" + j.activation_token,
          expires_at: j.expires_at,
        });
        setModal("invitation-result");
        await load();
        return;
      }
      if (j.key) {
        setForm({ key: j.key });
        setModal("key-result");
      } else {
        setModal("");
        setNotice("Cadastro criado.");
      }
      await load();
      };
      if (adminTab === "users") await runAdminAction("Criar convite para " + body.username, submit);
      else await submit();
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  }
  if (!ready) return <div className="loading">Carregando acesso…</div>;
  if (!session) return <Login onLogin={setSession} />;
  const title = (
    {
      people: "Pessoas",
      companies: "Empresas",
      bulk: "Consulta em massa",
      imports: "Importações",
      saved: "Pesquisas salvas",
      admin: "Administração",
      keys: "Minhas chaves",
      audit: "Auditoria",
    } as Dict
  )[view];
  return (
    <div className="shell">
      <aside className={"sidebar " + (mobile ? "open" : "")}>
        <a
          className="brand"
          href="#"
          onClick={(e) => {
            e.preventDefault();
            navigate("people");
          }}
        >
          <span className="mark">B</span> BIG BASE
        </a>
        <div className="workspace-label">CADASTRO UNIFICADO</div>
        <nav>
          {[
            ["people", Users, "Pessoas"],
            ["companies", Building2, "Empresas"],
            ["saved", Search, "Pesquisas salvas"],
            ["bulk", FileSpreadsheet, "Consulta em massa"],
            ["imports", Layers3, "Importações"],
            ["keys", KeyRound, "Minhas chaves"],
            ["audit", History, "Auditoria"],
            ["admin", ShieldCheck, "Administração"],
          ]
            .filter(
              ([id]) =>
                !["admin", "audit"].includes(String(id)) || can("admin"),
            )
            .filter(([id]) => id !== "bulk" || can("export"))
            .filter(([id]) => id !== "imports" || can("enrich"))
            .filter(([id]) => id !== "keys" || !can("admin"))
            .map(([id, Icon, label]: any) => (
              <button
                key={id}
                className={view === id ? "active" : ""}
                onClick={() => navigate(id)}
              >
                <Icon size={20} />
                {label}
                {view === id && <ChevronRight size={15} />}
              </button>
            ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="env-dot" /> Desenvolvimento isolado
          <p>Somente dados de teste</p>
        </div>
        <div className="user">
          <span className="avatar">
            {session.user.username[0].toUpperCase()}
          </span>
          <div>
            <strong>{session.user.username}</strong>
            <small>
              {session.user.role === "admin" ? "Administrador" : "Usuário"}
            </small>
          </div>
          <button
            className="icon"
            title="Sair"
            onClick={() =>
              api("/auth/logout", "POST")
                .then(() => {
                  csrf = "";
                  setSession(null);
                })
                .catch(fail)
            }
          >
            <LogOut size={18} />
          </button>
        </div>
      </aside>
      <div className="workspace">
        <header className="topbar">
          <button
            className="icon mobile-only"
            aria-label="Abrir menu"
            onClick={() => setMobile(!mobile)}
          >
            <Menu />
          </button>
          <span>
            Workspace <ChevronRight size={14} /> <strong>{title}</strong>
          </span>
          <div>
            <span className="secure">
              <ShieldCheck size={15} /> Acesso protegido
            </span>
            <button
              className="icon"
              aria-label="Alternar tema"
              onClick={() => setDark(!dark)}
            >
              {dark ? <Sun size={20} /> : <Moon size={20} />}
            </button>
          </div>
        </header>
        <main>
          <div className="page-head">
            <div>
              <span className="eyebrow">
                BIG BASE / {view === "bulk" ? "CONSULTAS" : "CADASTROS"}
              </span>
              <h1>{title}</h1>
              <p className="muted">
                {view === "bulk"
                  ? "Envie critérios, acompanhe o preparo e baixe os dados completos."
                  : view === "imports"
                    ? "Envie cadastros em lote e acompanhe o resultado de cada entrada."
                    : view === "people" || view === "companies"
                      ? "Consulte, agregue informações e acompanhe cada origem."
                      : view === "audit"
                        ? "Registro de acessos e operações, preservado por evento."
                        : "Gerencie acessos, fontes e definições do cadastro."}
              </p>
            </div>
            {["people", "companies"].includes(view) && can("enrich") && (
              <button className="primary" onClick={() => openAdd()}>
                <Plus size={18} />
                Novo cadastro
              </button>
            )}
          </div>
          {error && (
            <div className="error" role="alert">
              <AlertCircle size={18} />
              {error}
              <button
                className="icon"
                aria-label="Fechar erro"
                onClick={() => setError("")}
              >
                <X size={16} />
              </button>
            </div>
          )}
          {notice && (
            <div className="notice" role="status">
              <Check size={18} />
              {notice}
              <button
                className="icon"
                aria-label="Fechar aviso"
                onClick={() => setNotice("")}
              >
                <X size={16} />
              </button>
            </div>
          )}
          {["people", "companies"].includes(view) && (
            <>
              <div className="stats">
                {[
                  [Users, "Pessoas", stats.people],
                  [Building2, "Empresas", stats.companies],
                  [Layers3, "Informações", stats.items],
                  [History, "Observações rastreadas", stats.observations],
                ].map(([Icon, label, count]: any) => (
                  <div className="stat" key={label}>
                    <div>
                      <span>{label}</span>
                      <Icon size={19} />
                    </div>
                    <strong>{(count || 0).toLocaleString("pt-BR")}</strong>
                    <small>Ambiente de desenvolvimento</small>
                  </div>
                ))}
              </div>
              <section className="card">
                <div className="toolbar">
                  <div className="search">
                    <Search size={19} />
                    <input
                      aria-label="Pesquisar por nome"
                      placeholder="Pesquisar por nome ou início do nome…"
                      value={query}
                      onChange={(e) => {
                        setQuery(e.target.value);
                        setOffset(0);
                      }}
                    />
                  </div>
                  <button
                    className={"secondary " + (showFilters ? "chosen" : "")}
                    onClick={() => setShowFilters(!showFilters)}
                  >
                    <SlidersHorizontal size={17} />
                    Filtros{" "}
                    {filterRows.length > 0 && <span>{filterRows.length}</span>}
                  </button>
                  <button
                    className="icon"
                    aria-label="Atualizar resultados"
                    onClick={load}
                  >
                    <RefreshCw size={18} className={busy ? "spin" : ""} />
                  </button>
                </div>
                {showFilters && (
                  <FilterEditor
                    rows={filterRows}
                    setRows={setFilterRows}
                    mode={filterMode}
                    setMode={setFilterMode}
                  />
                )}
                <SortEditor value={sortSelection} fields={sortFields} onChange={changeSort} />
                <div className="list-info">
                  <span>
                    <strong>{total.toLocaleString("pt-BR")}</strong> cadastros
                    encontrados{" "}
                    {selected.size > 0 && ` · ${selected.size} selecionados`}
                  </span>
                  <div>
                    {can("export") && (
                      <button
                        className="text-button"
                        onClick={() => {
                          setBulkType(
                            view === "companies" ? "company" : "person",
                          );
                          if (selected.size) {
                            setFilterRows([
                              {
                                field: "id",
                                op: "in",
                                value: [...selected].join(","),
                              },
                            ]);
                            setFilterMode("and");
                          }
                          setBulkText(selected.size ? "" : query.trim());
                          setBulkField("name");
                          setBulkMode("prefix");
                          setBulkUpload(null);
                          setAllRecords(!Object.keys(currentFilters()).length);
                          navigate("bulk");
                        }}
                      >
                        <Download size={16} />
                        Exportar
                      </button>
                    )}
                  </div>
                </div>
                <div className="toolbar">
                  <label className="check">
                    <input
                      type="checkbox"
                      checked={includeInvalid}
                      onChange={(e) => {
                        setIncludeInvalid(e.target.checked);
                        setOffset(0);
                      }}
                    />
                    Incluir informações invalidadas
                  </label>
                  <button
                    className="text-button"
                    onClick={() => {
                      setForm({ name: "" });
                      setModal("save-search");
                    }}
                  >
                    Salvar pesquisa
                  </button>
                </div>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>
                          <input
                            type="checkbox"
                            aria-label="Selecionar página"
                            checked={
                              rows.length > 0 &&
                              rows.every((r) => selected.has(r.id))
                            }
                            onChange={(e) =>
                              setSelected(
                                e.target.checked
                                  ? new Set(rows.map((r) => r.id))
                                  : new Set(),
                              )
                            }
                          />
                        </th>
                        <th>CADASTRO</th>
                        <th>DOCUMENTOS</th>
                        <th>INFORMAÇÕES</th>
                        <th>ÚLTIMA ATUALIZAÇÃO</th>
                        <th />
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map((r) => (
                        <tr key={r.id}>
                          <td>
                            <input
                              type="checkbox"
                              aria-label={"Selecionar " + r.name}
                              checked={selected.has(r.id)}
                              onChange={() => {
                                const s = new Set(selected);
                                s.has(r.id) ? s.delete(r.id) : s.add(r.id);
                                setSelected(s);
                              }}
                            />
                          </td>
                          <td>
                            <button
                              className="entity-link"
                              onClick={() => {
                                setDetail(r);
                                setDetailTab("items");
                              }}
                            >
                              <span className="entity-avatar">{r.name[0]}</span>
                              <span>
                                <strong>{r.name}</strong>
                                <small>
                                  {r.id.slice(0, 8)} · versão {r.version}
                                </small>
                              </span>
                            </button>
                          </td>
                          <td>
                            {r.items
                              .filter((i: Dict) => i.kind === "document")
                              .map((i: Dict) => (
                                <div key={i.id} className="document-number">
                                  {i.value.type} {i.value.number}
                                </div>
                              ))}
                            {!r.items.some(
                              (i: Dict) => i.kind === "document",
                            ) && <span className="muted">Não informado</span>}
                          </td>
                          <td>
                            <span className="count-badge">
                              {r.items.length} itens
                            </span>
                            <small className="block muted">
                              {r.observations.length} observações
                            </small>
                          </td>
                          <td>{date(r.updated_at)}</td>
                          <td>
                            <button
                              className="icon"
                              aria-label={"Abrir " + r.name}
                              onClick={() => setDetail(r)}
                            >
                              <ArrowUpRight size={18} />
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {!rows.length && (
                    <div className="empty">
                      <Database size={32} />
                      <h3>
                        {busy
                          ? "Buscando cadastros…"
                          : "Nenhum cadastro encontrado"}
                      </h3>
                      <p>
                        {query || filterRows.length
                          ? "Revise os critérios de pesquisa."
                          : "Comece com um cadastro de teste para explorar o fluxo."}
                      </p>
                      {can("enrich") && !busy && (
                        <button className="secondary" onClick={() => openAdd()}>
                          <Plus size={16} />
                          Adicionar cadastro
                        </button>
                      )}
                    </div>
                  )}
                </div>
                <footer className="pagination">
                  <span>
                    {total ? offset + 1 : 0}–{Math.min(offset + 25, total)} de{" "}
                    {total}
                  </span>
                  <div>
                    <button
                      className="secondary"
                      disabled={offset === 0}
                      onClick={() => setOffset(Math.max(0, offset - 25))}
                    >
                      Anterior
                    </button>
                    <button
                      className="secondary"
                      disabled={offset + 25 >= total}
                      onClick={() => setOffset(offset + 25)}
                    >
                      Próxima
                    </button>
                  </div>
                </footer>
              </section>
              <p className="footnote">
                <History size={14} />
                Cada atualização preserva a informação anterior e registra sua
                origem.
              </p>
            </>
          )}
          {view === "bulk" && (
            <div className="bulk-grid">
              <form className="card form-card" onSubmit={startBulk}>
                <div className="section-title">
                  <FileSpreadsheet />
                  <h2>Nova consulta</h2>
                </div>
                <label>
                  Nome do trabalho
                  <input
                    value={bulkName}
                    placeholder="Ex.: Contatos da região Sul"
                    onChange={(e) => setBulkName(e.target.value)}
                  />
                </label>
                <div className="two">
                  <label>
                    Cadastros
                    <select
                      value={bulkType}
                      onChange={(e) => setBulkType(e.target.value)}
                    >
                      <option value="person">Pessoas</option>
                      <option value="company">Empresas</option>
                    </select>
                  </label>
                  <label>
                    Tipo da lista
                    <select
                      value={bulkField}
                      onChange={(e) => setBulkField(e.target.value)}
                    >
                      <option value="name">Nomes</option>
                      <option value="document">Documentos</option>
                      <option value="email">Emails</option>
                      <option value="phone">Telefones</option>
                      <option value="username">Usernames</option>
                    </select>
                  </label>
                </div>
                {bulkField === "document" && (
                  <label>
                    Tipo de documento
                    <select
                      value={bulkDocumentType}
                      onChange={(e) => setBulkDocumentType(e.target.value)}
                    >
                      <option value="CPF">CPF</option>
                      <option value="CNPJ">CNPJ</option>
                      <option value="RG">RG</option>
                      <option value="PASSPORT">Passaporte</option>
                    </select>
                  </label>
                )}
                <label>
                  Informações para consultar
                  <textarea
                    rows={5}
                    placeholder="Separe os valores por vírgula ou quebra de linha"
                    value={bulkText}
                    onChange={(e) => setBulkText(e.target.value)}
                  />
                </label>
                <label>
                  Correspondência
                  <select
                    value={bulkMode}
                    onChange={(e) => setBulkMode(e.target.value)}
                  >
                    <option value="eq">Exata</option>
                    <option value="prefix">Começa com</option>
                    <option value="contains">Contém</option>
                  </select>
                </label>
                <FilterEditor
                  rows={filterRows}
                  setRows={setFilterRows}
                  mode={filterMode}
                  setMode={setFilterMode}
                />
                <label className="check">
                  <span>
                    Ou envie uma lista CSV/XLSX (uma entrada por célula, sem
                    cabeçalho):
                  </span>
                  <input
                    type="file"
                    accept=".csv,.xlsx"
                    aria-label="Enviar lista CSV ou XLSX"
                    onChange={async (e) => {
                      const file = e.target.files?.[0];
                      if (!file) return;
                      setBusy(true);
                      try {
                        const data = new FormData();
                        data.append("file", file);
                        const response = await fetch(
                          "/api/v1/bulk-queries/uploads",
                          {
                            method: "POST",
                            credentials: "same-origin",
                            headers: { "X-CSRF-Token": csrf },
                            body: data,
                          },
                        );
                        const j = parseApiJson(await response.text());
                        if (!response.ok) throw new Error(j.detail);
                        setBulkUpload(j);
                        setBulkText("");
                        setNotice(
                          `${j.count} entradas recebidas. Confira o tipo da lista antes de consultar.`,
                        );
                      } catch (error) {
                        fail(error);
                      } finally {
                        setBusy(false);
                      }
                    }}
                  />
                </label>
                {bulkUpload && (
                  <div className="info">
                    Lista recebida: {bulkUpload.count} entradas.{" "}
                    <button
                      type="button"
                      className="text-button"
                      onClick={() => setBulkUpload(null)}
                    >
                      Remover lista
                    </button>
                  </div>
                )}
                <label className="check">
                  <input
                    type="checkbox"
                    checked={allRecords}
                    onChange={(e) => setAllRecords(e.target.checked)}
                  />
                  Selecionar todos os cadastros quando não houver lista ou
                  filtros
                </label>
                <SortEditor value={sortSelection} fields={sortFields} onChange={changeSort} />
                <label className="check"><input type="checkbox" checked={includeInvalid} onChange={e => { setIncludeInvalid(e.target.checked); setSelected(new Set()); setOffset(0); }} />Incluir informações invalidadas na seleção</label>
                <div className="info">
                  <Layers3 size={18} />
                  Entrega XLSX com abas para cadastros, contatos, relações e
                  histórico completo.
                </div>
                <button className="primary wide" disabled={busy}>
                  <Plus size={18} />
                  Preparar consulta
                </button>
              </form>
              <section className="card form-card">
                <div className="section-title">
                  <Clock />
                  <h2>Seus trabalhos</h2>
                </div>
                {!jobs.length && (
                  <div className="empty">
                    <FileSpreadsheet size={28} />
                    <h3>Nenhum trabalho ainda</h3>
                    <p>
                      As consultas continuam preparando mesmo com o painel
                      fechado.
                    </p>
                  </div>
                )}
                {jobs.map((j) => (
                  <article className="job" key={j.id}>
                    <div className="between">
                      <strong>{j.name}</strong>
                      <Badge
                        value={
                          j.status === "completed"
                            ? true
                            : j.status === "failed"
                              ? false
                              : null
                        }
                        label={
                          (
                            {
                              pending: "Pendente",
                              preparing: "Preparando",
                              completed: "Concluído",
                              failed: "Falhou",
                              cancelled: "Cancelado",
                            } as Dict
                          )[j.status]
                        }
                      />
                    </div>
                    <p className="muted">{date(j.created_at)}</p>
                    <div className="between">
                      <span>{j.phase}</span>
                      <strong>{j.progress_percent}%</strong>
                    </div>
                    <progress max={100} value={j.progress_percent} />
                    <small>{j.entity_count} cadastros encontrados</small>
                    {j.error && <p className="error">{j.error}</p>}
                    {j.artifact_expired && (
                      <p className="muted">
                        O arquivo temporário expirou. Os dados do cadastro e o
                        registro da consulta permanecem preservados.
                      </p>
                    )}
                    <div className="job-actions">
                      {j.status === "completed" && !j.artifact_expired && (
                        <a
                          className="secondary"
                          href={
                            "/api/v1/bulk-queries/" + j.id + "/files/result"
                          }
                        >
                          <Download size={16} />
                          {j.artifact === "zip"
                            ? "Baixar ZIP com planilhas completas"
                            : "Baixar XLSX completo"}
                        </a>
                      )}
                      {["pending", "preparing"].includes(j.status) && (
                        <button
                          className="text-button"
                          onClick={() =>
                            api("/bulk-queries/" + j.id + "/cancel", "POST")
                              .then(load)
                              .catch(fail)
                          }
                        >
                          Cancelar
                        </button>
                      )}
                    </div>
                  </article>
                ))}
              </section>
            </div>
          )}
          {view === "saved" && (
            <section className="card">
              <div className="toolbar">
                <h2>Critérios guardados para consultar novamente</h2>
              </div>
              {!saved.length && (
                <div className="empty">
                  Use “Salvar pesquisa” depois de configurar uma consulta.
                </div>
              )}
              {saved.map((search) => (
                <article className="job" key={search.id}>
                  <div className="between">
                    <strong>{search.name}</strong>
                    <Badge
                      label={
                        search.entity_type === "company"
                          ? "Empresas"
                          : "Pessoas"
                      }
                      value={null}
                    />
                  </div>
                  <small>Salva em {date(search.created_at)}</small>
                  <div className="job-actions">
                    <button
                      className="secondary"
                      onClick={() => {
                        setQuery("");
                        setFilterRows(
                          Object.keys(search.filters).length
                            ? [editorRow(search.filters)]
                            : [],
                        );
                        setFilterMode("and");
                        changeSort(search.sorts ? { sorts: search.sorts } : { sort: search.sort || "name", direction: search.direction || "asc" });
                        setIncludeInvalid(search.include_invalid);
                        setShowFilters(true);
                        navigate(
                          search.entity_type === "company"
                            ? "companies"
                            : "people",
                        );
                      }}
                    >
                      Executar pesquisa
                    </button>
                    <button
                      className="text-button"
                      onClick={() =>
                        api("/saved-searches/" + search.id, "PATCH")
                          .then(load)
                          .catch(fail)
                      }
                    >
                      Arquivar
                    </button>
                  </div>
                </article>
              ))}
            </section>
          )}
          {(view === "admin" || view === "keys") && (
            <section className="card">
              {view === "admin" ? <div className="tabs">
                {[
                  ["users", "Usuários"],
                  ["sources", "Fontes"],
                  ["fields", "Campos adicionais"],
                  ["api-keys", "Chaves de API"],
                ].map(([id, label]) => (
                  <button
                    key={id}
                    className={adminTab === id ? "active" : ""}
                    onClick={() => { if (id !== adminTab) { loadRevision.current++; setAdminRows([]); setAdminTab(id); } }}
                  >
                    {label}
                  </button>
                ))}
                <button
                  className="primary"
                  onClick={() => {
                    setForm({ type: "text", scope: "both", multiple: true });
                    setModal("admin");
                  }}
                >
                  <Plus size={16} />
                  Adicionar
                </button>
              </div> : <p className="info">Consulte e renove as chaves das suas integrações. A criação inicial é feita por um administrador.</p>}
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>NOME / USUÁRIO</th>
                      <th>ESTADO</th>
                      <th>DETALHES</th>
                    </tr>
                  </thead>
                  <tbody>
                    {adminRows.map((r) => (
                      <tr key={r.id}>
                        <td>
                          <strong>{r.name || r.username}</strong>
                          <small className="block muted">{r.id}</small>
                        </td>
                        <td>
                          <Badge
                            value={r.active}
                            label={r.active ? "Ativo" : "Inativo"}
                          />
                        </td>
                        <td>
                          {adminTab === "users" ? (
                            <>
                              {r.role} · OTP{" "}
                              {r.otp_enabled ? "configurado" : "pendente"}
                              {r.id !== session.user.id && (
                                <button
                                  className="text-button"
                                  disabled={busy}
                                  onClick={() => {
                                    setForm({ id: r.id, username: r.username, active: !r.active });
                                    setModal("user-status");
                                  }}
                                >
                                  {r.active ? "Desativar acesso" : "Ativar acesso"}
                                </button>
                              )}
                            </>
                          ) : adminTab === "api-keys" ? (
                            <>
                              {(r.scopes || []).join(", ")}{" "}
                              <small className="block muted">Válida até {date(r.expires_at)}</small>
                              {r.status === "transition" && <small className="block muted">Em transição até {date(r.retire_at)}</small>}
                              {r.predecessor_id && <small className="block muted">Substitui {r.predecessor_id}</small>}
                              {r.successor_id && <small className="block muted">Sucessora: {r.successor_id}</small>}
                              {r.can_rotate && <button
                                className="text-button"
                                onClick={() => {
                                  rotationAttempt.current = null;
                                  setRotationError(""); setRotationUncertain(false);
                                  setForm({ id: r.id, name: r.name, grace_seconds: 900, otp: "" });
                                  setModal("key-rotate");
                                }}
                              >Renovar chave</button>}
                              <button
                                className="text-button"
                                disabled={r.status === "revoked"}
                                onClick={() => { setForm({ id: r.id, name: r.name }); setModal("key-revoke"); }}
                              >
                                Revogar cadeia
                              </button>
                            </>
                          ) : adminTab === "fields" ? (
                            <>
                              <span>
                                {fieldTypes[r.type]} · versão {r.version} ·{" "}
                                {r.multiple
                                  ? "múltiplo"
                                  : "principal e alternativas"}
                              </span>
                              <small className="block muted">
                                Busca indexada:{" "}
                                {r.search_state === "ready"
                                  ? "pronta"
                                  : "em preparação"}
                              </small>
                              <button
                                className="text-button"
                                onClick={() => {
                                  setForm({
                                    id: r.id,
                                    name: r.name,
                                    active: r.active,
                                    version: r.version,
                                  });
                                  setModal("field-edit");
                                }}
                              >
                                Editar campo
                              </button>
                              <details>
                                <summary>Histórico de definições</summary>
                                {(r.definition_history || []).map(
                                  (entry: Dict, n: number) => (
                                    <p className="muted" key={n}>
                                      Versão {entry.version}: {entry.name} ·{" "}
                                      {entry.active ? "ativo" : "inativo"}
                                    </p>
                                  ),
                                )}
                              </details>
                            </>
                          ) : (
                            r.type || "Origem de dados"
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {!adminRows.length && (
                  <div className="empty">Nenhum item cadastrado.</div>
                )}
              </div>
            </section>
          )}
          {view === "imports" && (
            <ImportsPanel
              key={session.user.id}
              api={api}
              canEnrich={can("enrich")}
            />
          )}
          {view === "audit" && (
            <section className="card">
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>DATA</th>
                      <th>OPERAÇÃO</th>
                      <th>ATOR</th>
                      <th>REFERÊNCIA</th>
                    </tr>
                  </thead>
                  <tbody>
                    {adminRows.map((r) => (
                      <tr key={r.id}>
                        <td>{date(r.at)}</td>
                        <td>{r.action}</td>
                        <td>{r.actor_id.slice(0, 8)}</td>
                        <td className="mono">{r.target_id}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          )}
        </main>
      </div>
      {detail && (
        <div className="drawer-backdrop" onClick={() => setDetail(null)}>
          <aside
            className="drawer"
            onClick={(e) => e.stopPropagation()}
            role="dialog"
            aria-label="Ficha do cadastro"
            aria-modal="true"
          >
            <div className="drawer-top">
              <span className="eyebrow">FICHA COMPLETA</span>
              <button
                className="icon"
                aria-label="Fechar ficha"
                onClick={() => setDetail(null)}
              >
                <X />
              </button>
            </div>
            <h2>{detail.name}</h2>
            <p className="muted mono">{detail.id}</p>
            <div className="between">
              <Badge value={true} label={"Versão " + detail.version} />
              {can("enrich") && (
                <button className="secondary" onClick={() => openAdd(true)}>
                  <Plus size={16} />
                  Agregar informação
                </button>
              )}
            </div>
            <div className="tabs">
              <button
                className={detailTab === "items" ? "active" : ""}
                onClick={() => setDetailTab("items")}
              >
                Informações ({detail.items.length})
              </button>
              <button
                className={detailTab === "history" ? "active" : ""}
                onClick={() => setDetailTab("history")}
              >
                Histórico ({detail.observations.length})
              </button>
              <button
                className={detailTab === "relationships" ? "active" : ""}
                onClick={() => setDetailTab("relationships")}
              >
                Vínculos
              </button>
            </div>
            {detailTab === "relationships" ? (
              <div>
                {!relations ? (
                  <p>Carregando vínculos…</p>
                ) : (
                  <>
                    {(["outgoing", "incoming"] as const).map((direction) => (
                      <section key={direction}>
                        <h3>
                          {direction === "outgoing"
                            ? "Vínculos informados neste cadastro"
                            : "Vínculos recebidos de outros cadastros"}
                        </h3>
                        {!relations[direction].length && (
                          <p className="muted">Nenhum vínculo registrado.</p>
                        )}
                        {relations[direction].map((r: Dict) => (
                          <article
                            className="item-card"
                            key={r.owner_id + r.item.id}
                          >
                            <strong>
                              {direction === "incoming"
                                ? r.owner_name
                                : r.item.value.target_name ||
                                  r.item.value.target_document ||
                                  r.resolution.target_id ||
                                  "Identidade pendente"}
                            </strong>
                            <p>
                              {direction === "incoming"
                                ? r.inverse_role || r.role
                                : r.role}
                            </p>
                            <Badge value={r.item.flags.valid} />
                            <p className="muted">
                              {r.resolution.status === "resolved"
                                ? "Cadastro relacionado identificado"
                                : "Identificação pendente: " +
                                  r.resolution.status}
                            </p>
                            <small>
                              Origem: {r.item.sources.join(", ")}
                              {r.derived
                                ? " · Relação inversa derivada do vínculo original"
                                : ""}
                            </small>
                          </article>
                        ))}
                      </section>
                    ))}
                  </>
                )}
              </div>
            ) : detailTab === "items" ? (
              detail.items.map((it: Dict) => (
                <article className="item-card" key={it.id}>
                  <div className="between">
                    <strong>
                      {it.kind === "custom"
                        ? fieldDefinitions.find(
                            (d) => d.id === it.value.field_id,
                          )?.name || labels.custom
                        : labels[it.kind]}
                    </strong>
                    <Badge value={it.flags.valid} />
                  </div>
                  <dl>
                    {Object.entries(it.value).map(([k, v]) => (
                      <div key={k}>
                        <dt>{labels[k] || k}</dt>
                        <dd>
                          {it.kind === "phone" &&
                          k === "phone_normalization" &&
                          v &&
                          typeof v === "object" ? (
                            <PhoneNormalizationDetails audit={v as Dict} />
                          ) : k === "classification" ? (
                            (
                              {
                                mobile: "Celular",
                                fixed: "Fixo",
                                other: "Outro serviço",
                                unknown: "Desconhecido",
                              } as Dict
                            )[String(v)] || val(v)
                          ) : k === "usage" ? (
                            (
                              {
                                residential: "Residencial",
                                commercial: "Comercial",
                                unknown: "Não informado",
                              } as Dict
                            )[String(v)] || val(v)
                          ) : (
                            val(v)
                          )}
                          <small className="field-evidence">
                            Fonte:{" "}
                            {it.fields["value." + k]?.source_id ||
                              "Não informada"}{" "}
                            · observado:{" "}
                            {date(it.fields["value." + k]?.observed_at)}
                          </small>
                        </dd>
                      </div>
                    ))}
                  </dl>
                  <div className="item-sources">
                    Origem: {it.sources.join(", ")} · versão {it.version}
                    {it.kind === "custom" && (
                      <>
                        {" "}
                        · definição {it.field_definition_version ?? "pendente"}
                      </>
                    )}
                  </div>
                  {it.notes.map((n: string) => (
                    <p className="info" key={n}>
                      {n}
                    </p>
                  ))}
                  {can("validate") && (
                    <div className="flag-controls">
                      <label>
                        Validade
                        <select
                          value={
                            it.flags.valid === undefined ||
                            it.flags.valid === null
                              ? "null"
                              : String(it.flags.valid)
                          }
                          onChange={(e) =>
                            setFlag(
                              it,
                              "valid",
                              e.target.value === "null"
                                ? null
                                : e.target.value === "true",
                            )
                          }
                        >
                          <option value="null">Não confirmada</option>
                          <option value="true">Válida</option>
                          <option value="false">Inválida</option>
                        </select>
                        <small className="field-evidence">
                          Última observação:{" "}
                          {date(it.fields["flag.valid"]?.observed_at)}
                        </small>
                      </label>
                      {it.kind === "phone" && (
                        <label>
                          WhatsApp
                          <select
                            value={
                              it.flags.is_whatsapp === undefined ||
                              it.flags.is_whatsapp === null
                                ? "null"
                                : String(it.flags.is_whatsapp)
                            }
                            onChange={(e) =>
                              setFlag(
                                it,
                                "is_whatsapp",
                                e.target.value === "null"
                                  ? null
                                  : e.target.value === "true",
                              )
                            }
                          >
                            <option value="null">Não confirmado</option>
                            <option value="true">Sim</option>
                            <option value="false">Não</option>
                          </select>
                          <small className="field-evidence">
                            Última observação:{" "}
                            {date(it.fields["flag.is_whatsapp"]?.observed_at)}
                          </small>
                        </label>
                      )}
                      {[
                        "ownership_confirmed",
                        ...(it.kind === "email" ? ["deliverable"] : []),
                        ...(it.kind === "address"
                          ? ["residence_confirmed"]
                          : []),
                      ].map((flag) => (
                        <label key={flag}>
                          {
                            (
                              {
                                ownership_confirmed:
                                  "Titularidade / vínculo confirmado",
                                deliverable: "Email recebe mensagens",
                                residence_confirmed: "Residência confirmada",
                              } as Dict
                            )[flag]
                          }
                          <select
                            value={String(it.flags[flag] ?? "null")}
                            onChange={(e) =>
                              setFlag(
                                it,
                                flag,
                                e.target.value === "null"
                                  ? null
                                  : e.target.value === "true",
                              )
                            }
                          >
                            <option value="null">Não confirmado</option>
                            <option value="true">Sim</option>
                            <option value="false">Não</option>
                          </select>
                          <small className="field-evidence">
                            Última observação:{" "}
                            {date(it.fields["flag." + flag]?.observed_at)}
                          </small>
                        </label>
                      ))}
                    </div>
                  )}
                  {Object.entries(it.flag_details || {}).map(
                    ([flag, evidence]: [string, any]) =>
                      evidence.applicable === false ? (
                        <p className="info" key={flag}>
                          A verificação de {flag} pertence ao valor anterior. O
                          resultado está preservado no histórico; o novo valor
                          precisa ser verificado.
                        </p>
                      ) : evidence.stale ? (
                        <p className="warning" key={flag}>
                          A verificação de {flag} venceu em{" "}
                          {date(evidence.expires_at)}. Último resultado
                          preservado: {val(evidence.value)}.
                        </p>
                      ) : null,
                  )}
                </article>
              ))
            ) : (
              [...detail.observations].reverse().map((o: Dict) => (
                <article className="timeline" key={o.id}>
                  <span className="timeline-dot" />
                  <div className="between">
                    <strong>{o.path}</strong>
                    <Badge
                      value={o.applied}
                      label={o.applied ? "Aplicado" : "Preservado"}
                    />
                  </div>
                  <p>{val(o.value)}</p>
                  <small className="block">
                    Valor anterior: {val(o.previous)}
                  </small>
                  <small className="block">
                    Entrada recebida: {val(o.input_value)}
                  </small>
                  <small>
                    Fonte: {o.source_id} · observado: {date(o.observed_at)}
                  </small>
                  <small className="block">
                    Recebido: {date(o.received_at)} ·{" "}
                    {o.reason || "Sem motivo adicional"}
                  </small>
                </article>
              ))
            )}
          </aside>
        </div>
      )}
      {["entity", "item"].includes(modal) && (
        <Modal
          title={modal === "entity" ? "Novo cadastro" : "Agregar informação"}
          onClose={() => setModal("")}
        >
          <form onSubmit={saveItem}>
            <label>
              Tipo de informação
              <select
                value={kind}
                onChange={(e) => {
                  setKind(e.target.value);
                  setForm({});
                }}
              >
                {Object.keys(forms).map((k) => (
                  <option key={k} value={k}>
                    {labels[k]}
                  </option>
                ))}
              </select>
            </label>
            {kind === "custom" ? (
              <CustomValueEditor
                form={form}
                setForm={setForm}
                definitions={fieldDefinitions}
                entityType={
                  modal === "item"
                    ? detail?.entity_type
                    : view === "companies"
                      ? "company"
                      : "person"
                }
              />
            ) : (
              <div className="two">
                {(kind === "identity" &&
                (modal === "item"
                  ? detail?.entity_type === "company"
                  : view === "companies")
                  ? [
                      "name",
                      "trade_name",
                      "legal_nature",
                      "opened_at",
                      "registration_status",
                      "company_size",
                    ]
                  : forms[kind]
                ).map((k: string) => (
                  <label key={k}>
                    {labels[k] || k}
                    {kind === "phone" && k === "usage" ? (
                      <select
                        aria-label="Uso"
                        value={form.usage || "unknown"}
                        onChange={(e) =>
                          setForm({ ...form, usage: e.target.value })
                        }
                      >
                        <option value="unknown">Não informado</option>
                        <option value="residential">Residencial</option>
                        <option value="commercial">Comercial</option>
                      </select>
                    ) : (
                      <input
                        required={
                          kind !== "relationship" && forms[kind][0] === k
                        }
                        value={form[k] || ""}
                        onChange={(e) =>
                          setForm({ ...form, [k]: e.target.value })
                        }
                      />
                    )}
                  </label>
                ))}
              </div>
            )}
            <label>
              Origem
              <select
                value={source}
                onChange={(e) => setSource(e.target.value)}
              >
                {sources.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Motivo / observação
              <input
                value={reason}
                onChange={(e) => setReason(e.target.value)}
              />
            </label>
            <p className="info">
              A informação será registrada com a data atual e preservada no
              histórico.
            </p>
            <button className="primary wide" disabled={busy}>
              Salvar informação
            </button>
          </form>
        </Modal>
      )}
      {modal === "save-search" && (
        <Modal title="Salvar pesquisa" onClose={() => setModal("")}>
          <form
            onSubmit={async (e) => {
              e.preventDefault();
              try {
                await api(
                  "/saved-searches",
                  "POST",
                  {
                    name: form.name,
                    filters: currentFilters(),
                    entity_type: view === "companies" ? "company" : "person",
                    ...sortSelection,
                    include_invalid: includeInvalid,
                  },
                  { "Idempotency-Key": crypto.randomUUID() },
                );
                setModal("");
                setNotice("Pesquisa salva para seu usuário.");
              } catch (e) {
                fail(e);
              }
            }}
          >
            <label>
              Nome da pesquisa
              <input
                required
                value={form.name}
                onChange={(e) => setForm({ name: e.target.value })}
              />
            </label>
            <button className="primary wide">Salvar critérios</button>
          </form>
        </Modal>
      )}
      {stepUpOpen && (
        <Modal title="Confirme sua identidade" onClose={cancelStepUp}>
          <p>Use um novo código do Google Authenticator para continuar:</p>
          <p><strong>{pendingAdminAction.current?.name}</strong></p>
          <p className="info">A confirmação vale por até cinco minutos nesta sessão.</p>
          <form onSubmit={confirmStepUp}>
            <label>
              Código atual do autenticador
              <input
                autoFocus
                required
                inputMode="numeric"
                autoComplete="one-time-code"
                pattern="[0-9]{6}"
                maxLength={6}
                value={stepUpCode}
                onChange={(e) => setStepUpCode(e.target.value)}
                disabled={stepUpBusy}
              />
            </label>
            {stepUpError && <p className="error" role="alert">{stepUpError}</p>}
            <button className="primary wide" disabled={stepUpBusy}>
              {stepUpBusy ? "Confirmando…" : "Confirmar e continuar"}
            </button>
            <button type="button" className="text-button" onClick={cancelStepUp}>Cancelar confirmação</button>
          </form>
        </Modal>
      )}
      {modal === "user-status" && !stepUpOpen && (
        <Modal title={form.active ? "Ativar acesso" : "Desativar acesso"} onClose={() => setModal("")}>
          <p>{form.active ? "Ativar" : "Desativar"} o acesso de <strong>{form.username}</strong>?</p>
          {!form.active && <p className="info">As sessões e chaves desse usuário serão revogadas. Os dados e o histórico permanecem disponíveis.</p>}
          <form onSubmit={async (e) => {
            e.preventDefault();
            if (busy) return;
            const change = { ...form };
            await runAdminAction((change.active ? "Ativar acesso de " : "Desativar acesso de ") + change.username, async () => {
              await api("/admin/users/" + change.id, "PATCH", { active: change.active });
              setModal("");
              setNotice(change.active ? "Acesso ativado." : "Acesso desativado e credenciais revogadas.");
              await load();
            });
          }}>
            <button className="primary wide" disabled={busy}>{form.active ? "Confirmar ativação" : "Confirmar desativação"}</button>
          </form>
        </Modal>
      )}
      {modal === "admin" && !stepUpOpen && (
        <Modal
          title={
            "Adicionar " +
            (
              {
                users: "usuário",
                sources: "fonte",
                fields: "campo",
                "api-keys": "chave de API",
              } as Dict
            )[adminTab]
          }
          onClose={() => setModal("")}
        >
          <form onSubmit={createAdmin}>
            {(adminTab === "users"
              ? ["username"]
              : adminTab === "sources"
                ? ["id", "name"]
                : adminTab === "fields"
                  ? ["name"]
                  : ["name", "otp"]
            ).map((k) => (
              <label key={k}>
                {
                  (
                    {
                      username: "Usuário",
                      password: "Senha inicial (mínimo 12 caracteres)",
                      id: "Identificador",
                      name: "Nome",
                      otp: "Novo código do autenticador",
                    } as Dict
                  )[k]
                }
                <input
                  type={k === "password" ? "password" : "text"}
                  required
                  value={form[k] || ""}
                  onChange={(e) => setForm({ ...form, [k]: e.target.value })}
                />
              </label>
            ))}
            {adminTab === "fields" && (
              <>
                <label>
                  Tipo
                  <select
                    aria-label="Tipo"
                    value={form.type}
                    onChange={(e) => setForm({ ...form, type: e.target.value })}
                  >
                    {[
                      "text",
                      "integer",
                      "decimal",
                      "boolean",
                      "date",
                      "enum",
                      "url",
                      "reference",
                    ].map((k) => (
                      <option key={k} value={k}>
                        {fieldTypes[k] || k}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  Aplicar a
                  <select
                    value={form.scope || "both"}
                    onChange={(e) =>
                      setForm({ ...form, scope: e.target.value })
                    }
                  >
                    <option value="both">Pessoas e empresas</option>
                    <option value="person">Pessoas</option>
                    <option value="company">Empresas</option>
                  </select>
                </label>
                <label className="check">
                  <input
                    type="checkbox"
                    checked={form.multiple ?? true}
                    onChange={(e) =>
                      setForm({ ...form, multiple: e.target.checked })
                    }
                  />
                  Permitir vários valores
                </label>
                {form.type === "enum" && (
                  <label>
                    Opções (uma por linha)
                    <textarea
                      required
                      rows={5}
                      value={form.optionsText || ""}
                      onChange={(e) =>
                        setForm({ ...form, optionsText: e.target.value })
                      }
                    />
                  </label>
                )}{" "}
              </>
            )}
            <button className="primary wide" disabled={busy}>
              Criar
            </button>
          </form>
        </Modal>
      )}
      {modal === "field-edit" && (
        <Modal title="Editar campo" onClose={() => setModal("")}>
          <form
            onSubmit={async (e) => {
              e.preventDefault();
              setBusy(true);
              try {
                await api(
                  "/admin/fields/" + form.id,
                  "PATCH",
                  { name: form.name, active: form.active },
                  { "If-Match": String(form.version) },
                );
                setModal("");
                setNotice(
                  "Definição atualizada; dados e versões anteriores preservados.",
                );
                await load();
              } catch (e) {
                fail(e);
              } finally {
                setBusy(false);
              }
            }}
          >
            <label>
              Nome do campo
              <input
                required
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
              />
            </label>
            <label className="check">
              <input
                type="checkbox"
                checked={form.active}
                onChange={(e) => setForm({ ...form, active: e.target.checked })}
              />
              Campo ativo para novos dados
            </label>
            <p className="info">
              Desativar o campo mantém os valores e o histórico já registrados.
            </p>
            <button className="primary wide" disabled={busy}>
              Salvar definição
            </button>
          </form>
        </Modal>
      )}
      {modal === "invitation-result" && (
        <Modal title="Convite de primeiro acesso" onClose={() => setModal("")}>
          <p>
            Entregue este link ao usuário. Ele definirá a senha e configurará o
            autenticador. O convite só pode ser usado uma vez e vence em{" "}
            {date(form.expires_at)}.
          </p>
          <textarea
            aria-label="Link de ativação"
            readOnly
            rows={4}
            value={form.activation_link}
          />
          <button
            className="primary wide"
            onClick={() =>
              navigator.clipboard
                .writeText(form.activation_link)
                .then(() => setNotice("Convite copiado."))
                .catch(fail)
            }
          >
            Copiar convite
          </button>
        </Modal>
      )}
      {modal === "key-rotate" && (
        <Modal title="Renovar chave de API" closeDisabled={rotationBusy} onClose={() => { rotationAttempt.current = null; setForm({}); setModal(""); }}>
          <p>Renovar <strong>{form.name}</strong> mantendo as mesmas permissões, origens e data de validade.</p>
          <form onSubmit={(e) => { e.preventDefault(); void submitRotation(); }}>
            <label>
              Transição da chave anterior
              <select aria-label="Transição da chave anterior" value={form.grace_seconds} disabled={rotationBusy || rotationUncertain}
                onChange={(e) => setForm({ ...form, grace_seconds: Number(e.target.value) })}>
                <option value={900}>15 minutos</option>
                <option value={0}>Revogar imediatamente</option>
                <option value={3600}>1 hora</option>
                <option value={86400}>24 horas</option>
              </select>
            </label>
            {!rotationUncertain && <label>
              Novo código para renovar a chave
              <input required autoComplete="one-time-code" inputMode="numeric" pattern="[0-9]{6}" maxLength={6}
                value={form.otp || ""} disabled={rotationBusy} onChange={(e) => setForm({ ...form, otp: e.target.value })} />
            </label>}
            {rotationError && <p className="error" role="alert">{rotationError}</p>}
            {rotationUncertain ? <button type="button" className="primary wide" disabled={rotationBusy} onClick={() => void submitRotation(true)}>Recuperar resultado</button>
              : <button className="primary wide" disabled={rotationBusy}>{rotationBusy ? "Renovando…" : "Confirmar renovação"}</button>}
          </form>
        </Modal>
      )}
      {modal === "key-revoke" && (
        <Modal title="Revogar cadeia de chaves" onClose={() => { setForm({}); setModal(""); }}>
          <p>Revogar <strong>{form.name}</strong>, todas as suas versões anteriores e sucessoras? As integrações que usam essas chaves perderão acesso.</p>
          <button className="primary wide" disabled={busy} onClick={async () => {
            setBusy(true);
            try { await api("/admin/api-keys/" + form.id, "PATCH"); setForm({}); setModal(""); setNotice("Todas as chaves desta cadeia foram revogadas."); await load(); }
            catch (e) { fail(e); }
            finally { setBusy(false); }
          }}>Confirmar revogação</button>
        </Modal>
      )}
      {modal === "key-result" && (
        <Modal title="Chave criada" onClose={() => { setForm({}); setModal(""); }}>
          <p>Copie agora. O segredo não aparece na listagem de chaves.</p>
          {form.predecessor_id && <p>A chave anterior funciona até {date(form.predecessor_valid_until)}. A validade da nova chave termina em {date(form.expires_at)}.</p>}
          <textarea aria-label="Segredo da nova chave" readOnly rows={3} value={form.key} />
          <button
            className="primary wide"
            onClick={() =>
              navigator.clipboard
                .writeText(form.key)
                .then(() => setNotice("Chave copiada."))
                .catch(fail)
            }
          >
            Copiar chave
          </button>
        </Modal>
      )}
      {session.recovery_codes?.length > 0 && (
        <Modal
          title="Guarde seus códigos de recuperação"
          onClose={() => setSession({ ...session, recovery_codes: [] })}
        >
          <p>Estes códigos são exibidos uma vez. Guarde-os em local seguro.</p>
          <div className="recovery">
            {session.recovery_codes.map((c: string) => (
              <code key={c}>{c}</code>
            ))}
          </div>
          <button
            className="primary wide"
            onClick={() => setSession({ ...session, recovery_codes: [] })}
          >
            Guardei meus códigos
          </button>
        </Modal>
      )}
    </div>
  );
}
createRoot(document.getElementById("root")!).render(<App />);

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {
      /* App remains usable if installation is unavailable. */
    });
  });
}

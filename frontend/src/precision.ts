import { isSafeNumber, parse } from "lossless-json";

const JSON_NUMBER = /^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?$/;

/** Numeric JSON token kept outside IEEE-754; never send its text as a JSON string. */
export class PreciseNumber {
  readonly #literal: string;

  constructor(literal: string) {
    if (!JSON_NUMBER.test(literal))
      throw new TypeError("Número JSON inválido.");
    this.#literal = literal;
    Object.freeze(this);
  }

  toString(): string {
    return this.#literal;
  }

  toJSON(): never {
    throw new TypeError("Use serializeApiJson para preservar o número exato.");
  }

  [Symbol.toPrimitive](hint: string): string {
    if (hint === "string") return this.#literal;
    throw new TypeError(
      "Use o valor textual exato; conversão numérica perderia precisão.",
    );
  }
}

function parseNumericToken(token: string): number | PreciseNumber {
  const value = Number(token);
  return Number.isFinite(value) &&
    (!Number.isInteger(value) || Number.isSafeInteger(value)) &&
    isSafeNumber(token, { approx: false })
    ? value
    : new PreciseNumber(token);
}

/**
 * lossless-json 4.3.1 uses object[key] assignment while parsing objects.
 * Escape only __proto__ property names, then restore them as own properties.
 * The temporary name is absent from ALL decoded input keys, including escapes.
 * Reject duplicate keys independently of the library's conditional duplicate
 * hook: equal values and numeric wrappers must not silently replace an entry.
 */
function protectPrototypeKeys(text: string): { text: string; alias?: string } {
  const keys = new Set<string>();
  const scopes: (Set<string> | null)[] = [];
  let previousEnd = 0;
  const replacements: { start: number; end: number }[] = [];
  const strings = /"(?:[^"\\\u0000-\u001f]|\\["\\/bfnrt]|\\u[0-9a-fA-F]{4})*"/g;
  for (const match of text.matchAll(strings)) {
    // JSON punctuation outside strings determines each object's own key set.
    // Invalid JSON is still rejected by the full parser below.
    for (let position = previousEnd; position < match.index!; position++) {
      const char = text[position];
      if (char === "{") scopes.push(new Set());
      else if (char === "[") scopes.push(null);
      else if (char === "}" || char === "]") scopes.pop();
    }
    const end = match.index! + match[0].length;
    previousEnd = end;
    let next = end;
    while (next < text.length && /[\x20\x09\x0a\x0d]/.test(text[next])) next++;
    if (text[next] !== ":") continue;
    const key: string = JSON.parse(match[0]);
    const scope = scopes[scopes.length - 1];
    if (scope) {
      if (scope.has(key)) {
        const line = text.slice(0, match.index!).split("\n").length;
        throw new SyntaxError(
          "Chave JSON repetida na linha " + line + ": " + JSON.stringify(key),
        );
      }
      scope.add(key);
    }
    keys.add(key);
    if (key === "__proto__") replacements.push({ start: match.index!, end });
  }
  if (!replacements.length) return { text };
  let alias = "__bigbase_protected_key__";
  while (keys.has(alias)) alias += "_";
  let result = "";
  let position = 0;
  for (const replacement of replacements) {
    result += text.slice(position, replacement.start) + JSON.stringify(alias);
    position = replacement.end;
  }
  return { text: result + text.slice(position), alias };
}

function restorePrototypeKeys(value: unknown, alias: string): void {
  if (!value || typeof value !== "object" || value instanceof PreciseNumber)
    return;
  for (const key of Object.keys(value)) {
    const child = (value as Record<string, unknown>)[key];
    restorePrototypeKeys(child, alias);
    if (key === alias) {
      Object.defineProperty(value, "__proto__", {
        value: child,
        enumerable: true,
        writable: true,
        configurable: true,
      });
      delete (value as Record<string, unknown>)[alias];
    }
  }
}

/** Use on response.text(), never after Response.json()/JSON.parse(). */
export function parseApiJson(text: string): any {
  const protectedText = protectPrototypeKeys(text);
  const result = parse(protectedText.text, undefined, {
    parseNumber: parseNumericToken,
  });
  if (protectedText.alias) restorePrototypeKeys(result, protectedText.alias);
  return result;
}

/**
 * Serialize exact numeric tokens without treating arbitrary data properties such
 * as isLosslessNumber/value as a numeric wrapper. Plain unsupported values follow
 * JSON omission rules; non-finite or already-unsafe native numbers are rejected.
 */
export function serializeApiJson(body: unknown, space = 0): string {
  const indent = " ".repeat(Math.min(10, Math.max(0, Math.trunc(space) || 0)));
  const ancestors = new Set<object>();

  function encode(value: unknown, depth: number): string | undefined {
    if (value instanceof PreciseNumber) return value.toString();
    if (value === null) return "null";
    if (typeof value === "string" || typeof value === "boolean")
      return JSON.stringify(value);
    if (typeof value === "bigint") return value.toString();
    if (typeof value === "number") {
      if (
        !Number.isFinite(value) ||
        (Number.isInteger(value) && !Number.isSafeInteger(value))
      ) {
        throw new TypeError(
          "Número sem precisão garantida; use o texto original com parseApiJson ou parseIntegerInput.",
        );
      }
      return Object.is(value, -0) ? "-0" : JSON.stringify(value);
    }
    if (typeof value !== "object") return undefined;
    if (value instanceof Date) {
      if (!Number.isFinite(value.getTime()))
        throw new TypeError("Data inválida.");
      return JSON.stringify(value.toISOString());
    }
    if (
      !Array.isArray(value) &&
      Object.getPrototypeOf(value) !== Object.prototype &&
      Object.getPrototypeOf(value) !== null
    ) {
      throw new TypeError("Tipo não suportado no corpo JSON.");
    }
    if (ancestors.has(value))
      throw new TypeError("O corpo JSON contém referência circular.");
    ancestors.add(value);
    try {
      const array = Array.isArray(value);
      const parts: string[] = [];
      if (array) {
        for (let index = 0; index < value.length; index++)
          parts.push(encode(value[index], depth + 1) ?? "null");
      } else {
        for (const key of Object.keys(value)) {
          const encoded = encode(
            (value as Record<string, unknown>)[key],
            depth + 1,
          );
          if (encoded !== undefined)
            parts.push(JSON.stringify(key) + (indent ? ": " : ":") + encoded);
        }
      }
      const [open, close] = array ? ["[", "]"] : ["{", "}"];
      if (!parts.length) return open + close;
      return indent
        ? open +
            "\n" +
            indent.repeat(depth + 1) +
            parts.join(",\n" + indent.repeat(depth + 1)) +
            "\n" +
            indent.repeat(depth) +
            close
        : open + parts.join(",") + close;
    } finally {
      ancestors.delete(value);
    }
  }

  const result = encode(body, 0);
  if (result === undefined)
    throw new TypeError("O corpo não contém um valor JSON.");
  return result;
}

/** Text for React nodes and input values; wrappers must not be rendered as objects. */
export function displayPreciseValue(value: unknown): string {
  if (value === undefined) return "";
  if (typeof value === "string") return value;
  if (value instanceof PreciseNumber) return value.toString();
  return serializeApiJson(value);
}

/** Only for INTEGER fields. Documents, phones and identifiers remain text. */
export function parseIntegerInput(text: string): number | PreciseNumber {
  const trimmed = text.trim();
  if (!/^[+-]?\d+$/.test(trimmed))
    throw new TypeError(
      "Informe um número inteiro, sem casas decimais ou expoente.",
    );
  const sign = trimmed.startsWith("-") ? "-" : "";
  const digits = trimmed.replace(/^[+-]/, "").replace(/^0+(?=\d)/, "");
  return parseNumericToken(sign + digits);
}

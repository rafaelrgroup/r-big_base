import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import test from "node:test";
import ts from "typescript";

const directory = path.dirname(fileURLToPath(import.meta.url));
const temporary = await mkdtemp(path.join(directory, ".precision-test-"));
try {
  const source = await readFile(
    path.join(directory, "src/precision.ts"),
    "utf8",
  );
  const compiled = ts.transpileModule(source, {
    compilerOptions: {
      target: ts.ScriptTarget.ES2022,
      module: ts.ModuleKind.ESNext,
    },
    reportDiagnostics: true,
  });
  assert.equal(compiled.diagnostics?.length ?? 0, 0);
  const output = path.join(temporary, "precision.js");
  await writeFile(output, compiled.outputText);
  const {
    PreciseNumber,
    parseApiJson,
    serializeApiJson,
    displayPreciseValue,
    parseIntegerInput,
  } = await import(pathToFileURL(output).href);

  await test("safe integer limits and small metadata stay native numbers", () => {
    const data = parseApiJson(
      '{"version":5,"progress":99.9,"min":-9007199254740991,"max":9007199254740991}',
    );
    assert.equal(data.version, 5);
    assert.equal(data.progress, 99.9);
    assert.equal(data.min, Number.MIN_SAFE_INTEGER);
    assert.equal(data.max, Number.MAX_SAFE_INTEGER);
    assert.equal(
      serializeApiJson(data),
      '{"version":5,"progress":99.9,"min":-9007199254740991,"max":9007199254740991}',
    );
  });

  await test("unsafe integers keep every digit as numeric JSON tokens", () => {
    for (const literal of [
      "9007199254740992",
      "9007199254740993",
      "-9007199254740993",
      "1234567890123456789012345678901234567890",
    ]) {
      const data = parseApiJson(`{"value":${literal}}`);
      assert.ok(data.value instanceof PreciseNumber);
      assert.equal(displayPreciseValue(data.value), literal);
      assert.equal(serializeApiJson(data), `{"value":${literal}}`);
      assert.throws(() => Number(data.value), /perderia precisão/);
      assert.throws(() => JSON.stringify(data), /serializeApiJson/);
    }
  });

  await test("high precision decimals and out-of-range exponents remain exact", () => {
    for (const literal of [
      "0.12345678901234567890123456789",
      "-1.00000000000000000000001",
      "1e500",
      "1e-500",
      "9007199254740993.5",
    ]) {
      const value = parseApiJson(literal);
      assert.ok(value instanceof PreciseNumber);
      assert.equal(serializeApiJson(value), literal);
      assert.equal(String(value), literal);
    }
    assert.equal(parseApiJson("0.125"), 0.125);
  });

  await test("false, true, null, zero and negative zero remain distinct", () => {
    const values = parseApiJson("[null,false,true,0,-0]");
    assert.equal(values[0], null);
    assert.equal(values[1], false);
    assert.equal(values[2], true);
    assert.ok(Object.is(values[3], 0));
    assert.ok(Object.is(values[4], -0));
    assert.equal(serializeApiJson(values), "[null,false,true,0,-0]");
    assert.equal(displayPreciseValue(false), "false");
    assert.equal(displayPreciseValue(null), "null");
    assert.equal(displayPreciseValue(-0), "-0");
  });

  await test("Unicode, escapes and numeric-looking strings remain strings", () => {
    const text = String.raw`{"texto":"João 🧪 \"9007199254740993\"\n\u00e1","documento":"001234567890","literal":"{\"isLosslessNumber\":true}","\u0063have":"a\\b"}`;
    const expected = JSON.parse(text);
    const actual = parseApiJson(text);
    assert.deepEqual(actual, expected);
    assert.deepEqual(JSON.parse(serializeApiJson(actual)), expected);
  });

  await test("nested arrays and objects round-trip with exact numbers", () => {
    const input =
      '{"items":[{"value":9007199254740993,"flags":{"valid":null,"is_whatsapp":false}},[],{}],"counter":0}';
    assert.equal(serializeApiJson(parseApiJson(input)), input);
    assert.equal(
      displayPreciseValue(parseApiJson('{"value":9007199254740993}')),
      '{"value":9007199254740993}',
    );
  });

  await test("duplicate keys fail even for equal values and different precise numbers", () => {
    for (const input of [
      '{"x":false,"x":false}',
      '{"x":null,"x":null}',
      '{"x":9007199254740993,"x":9007199254740995}',
      '{"x":0.1234567890123456789,"x":0.1234567890123456788}',
      '{"nested":[{"x":1,"x":1}]}',
      String.raw`{"\u005f_proto__":false,"__proto__":false}`,
      String.raw`{"na\u006de":"one","name":"one"}`,
    ])
      assert.throws(() => parseApiJson(input), /Chave JSON repetida/);
    assert.throws(
      () => parseApiJson('{\n "value": 0,\n "value": 0\n}'),
      /linha 3/,
    );
  });

  await test("the same key in distinct objects and punctuation inside strings stay valid", () => {
    const input = String.raw`{"a":{"id":1},"b":[{"id":2},{"id":3}],"punctuation":"{[\"id\":1,\"id\":1]}"}`;
    assert.equal(serializeApiJson(parseApiJson(input)), input);
    assert.equal(
      parseApiJson('"root text with } [ { brackets"'),
      "root text with } [ { brackets",
    );
  });

  await test("ordinary data cannot impersonate a number wrapper", () => {
    const input =
      '{"isLosslessNumber":true,"value":"12345678901234567890","nested":{"isLosslessNumber":true,"value":false}}';
    assert.equal(serializeApiJson(parseApiJson(input)), input);
    assert.equal(typeof parseApiJson(input), "object");
  });

  await test("literal and escaped __proto__ keys remain own data properties", () => {
    const input = String.raw`{"\u005f_proto__":{"value":9007199254740993},"nested":{"__proto__":false},"__bigbase_protected_key__":"occupied","\u005f_bigbase_protected_key___":"also occupied"}`;
    const data = parseApiJson(input);
    assert.equal(Object.getPrototypeOf(data), Object.prototype);
    assert.ok(Object.hasOwn(data, "__proto__"));
    assert.ok(data.__proto__.value instanceof PreciseNumber);
    assert.ok(Object.hasOwn(data.nested, "__proto__"));
    assert.equal(data.nested.__proto__, false);
    const again = parseApiJson(serializeApiJson(data));
    assert.equal(String(again.__proto__.value), "9007199254740993");
    assert.equal(again.__bigbase_protected_key__, "occupied");
    assert.equal(again.__bigbase_protected_key___, "also occupied");
    assert.equal(Object.getPrototypeOf(again), Object.prototype);
  });

  await test("integer inputs support arbitrary length without parsing through Number", () => {
    for (const literal of [
      "9007199254740993",
      "-9007199254740993",
      "8".repeat(1000),
    ]) {
      const value = parseIntegerInput(literal);
      assert.ok(value instanceof PreciseNumber);
      assert.equal(serializeApiJson(value), literal);
    }
    assert.equal(parseIntegerInput(" +00123 "), 123);
    assert.ok(Object.is(parseIntegerInput("-0000"), -0));
    for (const invalid of ["", "1.5", "1e3", "12a", "1,000", "false"]) {
      assert.throws(() => parseIntegerInput(invalid), /número inteiro/);
    }
  });

  await test("invalid or already lossy JS values cannot silently become new data", () => {
    for (const value of [NaN, Infinity, -Infinity, 9007199254740992]) {
      assert.throws(() => serializeApiJson({ value }), /precisão/);
    }
    assert.throws(() => new PreciseNumber('1,"injected":true'), /inválido/);
    assert.throws(() => parseApiJson('{"number":01}'));
    assert.throws(() => parseApiJson('{"number":NaN}'));
    assert.throws(() => serializeApiJson(new Map()), /não suportado/);
  });

  await test("serialization handles cycles, shared references and omitted optional fields", () => {
    const circular = {};
    circular.self = circular;
    assert.throws(() => serializeApiJson(circular), /circular/);
    const shared = { value: parseIntegerInput("9007199254740993") };
    assert.equal(
      serializeApiJson([shared, shared]),
      '[{"value":9007199254740993},{"value":9007199254740993}]',
    );
    assert.equal(
      serializeApiJson({ omitted: undefined, present: null }),
      '{"present":null}',
    );
    assert.equal(serializeApiJson([undefined, false]), "[null,false]");
    assert.equal(serializeApiJson(9007199254740993n), "9007199254740993");
  });

  await test("pretty serialization and scalar display produce React-safe text", () => {
    const value = parseApiJson('{"value":9007199254740993,"state":false}');
    assert.equal(
      serializeApiJson(value, 2),
      '{\n  "value": 9007199254740993,\n  "state": false\n}',
    );
    assert.equal(typeof displayPreciseValue(value), "string");
    assert.equal(displayPreciseValue("00123"), "00123");
    assert.equal(displayPreciseValue(undefined), "");
  });
} finally {
  await rm(temporary, { recursive: true, force: true });
}

import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import test from "node:test";
import React from "react";
import { renderToString } from "react-dom/server";
import ts from "typescript";

const directory = path.dirname(fileURLToPath(import.meta.url));
const temporary = await mkdtemp(path.join(directory, ".imports-input-test-"));
try {
  for (const name of ["precision.ts", "ImportsPanel.tsx"]) {
    let source = await readFile(path.join(directory, "src", name), "utf8");
    // Node tests cover parsing and markup; Vite owns CSS loading in the browser.
    source = source
      .replace('import "./imports.css";', "")
      .replace('from "./precision"', 'from "./precision.js"');
    const compiled = ts.transpileModule(source, {
      compilerOptions: {
        target: ts.ScriptTarget.ES2022,
        module: ts.ModuleKind.ESNext,
        jsx: ts.JsxEmit.ReactJSX,
      },
      reportDiagnostics: true,
    });
    assert.equal(compiled.diagnostics?.length ?? 0, 0);
    await writeFile(
      path.join(temporary, name.replace(/\.tsx?$/, ".js")),
      compiled.outputText,
    );
  }
  const { ImportsPanel, parseImportEntries } = await import(
    pathToFileURL(path.join(temporary, "ImportsPanel.js")).href
  );
  const { serializeApiJson, PreciseNumber } = await import(
    pathToFileURL(path.join(temporary, "precision.js")).href
  );

  await test("JSON arrays preserve exact numbers, false, null and numeric-looking text", () => {
    const text =
      '[{"value":9007199254740993,"decimal":0.123456789012345678901,"flag":false,"missing":null,"document":"00123"}]';
    const preview = parseImportEntries(text);
    assert.equal(preview.errors.length, 0);
    assert.equal(preview.entries.length, 1);
    assert.ok(preview.entries[0].value instanceof PreciseNumber);
    assert.equal(serializeApiJson(preview.entries), text);
  });

  await test("JSONL errors identify physical lines and cannot become accepted entries", () => {
    const preview = parseImportEntries(
      '{"source_id":"synthetic"}\n\n{"broken":}\n{"flag":false}\n',
    );
    assert.equal(preview.entries.length, 2);
    assert.equal(preview.errors.length, 1);
    assert.equal(preview.errors[0].line, 3);
    assert.deepEqual(preview.entries[1], { flag: false });
  });

  await test("UTF-8 BOM and CRLF JSONL are supported without changing values", () => {
    const preview = parseImportEntries(
      '\ufeff{"name":"João Sintético"}\r\n{"value":-0}\r\n',
    );
    assert.equal(preview.errors.length, 0);
    assert.equal(preview.entries[0].name, "João Sintético");
    assert.ok(Object.is(preview.entries[1].value, -0));
  });

  await test("duplicate keys block JSON array and JSONL submissions without discarding a value", () => {
    const array = parseImportEntries(
      '[{"value":9007199254740993,"value":9007199254740995}]',
    );
    assert.equal(array.entries.length, 0);
    assert.match(array.errors[0].message, /Chave JSON repetida/);
    const lines = parseImportEntries(
      '{"value":null}\n{"value":false,"value":false}',
    );
    assert.equal(lines.entries.length, 1);
    assert.equal(lines.errors[0].line, 2);
    assert.match(lines.errors[0].message, /Chave JSON repetida/);
  });

  await test("limits reject more than 1000 entries and measure UTF-8 bytes", () => {
    assert.equal(
      parseImportEntries("[" + "{},".repeat(999) + "{}]").errors.length,
      0,
    );
    assert.match(
      parseImportEntries("[" + "{},".repeat(1000) + "{}]").errors[0].message,
      /1\.000/,
    );
    const preview = parseImportEntries(JSON.stringify("á".repeat(1_100_000)));
    assert.ok(preview.bytes > 2 * 1024 * 1024);
    assert.equal(preview.entries.length, 0);
    assert.match(preview.errors[0].message, /2 MiB/);
  });

  await test("payload structure is delegated to the API rather than silently omitted", () => {
    const preview = parseImportEntries('[null,false,0,"text",{},[]]');
    assert.equal(preview.entries.length, 6);
    assert.equal(preview.errors.length, 0);
    assert.deepEqual(preview.entries, [null, false, 0, "text", {}, []]);
  });

  await test("empty input and malformed arrays cannot be submitted", () => {
    assert.equal(parseImportEntries(" \n ").entries.length, 0);
    assert.equal(parseImportEntries("[]").errors.length, 1);
    assert.equal(parseImportEntries('[{"value":false},]').errors.length, 1);
  });

  await test("permission denial hides controls without making API requests", () => {
    const api = () => {
      throw new Error("Must not fetch during rendering");
    };
    const html = renderToString(
      React.createElement(ImportsPanel, { api, canEnrich: false }),
    );
    assert.match(html, /permissão/);
    assert.doesNotMatch(html, /textarea|type="file"/);
  });

  await test("initial markup labels controls and keeps empty submission disabled", () => {
    const api = () => {
      throw new Error("Must not fetch during rendering");
    };
    const html = renderToString(
      React.createElement(ImportsPanel, { api, canEnrich: true }),
    );
    assert.match(html, /Conteúdo da importação/);
    assert.match(html, /pessoa\.sintetica@example\.invalid/);
    assert.match(html, /disabled="" type="submit"/);
    assert.match(html, /aria-describedby="imports-format-help"/);
  });
} finally {
  await rm(temporary, { recursive: true, force: true });
}

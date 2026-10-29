import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

const source = readFileSync(new URL("../src/lib/api.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const { updateAgentVoiceSettings } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);

test("validation errors show the field and reason without exposing input", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: [
    { loc: ["body", "speed"], msg: "Input should be less than or equal to 1.3", input: "secret" },
  ] }), { status: 422, headers: { "Content-Type": "application/json" } });
  try {
    await assert.rejects(updateAgentVoiceSettings({ speed: 2 }), (error) => {
      assert.equal(error.message, "speed: Input should be less than or equal to 1.3");
      assert.equal(error.status, 422);
      return true;
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

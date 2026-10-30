import {readFileSync} from 'node:fs';
import ts from 'typescript';
const cache = new Map();
export function moduleUrl(name) {
  if (cache.has(name)) return cache.get(name);
  let source = readFileSync(new URL(`../src/lib/${name}.ts`, import.meta.url), 'utf8');
  source = source.replace(/^import (\w+) from ['"]\.\/[^'"]+\?worker&url['"];$/gm, 'const $1 = "https://localhost/worklet.js";')
    .replace("new URL('./voice.worker.ts', import.meta.url)", '"https://localhost/voice.worker.js"');
  source = source.replace(/from ['"]\.\/([^'"]+)['"]/g, (_, dependency) => `from ${JSON.stringify(moduleUrl(dependency))}`);
  const output = ts.transpileModule(source, {compilerOptions: {target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext}}).outputText;
  const url = `data:text/javascript;base64,${Buffer.from(output).toString('base64')}`;
  cache.set(name, url);
  return url;
}
export const flush = () => new Promise((resolve) => setImmediate(resolve));

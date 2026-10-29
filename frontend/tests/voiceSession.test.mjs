import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

// Run the real connection effect with controlled requests and timers.
// Rendering and audio are covered independently.
const moduleUrl = (source) => `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const transpile = (source) => ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
}).outputText;
const react = moduleUrl(`
  export const useCallback = (fn) => fn;
  export const useRef = (value) => ({current: value});
  export function useState(value) {
    const h = globalThis.voiceTest; const index = h.states.push(value) - 1;
    return [value, (next) => { h.states[index] = typeof next === 'function' ? next(h.states[index]) : next; }];
  }
  export const useEffect = (effect) => globalThis.voiceTest.effects.push(effect);
`);
const imports = new Map([
  ["react", react],
  ["react/jsx-runtime", moduleUrl("export const jsx = (type, props) => ({type, props}); export const jsxs = jsx;")],
  ["lucide-react", moduleUrl("export const Mic = 'mic', Play = 'play';")],
  ["../components/ui", moduleUrl("export const Button = 'button', PageShell = 'div', PageToolbar = 'header', Panel = 'section', StatusBadge = 'span';")],
  ["../context/AgentSessionContext", moduleUrl("export const useAgentSession = () => ({agents: [], selectedAgent: 'test', refresh: async () => {}});")],
  ["../lib/api", moduleUrl(`export const getDevices = () => globalThis.voiceTest.getDevices();
    export const createBrowserVoiceSession = () => globalThis.voiceTest.createSession();
    export const getAgentVoiceSettings = () => globalThis.voiceTest.voiceSettings();
    export const restartRuntime = () => Promise.resolve();
    export const connectChannel = () => { globalThis.voiceTest.channelStarts++; return Promise.resolve(); };
    export const startRuntime = () => Promise.resolve();`)],
  ["../lib/phoneVoiceClient", moduleUrl(`export class VoiceConnectionSuperseded extends Error {}
    export class PhoneVoiceClient {
      constructor(listener) { this.listener = listener; globalThis.voiceTest.clients.push(this); }
      async connect(options) { globalThis.voiceTest.connections.push(options); await globalThis.voiceTest.onConnect(); this.listener.onLive({}); }
      disconnect() { this.disconnected = true; }
      setHolding() { return Promise.resolve(); }
    }`)],
  ["../lib/deviceVoice", moduleUrl(transpile(readFileSync(new URL("../src/lib/deviceVoice.ts", import.meta.url), "utf8")))],
]);
const source = transpile(readFileSync(new URL("../src/pages/VoicePage.tsx", import.meta.url), "utf8"))
  .replace(/from "([^"]+)"/g, (match, name) => imports.has(name) ? `from ${JSON.stringify(imports.get(name))}` : match);
const { VoicePage } = await import(moduleUrl(source));
const flush = () => new Promise((resolve) => setImmediate(resolve));

async function harness(t, options = {}) {
  const h = {
    effects: [], states: [], clients: [], connections: [], pending: new Map(), listeners: new Map(), channelStarts: 0,
    getDevices: async () => ({runtime_available: true, api_available: true, api_url: "http://localhost:8013"}),
    voiceSettings: async () => ({configured: true, restart_required: false}),
    createSession: async () => ({session_id: "web-voice-auto", token: "voice_once"}),
    onConnect: async () => {}, ...options,
  };
  let id = 0;
  const window = {
    location: {protocol: "http:"},
    setTimeout: (callback, delay) => { h.pending.set(++id, {callback, delay}); return id; },
    clearTimeout: (key) => h.pending.delete(key),
    addEventListener: (name, fn) => h.listeners.set(name, fn),
    removeEventListener: (name) => h.listeners.delete(name),
  };
  const saved = new Map();
  for (const [key, value] of Object.entries({voiceTest: h, window, document: window})) {
    saved.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
    Object.defineProperty(globalThis, key, {value, configurable: true});
  }
  const page = VoicePage();
  page.type(page.props);
  const cleanup = h.effects.map((effect) => effect());
  h.dispose = () => cleanup.forEach((fn) => fn?.());
  h.fireNextTimer = async () => {
    const [key, timer] = [...h.pending].sort((a, b) => a[1].delay - b[1].delay)[0];
    h.pending.delete(key); timer.callback(); await flush(); return timer.delay;
  };
  t.after(() => {
    h.dispose();
    for (const [key, value] of saved) value ? Object.defineProperty(globalThis, key, value) : delete globalThis[key];
  });
  await flush();
  return h;
}

test("opening Voice automatically acquires a temporary session and connects", async (t) => {
  const h = await harness(t);
  assert.equal(h.connections.length, 1);
  assert.match(h.connections[0].socketUrl, /\/ws\/voice\/web-voice-auto\?token=voice_once$/);
  assert.ok(h.states.includes("live"));
  h.dispose();
  assert.equal(h.clients[0].disconnected, true);
  assert.equal(h.pending.size, 0);
});

test("leaving while a ticket is being fetched prevents a late connection", async (t) => {
  let resolve;
  const h = await harness(t, {createSession: () => new Promise((done) => { resolve = done; })});
  h.dispose();
  resolve({session_id: "late-session", token: "voice_late"});
  await flush();
  assert.equal(h.connections.length, 0);
  assert.equal(h.pending.size, 0);
});

test("connection failures retry with backoff and stop after three retries", async (t) => {
  const h = await harness(t, {onConnect: async () => { throw new Error("Unavailable"); }});
  assert.equal(await h.fireNextTimer(), 1000);
  assert.equal(await h.fireNextTimer(), 2000);
  assert.equal(await h.fireNextTimer(), 4000);
  assert.equal(h.connections.length, 4);
  assert.ok(h.states.includes("failed"));
  assert.equal(h.pending.size, 0);
});

test("a disconnected session obtains a fresh ticket and reconnects", async (t) => {
  let issued = 0;
  const h = await harness(t, {createSession: async () => ({session_id: `session-${++issued}`, token: `voice_${issued}`})});
  h.clients[0].listener.onClose({code: 1006});
  await h.fireNextTimer();
  assert.equal(h.connections.length, 2);
  assert.notEqual(h.connections[0].socketUrl, h.connections[1].socketUrl);
});

test("browser page suspension disconnects and restoration reconnects", async (t) => {
  const h = await harness(t);
  h.listeners.get("pagehide")();
  assert.equal(h.clients[0].disconnected, true);
  assert.equal(h.pending.size, 0);
  h.listeners.get("pageshow")();
  await flush();
  assert.equal(h.connections.length, 2);
});

test("a stopped Agent shows its start state without opening a connection", async (t) => {
  const h = await harness(t, {getDevices: async () => ({runtime_available: false, api_available: false})});
  assert.equal(h.connections.length, 0);
  assert.equal(h.channelStarts, 0);
  assert.ok(h.states.includes("stopped"));
  assert.equal(h.pending.size, 0);
});


test("missing shared voice setup opens no session or channel", async (t) => {
  const h = await harness(t, {voiceSettings: async () => ({configured: false, restart_required: false})});
  assert.equal(h.connections.length, 0);
  assert.equal(h.channelStarts, 0);
  assert.ok(h.states.includes("unconfigured"));
  assert.equal(h.pending.size, 0);
});

test("saved settings requiring restart do not enter an unsuccessful reconnect loop", async (t) => {
  const h = await harness(t, {voiceSettings: async () => ({configured: true, restart_required: true})});
  assert.equal(h.connections.length, 0);
  assert.ok(h.states.includes("restart"));
  assert.equal(h.pending.size, 0);
});

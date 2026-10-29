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
  export function useState(value) {
    const h = globalThis.voiceTest; const index = h.cursor++;
    if (!(index in h.states)) h.states[index] = value;
    return [h.states[index], (next) => {
      const value = typeof next === 'function' ? next(h.states[index]) : next;
      if (!Object.is(value, h.states[index])) { h.states[index] = value; h.dirty = true; }
    }];
  }
  export function useRef(value) {
    const h = globalThis.voiceTest; const index = h.cursor++;
    return h.refs[index] ??= {current: value};
  }
  export function useCallback(fn, deps) {
    const h = globalThis.voiceTest; const index = h.cursor++;
    const previous = h.hooks[index];
    if (!previous || deps.some((v, i) => !Object.is(v, previous.deps[i]))) h.hooks[index] = {fn, deps};
    return h.hooks[index].fn;
  }
  export function useEffect(effect, deps) {
    const h = globalThis.voiceTest; const index = h.cursor++;
    const previous = h.hooks[index];
    if (!previous || deps.some((v, i) => !Object.is(v, previous.deps[i]))) {
      h.effects.push(() => { previous?.cleanup?.(); h.hooks[index] = {deps, cleanup: effect()}; });
    }
  }
`);
const imports = new Map([
  ["react", react],
  ["react/jsx-runtime", moduleUrl("export const jsx = (type, props) => ({type, props}); export const jsxs = jsx; export const Fragment = 'fragment';")],
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
      disconnect() { this.disconnected = true; this.capturing = false; }
      prime() {}
      setCapture(enabled) { this.capturing = enabled; this.listener.onCapture?.(enabled); return Promise.resolve(); }
    }`)],
  ["../lib/deviceVoice", moduleUrl(transpile(readFileSync(new URL("../src/lib/deviceVoice.ts", import.meta.url), "utf8")))],
]);
const source = transpile(readFileSync(new URL("../src/pages/VoicePage.tsx", import.meta.url), "utf8"))
  .replace(/from "([^"]+)"/g, (match, name) => imports.has(name) ? `from ${JSON.stringify(imports.get(name))}` : match);
const { VoicePage } = await import(moduleUrl(source));
const flush = () => new Promise((resolve) => setImmediate(resolve));

async function harness(t, options = {}) {
  const h = {
    effects: [], states: [], refs: [], hooks: [], cursor: 0, clients: [], connections: [], pending: new Map(), listeners: new Map(), channelStarts: 0,
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
  h.render = () => {
    h.cursor = 0; h.dirty = false;
    const page = VoicePage();
    h.tree = page.type(page.props);
    h.effects.splice(0).forEach((effect) => effect());
  };
  h.update = async () => {
    await flush();
    for (let i = 0; h.dirty && i < 20; i++) { h.render(); await flush(); }
    assert.equal(h.dirty, false, "renders should settle");
  };
  h.render();
  h.dispose = () => { h.hooks.forEach((hook) => { hook.cleanup?.(); delete hook.cleanup; }); };
  h.find = (predicate) => {
    const visit = (value) => {
      if (!value || typeof value !== "object") return undefined;
      if (predicate(value.props ?? {})) return value;
      for (const child of [value.props?.children].flat(Infinity)) { const result = visit(child); if (result) return result; }
    };
    const result = visit(h.tree);
    assert.ok(result, "control must exist");
    return result.props;
  };
  h.selectContinuous = async () => {
    h.find((p) => p.children === "Continuous conversation").onClick();
    await h.update();
  };
  h.fireNextTimer = async () => {
    const [key, timer] = [...h.pending].sort((a, b) => a[1].delay - b[1].delay)[0];
    h.pending.delete(key); timer.callback(); await flush(); return timer.delay;
  };
  t.after(() => {
    h.dispose();
    for (const [key, value] of saved) value ? Object.defineProperty(globalThis, key, value) : delete globalThis[key];
  });
  await h.update();
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

test("continuous mode waits for Start, supports mute, and ends without losing confirmed transcripts", async (t) => {
  const h = await harness(t);
  await h.selectContinuous();
  assert.equal(h.connections.length, 1);
  const client = h.clients.at(-1);
  assert.ok(!client.capturing);
  h.find((p) => p['aria-label'] === 'Start conversation').onClick();
  await h.update();
  assert.equal(h.connections.length, 2);
  assert.equal(client.capturing, true);
  for (let turn = 0; turn < 3; turn++) {
    client.listener.onTranscript({role: 'user', text: `Question ${turn}`, final: true});
    client.listener.onFloor('thinking');
    client.listener.onPlaying(true);
    client.listener.onTranscript({role: 'assistant', text: `Answer ${turn}`, final: true});
    client.listener.onPlaying(false);
    client.listener.onFloor('listening');
    await h.update();
    assert.equal(client.capturing, true);
  }
  h.find((p) => p.children === 'Mute').onClick();
  await h.update();
  assert.equal(client.capturing, false);
  assert.equal(client.disconnected, undefined);
  h.find((p) => p.children === 'Unmute').onClick();
  await h.update();
  assert.equal(client.capturing, true);
  h.find((p) => p['aria-label'] === 'End conversation').onClick();
  await h.update();
  assert.equal(client.disconnected, true);
  assert.ok(h.states.some((s) => Array.isArray(s) && s.length === 6));
  assert.equal(h.pending.size, 0);
});

test("continuous mode ignores blur, pauses when hidden and requires Continue on return", async (t) => {
  const h = await harness(t);
  await h.selectContinuous();
  h.find((p) => p['aria-label'] === 'Start conversation').onClick();
  await h.update();
  const client = h.clients.at(-1);
  h.listeners.get('blur')();
  assert.equal(client.capturing, true);
  document.visibilityState = 'hidden';
  h.listeners.get('visibilitychange')();
  await h.update();
  assert.equal(client.capturing, false);
  assert.equal(client.disconnected, true);
  document.visibilityState = 'visible';
  h.listeners.get('visibilitychange')();
  await h.update();
  assert.equal(h.connections.length, 2);
  h.find((p) => p['aria-label'] === 'Continue conversation').onClick();
  await h.update();
  assert.equal(h.connections.length, 3);
  assert.equal(client.capturing, true);
});

test("continuous reconnect restores mute; switching modes ignores late callbacks", async (t) => {
  const h = await harness(t);
  await h.selectContinuous();
  h.find((p) => p['aria-label'] === 'Start conversation').onClick();
  await h.update();
  const client = h.clients.at(-1);
  h.find((p) => p.children === 'Mute').onClick();
  await h.update();
  client.listener.onClose({code: 1006});
  await h.fireNextTimer();
  await h.update();
  assert.equal(client.capturing, false);
  client.listener.onTranscript({role: 'assistant', delta: 'Unfinished', final: false});
  await h.update();
  h.find((p) => p.children === 'Hold to talk').onClick();
  await h.update();
  const count = h.connections.length;
  client.listener.onTranscript({role: 'user', text: 'Late', final: true});
  client.listener.onClose({code: 1006});
  await h.update();
  assert.equal(h.connections.length, count);
  assert.ok(!h.states.some((s) => Array.isArray(s) && s.some((line) => line.text === 'Late' || line.pending)));
});

test("permission errors end continuous capture without automatic retries", async (t) => {
  const h = await harness(t);
  await h.selectContinuous();
  h.find((p) => p['aria-label'] === 'Start conversation').onClick();
  await h.update();
  h.clients.at(-1).listener.onNotice('Microphone permission was denied.');
  await h.update();
  assert.equal(h.clients.at(-1).capturing, false);
  assert.equal(h.pending.size, 0);
  assert.ok(h.states.includes('failed'));
});

test("hold mode retains pointer cancellation and keyboard press/release controls", async (t) => {
  const h = await harness(t);
  const client = h.clients.at(-1);
  const button = () => h.find((p) => p['aria-label'] === 'Hold to talk' || p['aria-label'] === 'Release to stop recording');
  const event = {button: 0, pointerId: 1, preventDefault() {}, currentTarget: {setPointerCapture() {}}};
  button().onPointerDown(event);
  await h.update();
  assert.equal(client.capturing, true);
  button().onPointerCancel();
  await h.update();
  assert.equal(client.capturing, false);
  for (const key of [' ', 'Enter']) {
    button().onKeyDown({key, repeat: false, preventDefault() {}});
    await h.update();
    assert.equal(client.capturing, true);
    button().onKeyUp({key, preventDefault() {}});
    await h.update();
    assert.equal(client.capturing, false);
  }
});

test("ending continuous conversation cancels a pending ticket and a late permission notice cannot restart it", async (t) => {
  const h = await harness(t);
  await h.selectContinuous();
  let ticket;
  h.createSession = () => new Promise((resolve) => { ticket = resolve; });
  h.find((p) => p['aria-label'] === 'Start conversation').onClick();
  await h.update();
  h.find((p) => p['aria-label'] === 'End conversation').onClick();
  await h.update();
  ticket({session_id: 'late', token: 'late'});
  await h.update();
  assert.equal(h.connections.length, 1);
  assert.equal(h.pending.size, 0);
  h.find((p) => p.children === 'Hold to talk').onClick();
  await h.update();
  h.clients.at(-2).listener.onNotice('Old permission error');
  await h.update();
  assert.ok(!h.states.includes('Old permission error'));
});

test("a codec failure reconnects with PCM and remembers that choice across mode changes", async (t) => {
  const h = await harness(t);
  await h.selectContinuous();
  h.find((p) => p['aria-label'] === 'Start conversation').onClick();
  await h.update();
  h.clients.at(-1).listener.onClose({code: 1011, reason: 'Codec failed', codecFailure: true});
  await h.fireNextTimer();
  await h.update();
  assert.equal(h.connections.at(-1).forcePcm, true);
  assert.equal(h.clients.at(-1).capturing, true);
  h.find((p) => p.children === 'Hold to talk').onClick();
  await h.update();
  assert.equal(h.connections.at(-1).forcePcm, true);
});

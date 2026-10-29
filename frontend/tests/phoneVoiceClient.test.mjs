import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

const moduleUrl = (source) => `data:text/javascript;base64,${Buffer.from(ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
}).outputText).toString("base64")}`;
const helpersUrl = moduleUrl(readFileSync(new URL("../src/lib/deviceVoice.ts", import.meta.url), "utf8"));
const clientSource = readFileSync(new URL("../src/lib/phoneVoiceClient.ts", import.meta.url), "utf8").replace('"./deviceVoice"', JSON.stringify(helpersUrl));
const { PhoneVoiceClient, VoiceConnectionSuperseded } = await import(moduleUrl(clientSource));

function harness(t) {
  const timers = new Map();
  const sockets = [];
  const audio = [];
  const tracks = [];
  const notices = [];
  let timerId = 0;
  let micRequests = 0;
  let captureNodes = 0;
  const fake = { timers, sockets, audio, tracks, notices, suspendAudio: false, onMedia: null, closed: 0 };
  class Socket {
    static OPEN = 1;
    static CLOSING = 2;
    constructor(url) { this.url = url; this.readyState = 0; this.bufferedAmount = 0; this.sent = []; sockets.push(this); }
    send(data) { this.sent.push(data); }
    open() { this.readyState = 1; this.onopen?.(); }
    frame(value) { this.onmessage?.({ data: JSON.stringify(value) }); }
    close() { this.readyState = 3; }
    drop() { this.readyState = 3; this.onclose?.({ code: 1006, reason: "" }); }
  }
  const node = () => ({ connect() {}, disconnect() {} });
  class Audio {
    sampleRate = 16000;
    destination = {};
    currentTime = 0;
    closed = false;
    constructor() { audio.push(this); }
    resume() { return fake.suspendAudio ? new Promise(() => {}) : Promise.resolve(); }
    close() { this.closed = true; return Promise.resolve(); }
    createMediaStreamSource() { captureNodes++; return node(); }
    createScriptProcessor() { return node(); }
    createGain() { return { ...node(), gain: { value: 0 } }; }
  }
  const globals = {
    WebSocket: Socket,
    window: {
      AudioContext: Audio,
      setTimeout: (callback) => { timers.set(++timerId, callback); return timerId; },
      clearTimeout: (id) => timers.delete(id),
      setInterval: (callback) => { timers.set(++timerId, callback); return timerId; },
      clearInterval: (id) => timers.delete(id),
    },
    navigator: { mediaDevices: { getUserMedia: () => {
      micRequests++;
      if (fake.onMedia) return fake.onMedia();
      const track = { stopped: false, stop() { this.stopped = true; } };
      tracks.push(track);
      return Promise.resolve({ getTracks: () => [track] });
    } } },
  };
  const restore = [];
  for (const [key, value] of Object.entries(globals)) {
    const previous = Object.getOwnPropertyDescriptor(globalThis, key);
    Object.defineProperty(globalThis, key, { value, configurable: true });
    restore.push(() => previous ? Object.defineProperty(globalThis, key, previous) : delete globalThis[key]);
  }
  const client = new PhoneVoiceClient({
    onLive() {}, onFloor() {}, onPlaying() {}, onLevel() {}, onTranscript() {},
    onNotice: (message) => notices.push(message), onClose: () => { fake.closed++; },
  });
  t.after(() => { client.disconnect(); restore.forEach((undo) => undo()); });
  return Object.assign(fake, {
    client, requests: () => micRequests, captureNodes: () => captureNodes,
    async connect() {
      const promise = client.connect({ socketUrl: "ws://localhost/ws/voice/session?token=voice_once", pageProtocol: "http:" });
      const socket = sockets.at(-1);
      socket.open();
      socket.frame({ type: "ready" });
      await promise;
      return socket;
    },
  });
}

test("automatic voice connection requests no mic until held and releases tracks on release", async (t) => {
  const h = harness(t);
  await h.connect();
  assert.equal(h.requests(), 0);
  assert.equal(h.client.live, true);
  await h.client.setHolding(true);
  assert.equal(h.requests(), 1);
  assert.equal(h.tracks[0].stopped, false);
  await h.client.setHolding(false);
  assert.equal(h.tracks[0].stopped, true);
  h.client.disconnect();
  assert.equal(h.audio[0].closed, true);
  assert.equal(h.timers.size, 0);
});

test("leaving during the handshake cancels it and frees audio and timers", async (t) => {
  const h = harness(t);
  const pending = h.client.connect({ socketUrl: "ws://localhost/ws/voice/session", pageProtocol: "http:" });
  h.client.disconnect();
  await assert.rejects(pending, VoiceConnectionSuperseded);
  assert.equal(h.sockets[0].readyState, 3);
  assert.equal(h.audio[0].closed, true);
  assert.equal(h.timers.size, 0);
});

test("a mic grant arriving after reconnect cannot capture into the new session", async (t) => {
  const h = harness(t);
  let grant;
  h.onMedia = () => new Promise((resolve) => { grant = resolve; });
  await h.connect();
  const pending = h.client.setHolding(true);
  h.sockets[0].drop();
  assert.equal(h.timers.size, 0);
  await h.connect();
  const track = { stopped: false, stop() { this.stopped = true; } };
  grant({ getTracks: () => [track] });
  await pending;
  assert.equal(track.stopped, true);
  assert.equal(h.captureNodes(), 0);
  assert.equal(h.closed, 1);
});

test("denied mic permission is handled even while audio resume is suspended", async (t) => {
  const h = harness(t);
  h.suspendAudio = true;
  h.onMedia = () => Promise.reject(new DOMException("Denied", "NotAllowedError"));
  await h.connect();
  await h.client.setHolding(true);
  assert.match(h.notices[0], /permission was denied/);
  assert.equal(h.client.live, true);
});

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

const source = readFileSync(new URL("../src/lib/voiceDevice.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const voice = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);

function memoryStorage() {
  const data = new Map();
  return {
    getItem: (key) => (data.has(key) ? data.get(key) : null),
    setItem: (key, value) => data.set(key, String(value)),
    removeItem: (key) => data.delete(key),
  };
}

function fakeSocket() {
  return {
    binaryType: "blob",
    readyState: 0,
    sent: [],
    onopen: null,
    onmessage: null,
    onerror: null,
    onclose: null,
    send(data) {
      this.sent.push(data);
    },
    close() {
      this.readyState = 3;
      this.onclose?.({ code: 1000, reason: "" });
    },
    open() {
      this.readyState = 1;
      this.onopen?.({});
    },
    serverText(payload) {
      this.onmessage?.({ data: JSON.stringify(payload) });
    },
    serverBinary(bytes) {
      this.onmessage?.({ data: bytes });
    },
  };
}

function harness() {
  const socket = fakeSocket();
  const urls = [];
  const player = { began: [], pushed: 0, cleared: 0, resumed: 0, begin(rate, channels) { this.began.push([rate, channels]); }, push() { this.pushed += 1; }, clear() { this.cleared += 1; }, resume() { this.resumed += 1; } };
  const events = { phase: [], floor: [], transcript: [], ready: [], closed: [], errors: [], talking: [] };
  let capture;
  const timers = { fns: [], setInterval(fn) { this.fns.push(fn); return this.fns.length; }, clearInterval() {} };
  const connection = new voice.VoiceDeviceConnection({
    socketUrl(path) {
      urls.push(path);
      return `wss://agent.example${path}`;
    },
    createSocket() {
      return socket;
    },
    player,
    async startCapture(onPcm) {
      capture = onPcm;
      return () => {};
    },
    timers,
    listener: {
      onPhase: (phase) => events.phase.push(phase),
      onFloor: (floor) => events.floor.push(floor),
      onTranscript: (event) => events.transcript.push(event),
      onReady: (info) => events.ready.push(info),
      onClose: (info) => events.closed.push(info),
      onTalking: (talking) => events.talking.push(talking),
      onError: (message) => events.errors.push(message),
    },
  });
  return { connection, socket, urls, player, events, timers, get capture() { return capture; } };
}

test("hello uses the headset voice profile and native PCM rates", () => {
  const hello = voice.voiceHello("  alice  ");
  assert.equal(hello.type, "hello");
  assert.equal(hello.profile, "headset");
  assert.equal(hello.echo_managed, true);
  assert.equal(hello.interruptions, true);
  assert.equal(hello.input_sample_rate, 16000);
  assert.equal(hello.input_channels, 1);
  assert.equal(hello.output_sample_rate, 24000);
  assert.equal(hello.output_channels, 1);
  assert.equal(hello.user_id, "alice");
  assert.equal(voice.voiceHello(`${"x".repeat(200)}`).user_id.length, 128);
  assert.deepEqual(voice.voiceAuthFrame("dvc_secret"), { type: "auth", token: "dvc_secret" });
  assert.equal(voice.voiceSocketPath("webvoice1"), "/ws/devices/webvoice1/voice");
  assert.equal(voice.normalizeDeviceToken("Bearer dvc_secret"), "dvc_secret");
});

test("browser voice handshake sends auth then hello and saves nothing in the socket url", () => {
  const { connection, socket, urls, events, player } = harness();
  connection.connect({ deviceId: "webvoice1", token: "pair_once", userId: "web_user" });
  assert.deepEqual(socket.sent, []);
  socket.open();
  const frames = socket.sent.map((item) => JSON.parse(item));
  assert.deepEqual(frames[0], { type: "auth", token: "pair_once" });
  assert.equal(frames[1].type, "hello");
  assert.equal(frames[1].profile, "headset");
  assert.equal(frames[1].echo_managed, true);
  assert.equal(frames[1].interruptions, true);
  assert.equal(urls[0], "/ws/devices/webvoice1/voice");
  assert.equal(urls[0].includes("pair_once"), false);
  socket.serverText({
    type: "ready",
    device_id: "webvoice1",
    room_name: "browser",
    credential: "dvc_issued",
    output_sample_rate: 24000,
    output_channels: 1,
  });
  assert.equal(events.phase.at(-1), "live");
  assert.deepEqual(events.ready[0], { deviceId: "webvoice1", roomName: "browser", credential: "dvc_issued" });
  assert.equal(player.resumed, 1);
});

test("playback progress is reported and audio is ignored until playback starts", () => {
  const box = harness();
  box.connection.connect({ deviceId: "webvoice1", token: "dvc_saved", userId: "web_user" });
  box.socket.open();
  box.socket.serverText({ type: "ready", device_id: "webvoice1", room_name: "browser" });
  box.socket.serverBinary(new Uint8Array([1, 2, 3, 4]).buffer);
  assert.equal(box.player.pushed, 0);
  box.socket.serverText({ type: "playback_start", sample_rate: 24000, channels: 1 });
  box.socket.serverBinary(new Uint8Array([1, 2, 3, 4]).buffer);
  assert.deepEqual(box.player.began[0], [24000, 1]);
  assert.equal(box.player.pushed, 1);
  box.connection.notePlaybackProgress(4);
  assert.deepEqual(JSON.parse(box.socket.sent.at(-1)), { type: "playback_progress", played_bytes: 4 });
});

test("microphone frames are sent only while the talk button is held", async () => {
  const box = harness();
  box.connection.connect({ deviceId: "webvoice1", token: "dvc_saved", userId: "web_user" });
  box.socket.open();
  box.socket.serverText({ type: "ready", device_id: "webvoice1", room_name: "browser" });
  const pcm = new Uint8Array([8, 0]).buffer;
  await box.connection.beginTalk();
  box.capture(pcm);
  assert.equal(box.socket.sent.at(-1), pcm);
  box.connection.endTalk();
  const count = box.socket.sent.length;
  box.capture(new Uint8Array([1, 0]).buffer);
  assert.equal(box.socket.sent.length, count);
  assert.equal(box.events.talking.at(-1), false);
});

test("clear_playback stops audio and a later chunk is not played", () => {
  const box = harness();
  box.connection.connect({ deviceId: "webvoice1", token: "dvc_saved", userId: "web_user" });
  box.socket.open();
  box.socket.serverText({ type: "ready", device_id: "webvoice1", room_name: "browser" });
  box.socket.serverText({ type: "playback_start", sample_rate: 24000, channels: 1 });
  box.socket.serverText({ type: "clear_playback" });
  const pushed = box.player.pushed;
  box.socket.serverBinary(new Uint8Array([1, 2]).buffer);
  assert.equal(box.player.pushed, pushed);
  assert.ok(box.player.cleared >= 1);
});

test("transcript deltas collapse into one assistant line", () => {
  let id = 0;
  const next = () => `line-${id++}`;
  let lines = [];
  lines = voice.applyVoiceTranscript(lines, { role: "user", text: "turn on the light", final: true }, next);
  lines = voice.applyVoiceTranscript(lines, { role: "assistant", text: "On", final: false }, next);
  lines = voice.applyVoiceTranscript(lines, { role: "assistant", text: " it.", final: false }, next);
  lines = voice.applyVoiceTranscript(lines, { role: "assistant", text: "On it.", final: true }, next);
  assert.deepEqual(lines.map((line) => [line.role, line.text, line.pending]), [
    ["user", "turn on the light", false],
    ["assistant", "On it.", false],
  ]);
});

test("device credentials are stored per agent and ignored when corrupt", () => {
  const storage = memoryStorage();
  voice.saveWebVoiceDevice(storage, { agent: "alpha", deviceId: "webalpha1", credential: "dvc_alpha" });
  voice.saveWebVoiceDevice(storage, { agent: "beta", deviceId: "webbeta1", credential: "dvc_beta" });
  assert.deepEqual(voice.loadWebVoiceDevice(storage, "alpha"), {
    agent: "alpha",
    deviceId: "webalpha1",
    credential: "dvc_alpha",
  });
  assert.equal(voice.loadWebVoiceDevice(storage, "missing"), null);
  storage.setItem("xagent_web_voice_devices", "{");
  assert.equal(voice.loadWebVoiceDevice(storage, "alpha"), null);
  voice.clearWebVoiceDevice(storage, "beta");
  voice.saveWebVoiceDevice(storage, { agent: "beta", deviceId: "webbeta1", credential: "dvc_beta" });
  voice.clearWebVoiceDevice(storage, "beta");
  assert.equal(voice.loadWebVoiceDevice(storage, "beta"), null);
});

test("server transcript and floor frames parse into voice events", () => {
  assert.equal(voice.parseVoiceServerMessage('{"type":"state","floor":"thinking"}').floor, "thinking");
  const partial = voice.parseVoiceServerMessage('{"type":"transcript","role":"assistant","delta":"Hi","final":false}');
  assert.deepEqual(partial, { kind: "transcript", role: "assistant", text: "Hi", final: false });
  assert.equal(voice.parseVoiceServerMessage("not-json").kind, "ignore");
  assert.equal(voice.voiceFloorLabel("speaking"), "Speaking");
});

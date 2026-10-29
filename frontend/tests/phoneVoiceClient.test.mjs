import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

const moduleUrl = (source) => `data:text/javascript;base64,${Buffer.from(ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
}).outputText).toString("base64")}`;
const helpersUrl = moduleUrl(readFileSync(new URL("../src/lib/deviceVoice.ts", import.meta.url), "utf8"));
const audioUrl = moduleUrl(readFileSync(new URL("../src/lib/voiceAudio.ts", import.meta.url), "utf8"));
const clientSource = readFileSync(new URL("../src/lib/phoneVoiceClient.ts", import.meta.url), "utf8").replace('"./deviceVoice"', JSON.stringify(helpersUrl)).replace('"./voiceAudio"', JSON.stringify(audioUrl));
const { PhoneVoiceClient, VoiceConnectionSuperseded } = await import(moduleUrl(clientSource));

function harness(t) {
  const timers = new Map();
  const sockets = [];
  const audio = [];
  const tracks = [];
  const notices = [];
  const playing = [];
  const sources = [];
  const processors = [];
  let timerId = 0;
  let micRequests = 0;
  let captureNodes = 0;
  const fake = { timers, sockets, audio, tracks, notices, playing, sources, processors, suspendAudio: false, onMedia: null, closed: 0 };
  class Socket {
    static OPEN = 1;
    static CLOSING = 2;
    constructor(url) { this.url = url; this.readyState = 0; this.bufferedAmount = 0; this.sent = []; sockets.push(this); }
    send(data) { this.sent.push(data); }
    open() { this.readyState = 1; this.onopen?.(); }
    frame(value) { this.onmessage?.({ data: value instanceof ArrayBuffer ? value : JSON.stringify(value) }); }
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
    createBuffer(channels, frames) { return {copyToChannel() {}}; }
    createBufferSource() {
      const source = {...node(), start() {}, stop() {}, end() { this.onended?.(); }};
      sources.push(source);
      return source;
    }
    createMediaStreamSource() { captureNodes++; return node(); }
    createScriptProcessor() { const processor = node(); processors.push(processor); return processor; }
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
    onLive() {}, onFloor() {}, onPlaying(value) { playing.push(value); }, onLevel() {}, onTranscript() {},
    onNotice: (message) => notices.push(message), onClose: () => { fake.closed++; },
  });
  t.after(() => { client.disconnect(); restore.forEach((undo) => undo()); });
  return Object.assign(fake, {
    client, requests: () => micRequests, captureNodes: () => captureNodes,
    async connect(ready = { type: "ready" }) {
      const promise = client.connect({ socketUrl: "ws://localhost/ws/voice/session?token=voice_once", pageProtocol: "http:" });
      const socket = sockets.at(-1);
      socket.open();
      await new Promise((resolve) => setImmediate(resolve));
      socket.frame(ready);
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

test("only acknowledges actual playback completion, including empty audio", async (t) => {
  const h = harness(t);
  const socket = await h.connect();
  assert.equal(JSON.parse(socket.sent[0]).playback_ack, true);
  socket.frame({type: 'playback_start', playback_id: 'one', sample_rate: 24000});
  socket.frame(new ArrayBuffer(48000));
  socket.frame({type: 'playback_done', playback_id: 'one'});
  const finished = () => socket.sent.filter((s) => typeof s === 'string').map(JSON.parse).filter((f) => f.type === 'playback_finished');
  assert.equal(finished().length, 0);
  h.audio[0].currentTime = 1.1;
  h.sources[0].end();
  assert.deepEqual(finished(), [{type: 'playback_finished', playback_id: 'one'}]);
  for (const timer of h.timers.values()) timer();
  assert.equal(finished().length, 1);
  socket.frame({type: 'playback_start', playback_id: 'empty'});
  socket.frame({type: 'playback_done', playback_id: 'empty'});
  assert.equal(finished()[1].playback_id, 'empty');
});

test("late done, clear, audio callbacks and socket frames cannot affect new playback", async (t) => {
  const h = harness(t);
  const oldSocket = await h.connect();
  const oldMessage = oldSocket.onmessage;
  oldSocket.frame({type: 'playback_start', playback_id: 'old'});
  oldSocket.frame(new ArrayBuffer(4800));
  const lateEnded = h.sources[0].onended;
  oldSocket.frame({type: 'clear_playback', playback_id: 'old'});
  oldSocket.frame({type: 'playback_start', playback_id: 'new'});
  oldSocket.frame(new ArrayBuffer(4800));
  oldSocket.frame({type: 'playback_done', playback_id: 'old'});
  oldSocket.frame({type: 'clear_playback', playback_id: 'old'});
  lateEnded();
  assert.equal(h.playing.at(-1), true);
  assert.equal(h.sources[1].onended instanceof Function, true);
  h.client.disconnect();
  await h.connect();
  const count = h.playing.length;
  oldMessage({data: JSON.stringify({type: 'playback_start', playback_id: 'late'})});
  assert.equal(h.playing.length, count);
});

test("duplicate capture requests share a pending permission and late grants stop after mute", async (t) => {
  const h = harness(t);
  let grant;
  h.onMedia = () => new Promise((resolve) => { grant = resolve; });
  await h.connect();
  const pending = h.client.setCapture(true);
  await h.client.setCapture(true);
  assert.equal(h.requests(), 1);
  await h.client.setCapture(false);
  const track = {stopped: false, stop() { this.stopped = true; }};
  grant({getTracks: () => [track]});
  await pending;
  assert.equal(track.stopped, true);
  assert.equal(h.captureNodes(), 0);
});

test("v2 PCM capture carries stream boundaries and duplicate release emits one end", async (t) => {
  const h = harness(t);
  const socket = await h.connect({type:'ready', audio_protocol:2, input_codec:'pcm_s16le', output_codec:'pcm_s16le'});
  const messages = () => socket.sent.filter((value) => typeof value === 'string').map(JSON.parse);
  await h.client.setCapture(true);
  h.processors[0].onaudioprocess({inputBuffer:{sampleRate:16000,getChannelData:()=>new Float32Array(320)}});
  const packets = socket.sent.filter((value) => value instanceof ArrayBuffer);
  const header = new DataView(packets[0]);
  assert.equal(header.getUint32(0),0x58415632);
  assert.equal(header.getUint32(4),1);
  assert.equal(header.getUint32(20),320);
  await Promise.all([h.client.setCapture(false),h.client.setCapture(false)]);
  assert.equal(messages().filter((m)=>m.type==='input_end').length,1);
  await h.client.setCapture(true);
  assert.deepEqual(messages().filter((m)=>m.type==='input_start').map((m)=>m.stream_id),[1,2]);
});

test("v2 playback reports samples instead of compressed bytes and rejects stale binary streams", async (t) => {
  const h = harness(t);
  const socket = await h.connect({type:'ready', audio_protocol:2, input_codec:'pcm_s16le', output_codec:'pcm_s16le'});
  const {packAudio} = await import(audioUrl);
  socket.frame({type:'playback_start',playback_id:'one',stream_id:1,sample_rate:24000});
  socket.frame(packAudio({stream:1,sequence:0,timestamp:0,samples:480,discard:0,payload:new ArrayBuffer(960)}));
  socket.frame({type:'playback_done',playback_id:'one'});
  h.audio[0].currentTime=.1;
  h.sources[0].end();
  const progress = socket.sent.filter((v)=>typeof v==='string').map(JSON.parse).find((m)=>m.type==='playback_progress');
  assert.equal(progress.played_samples,480);
  assert.equal(progress.sample_rate,24000);
  assert.equal(progress.played_bytes,undefined);
  socket.frame({type:'playback_start',playback_id:'two',stream_id:2,sample_rate:24000});
  const count=h.sources.length;
  socket.frame(packAudio({stream:1,sequence:1,timestamp:20000,samples:480,discard:0,payload:new ArrayBuffer(960)}));
  assert.equal(h.sources.length,count);
});

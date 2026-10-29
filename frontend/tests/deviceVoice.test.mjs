import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

const source = readFileSync(new URL("../src/lib/deviceVoice.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const voice = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);

test("voice socket url uses wss and a query ticket on the Agent host", () => {
  const url = new URL(voice.browserVoiceSocketUrl("https://devices.example/agent", "web-voice-123", "voice_abc/def"));
  assert.equal(url.protocol, "wss:");
  assert.equal(url.host, "devices.example");
  assert.equal(url.pathname, "/agent/ws/voice/web-voice-123");
  assert.equal(url.searchParams.get("token"), "voice_abc/def");
  assert.equal(url.username, "");
});

test("http api urls become ws and existing query strings are dropped", () => {
  const url = new URL(voice.browserVoiceSocketUrl("http://127.0.0.1:8010?ignored=1", "web-voice-123", "voice_once"));
  assert.equal(url.protocol, "ws:");
  assert.equal(url.host, "127.0.0.1:8010");
  assert.equal(url.pathname, "/ws/voice/web-voice-123");
  assert.equal(url.searchParams.get("token"), "voice_once");
  assert.equal(url.searchParams.get("ignored"), null);
});

test("browser voice uses a temporary session endpoint with no paired device identity", () => {
  const url = new URL(voice.browserVoiceSocketUrl("https://agent.example/prefix", "web-voice-123", "voice_once/only"));
  assert.equal(url.protocol, "wss:");
  assert.equal(url.pathname, "/prefix/ws/voice/web-voice-123");
  assert.equal(url.searchParams.get("token"), "voice_once/only");
});

test("https pages reject a cleartext voice socket", () => {
  const socketUrl = voice.browserVoiceSocketUrl("http://127.0.0.1:8010", "web-voice-123", "voice_token");
  assert.throws(() => voice.assertSocketAllowedOnPage(socketUrl, "https:"), /WSS/);
  assert.doesNotThrow(() => voice.assertSocketAllowedOnPage(socketUrl, "http:"));
});

test("browser hello is a headset profile with echo managed by the page", () => {
  assert.deepEqual(voice.voiceHello(48000), {
    type: "hello",
    name: "Browser microphone",
    location: "",
    input_sample_rate: 48000,
    input_channels: 1,
    output_sample_rate: 24000,
    output_channels: 1,
    profile: "headset",
    echo_managed: true,
  });
  assert.equal(voice.voiceHello(96000).input_sample_rate, 48000);
  assert.equal(voice.clampSampleRate(Number.NaN), 16000);
});

test("pcm16 roundtrip keeps silence and a mid-scale sample", () => {
  const encoded = voice.floatToPcm16([0, 0.5, -0.5]);
  const [plane] = voice.pcm16ToChannelPlanes(encoded, 1);
  assert.equal(plane[0], 0);
  assert.ok(Math.abs(plane[1] - 0.5) < 0.001);
  assert.ok(Math.abs(plane[2] + 0.5) < 0.001);
});

test("linear resample halves a four-sample ramp", () => {
  const output = voice.resampleLinear([0, 1, 2, 3], 4, 2);
  assert.equal(output.length, 2);
  assert.equal(output[0], 0);
  assert.equal(output[1], 2);
});

test("assistant deltas fold into one final transcript line", () => {
  let id = 1;
  const nextId = () => id++;
  const partial = voice.applyTranscript([], { role: "assistant", delta: "Hi", final: false }, nextId);
  const more = voice.applyTranscript(partial, { role: "assistant", delta: " there", final: false }, nextId);
  const done = voice.applyTranscript(more, { role: "assistant", text: "Hi there", final: true }, nextId);
  assert.equal(done.length, 1);
  assert.equal(done[0].text, "Hi there");
  assert.equal(done[0].pending, false);
  const user = voice.applyTranscript(done, { role: "user", text: "Hello", final: true }, nextId);
  assert.deepEqual(user.map((line) => line.text), ["Hi there", "Hello"]);
});

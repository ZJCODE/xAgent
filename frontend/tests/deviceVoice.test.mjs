import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

const source = readFileSync(new URL("../src/lib/deviceVoice.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const voice = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);

test("voice socket url uses wss and a query token on the devices host", () => {
  const url = new URL(voice.deviceVoiceSocketUrl("https://devices.example/agent", "phone-web", "dvc_abc/def"));
  assert.equal(url.protocol, "wss:");
  assert.equal(url.host, "devices.example");
  assert.equal(url.pathname, "/agent/ws/devices/phone-web/voice");
  assert.equal(url.searchParams.get("token"), "dvc_abc/def");
  assert.equal(url.username, "");
});

test("http api urls become ws and existing query strings are dropped", () => {
  const url = new URL(voice.deviceVoiceSocketUrl("http://127.0.0.1:8010?ignored=1", voice.PHONE_DEVICE_ID, "pair_code"));
  assert.equal(url.protocol, "ws:");
  assert.equal(url.host, "127.0.0.1:8010");
  assert.equal(url.pathname, "/ws/devices/phone-web/voice");
  assert.equal(url.searchParams.get("token"), "pair_code");
  assert.equal(url.searchParams.get("ignored"), null);
});

test("https pages reject a cleartext voice socket", () => {
  const socketUrl = voice.deviceVoiceSocketUrl("http://127.0.0.1:8010", "phone-web", "dvc_token");
  assert.throws(() => voice.assertSocketAllowedOnPage(socketUrl, "https:"), /WSS/);
  assert.doesNotThrow(() => voice.assertSocketAllowedOnPage(socketUrl, "http:"));
});

test("phone hello is a headset profile with echo managed by the page", () => {
  assert.deepEqual(voice.voiceHello(48000), {
    type: "hello",
    name: "Phone mic",
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

test("credential storage keeps only a dvc token for this phone and agent", () => {
  const values = new Map();
  const storage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  };
  assert.equal(voice.readStoredCredential(storage, "home"), "");
  voice.writeStoredCredential(storage, "home", "dvc_secret");
  assert.equal(voice.readStoredCredential(storage, "home"), "dvc_secret");
  assert.equal(voice.readStoredCredential(storage, "other"), "");
  voice.writeStoredCredential(storage, "home", "pair_once");
  values.set(voice.credentialStorageKey("home"), JSON.stringify({ deviceId: "phone-web", credential: "pair_once" }));
  assert.equal(voice.readStoredCredential(storage, "home"), "");
  voice.writeStoredCredential(storage, "home", "");
  assert.equal(values.size, 0);
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

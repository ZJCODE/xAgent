import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

const source = readFileSync(new URL("../src/lib/voicePcm.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const pcm = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);

test("float samples convert to little-endian PCM16", () => {
  assert.equal(pcm.floatSampleToInt16(0), 0);
  assert.equal(pcm.floatSampleToInt16(1), 32767);
  assert.equal(pcm.floatSampleToInt16(-1), -32768);
  assert.equal(pcm.floatSampleToInt16(2), 32767);
});

test("downsampler emits 16 kHz PCM16 frames", () => {
  const frames = [];
  const down = pcm.createPcm16Downsampler(16000, 16000, (frame) => frames.push(new Int16Array(frame)), 4);
  down.push(new Float32Array([0, 1, -1, 0.5]));
  assert.equal(frames.length, 1);
  assert.deepEqual(Array.from(frames[0]), [0, 32767, -32768, 16384]);

  const halved = [];
  const half = pcm.createPcm16Downsampler(32000, 16000, (frame) => halved.push(new Int16Array(frame)), 2);
  half.push(new Float32Array([0, 0, 1, 1]));
  assert.equal(halved.length, 1);
  assert.equal(halved[0][0], 0);
  assert.equal(halved[0][1], 32767);
});

test("partial capture is flushed when the talk button is released", () => {
  const frames = [];
  const down = pcm.createPcm16Downsampler(16000, 16000, (frame) => frames.push(frame), 4);
  down.push(new Float32Array([0.5, -0.5]));
  assert.equal(frames.length, 0);
  const partial = down.takePartial();
  assert.equal(partial.byteLength, 4);
  const view = new DataView(partial);
  assert.equal(view.getInt16(0, true), 16384);
  assert.equal(view.getInt16(2, true), -16384);
});

test("playback progress counts wire bytes that have actually played", () => {
  const segments = [
    { start: 1, duration: 1, bytes: 480 },
    { start: 2, duration: 1, bytes: 240 },
  ];
  assert.equal(pcm.bytesPlayedAt(segments, 0.5), 0);
  assert.equal(pcm.bytesPlayedAt(segments, 1.5), 240);
  assert.equal(pcm.bytesPlayedAt(segments, 2.5), 600);
  assert.equal(pcm.bytesPlayedAt(segments, 4), 720);
});

test("stereo PCM16 mixes down to mono floats", () => {
  const bytes = new ArrayBuffer(8);
  const view = new DataView(bytes);
  view.setInt16(0, 16384, true);
  view.setInt16(2, -16384, true);
  view.setInt16(4, 0, true);
  view.setInt16(6, 0, true);
  const mixed = pcm.pcm16ToMonoFloat(bytes, 2);
  assert.equal(mixed.length, 2);
  assert.ok(Math.abs(mixed[0]) < 0.01);
  assert.equal(mixed[1], 0);
});

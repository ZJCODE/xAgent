import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import ts from 'typescript';
const source = readFileSync(new URL('../src/lib/voiceAudio.ts', import.meta.url), 'utf8');
const output = ts.transpileModule(source, {compilerOptions: {target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext}}).outputText;
const audio = await import(`data:text/javascript;base64,${Buffer.from(output).toString('base64')}`);

test('audio packet header matches the Python wire format and rejects invalid sizes', () => {
  const packet = {stream: 2, sequence: 3, timestamp: 20000, samples: 320, discard: 0, payload: new ArrayBuffer(640)};
  const wire = audio.packAudio(packet);
  assert.equal(Buffer.from(wire).subarray(0, 28).toString('hex'), '5841563200000002000000030000000000004e200000014000000000');
  assert.deepEqual(audio.unpackAudio(wire), packet);
  assert.throws(() => audio.unpackAudio(new ArrayBuffer(28)));
  assert.throws(() => audio.unpackAudio(audio.packAudio({...packet, samples: 6000})));
});

test('Opus input uses encoder pre-skip and trims the drained tail to the submitted samples', async (t) => {
  const previousEncoder = globalThis.AudioEncoder, previousData = globalThis.AudioData;
  const packets = [], failures = [];
  class Encoder {
    state='configured'; encodeQueueSize=0; first=true;
    constructor(init) {this.init=init;}
    configure() {}
    emit() {
      const description = Uint8Array.from([...Buffer.from('OpusHead'), 1, 1, 56, 1, 128, 187, 0, 0, 0, 0, 0]).buffer;
      this.init.output({byteLength: 2, duration: 20000, copyTo: (out) => new Uint8Array(out).set([0x98, 0])}, this.first ? {decoderConfig:{description}} : undefined);
      this.first=false;
    }
    encode() {this.emit();}
    async flush() {this.emit();}
    close() {this.state='closed';}
  }
  globalThis.AudioEncoder=Encoder;
  globalThis.AudioData=class {close(){}};
  t.after(() => {globalThis.AudioEncoder=previousEncoder; globalThis.AudioData=previousData;});
  const encoder = new audio.OpusInput(1, (packet) => packets.push(packet), (error) => failures.push(error));
  encoder.write(new Float32Array(960));
  await encoder.finish();
  encoder.close();
  assert.equal(failures.length, 0);
  assert.deepEqual(packets.map((p) => [p.sequence,p.timestamp,p.samples,p.discard]), [[0,0,648,312],[1,13500,312,0]]);
});

test('startup selects symmetric PCM when the complete codec contract is unavailable', async (t) => {
  const saved = {AudioEncoder: globalThis.AudioEncoder, AudioDecoder: globalThis.AudioDecoder, AudioData: globalThis.AudioData};
  t.after(() => Object.assign(globalThis,saved));
  globalThis.AudioEncoder = undefined;
  assert.equal(await audio.selectAudioCodec(), 'pcm_s16le');
  globalThis.AudioEncoder = {isConfigSupported: async () => ({supported: true})};
  globalThis.AudioDecoder = {isConfigSupported: async () => ({supported: false})};
  globalThis.AudioData = class {};
  assert.equal(await audio.selectAudioCodec(), 'pcm_s16le');
});

test('decoder drain precedes completion and closed decoder callbacks only release their data', async (t) => {
  const saved = {AudioDecoder:globalThis.AudioDecoder,EncodedAudioChunk:globalThis.EncodedAudioChunk};
  t.after(()=>Object.assign(globalThis,saved));
  let instance, closed=0, resolve;
  globalThis.EncodedAudioChunk=class {constructor(init){Object.assign(this,init);}};
  globalThis.AudioDecoder=class {
    state='configured';
    constructor(init){instance=this;this.init=init;}
    configure(){}
    decode(){}
    flush(){return new Promise((done)=>{resolve=done;});}
    close(){this.state='closed';}
    output(){this.init.output({sampleRate:48000,numberOfFrames:960,copyTo:(samples,options)=>{assert.equal(options.frameOffset,312);samples.fill(.5);},close:()=>{closed++;}});}
  };
  const frames=[],errors=[];
  const decoder=new audio.OpusOutput((samples)=>frames.push(samples),(error)=>errors.push(error));
  decoder.write({stream:1,sequence:0,timestamp:0,samples:648,discard:312,payload:new ArrayBuffer(2)});
  let done=false;
  const finishing=decoder.finish().then(()=>{done=true;});
  assert.equal(done,false);
  instance.output(); resolve(); await finishing;
  assert.equal(frames[0].length,648);
  decoder.close(); instance.output();
  assert.equal(frames.length,1);
  assert.equal(closed,2);
  assert.equal(errors.length,0);
});

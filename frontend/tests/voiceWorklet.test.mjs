import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {moduleUrl} from './voiceTestModules.mjs';
const playbackConfig = await import(moduleUrl('voicePlaybackConfig'));
const source=readFileSync(new URL('../src/lib/audioDsp.js',import.meta.url),'utf8');
const {AudioRing}=await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
function worklet(name,rate=48000){
  let Constructor;const messages=[];
  const source=readFileSync(new URL(`../src/lib/${name}.worklet.js`,import.meta.url),'utf8').replace(/^import .*;\n/gm,'');
  vm.runInNewContext(source,{AudioRing,...playbackConfig,sampleRate:rate,Float32Array,Math,Array,
    AudioWorkletProcessor:class{constructor(){this.port={postMessage:(data,transfer=[])=>messages.push(structuredClone(data,{transfer}))};}},
    registerProcessor:(_,value)=>{Constructor=value;}});
  const node=new Constructor();return{node,messages,send:(data)=>node.port.onmessage({data})};
}

test('ring resampling is sample-exact and independent of callback block size',()=>{
  for(const [from,to] of [[48000,16000],[48000,48000],[44100,48000],[24000,44100]]){
    const run=(size)=>{const ring=new AudioRing(from,to),out=[],frame=new Float32Array(960);const drain=()=>{let count;while((count=ring.read(frame)))out.push(...frame.slice(0,count));};
      for(let start=0;start<from+101;start+=size){ring.push(Float32Array.from({length:Math.min(size,from+101-start)},(_,i)=>Math.sin((start+i)*.02)));drain();}
      ring.final=true;drain();assert.equal(out.length,Math.floor((from+101)*to/from));assert.equal(ring.complete,true);return out;};
    const a=run(128),b=run(2048);assert.equal(a.length,b.length);for(let i=0;i<a.length;i++)assert.ok(Math.abs(a[i]-b[i])<.00001);
  }
});
test('thirty minutes of media keeps a fixed ring and exact counters',()=>{
  const ring=new AudioRing(48000,48000),input=new Float32Array(960).fill(.25),output=new Float32Array(960),storage=ring.data;
  for(let i=0;i<30*60*50;i++){ring.push(input);assert.equal(ring.read(output),960);}
  ring.final=true;assert.equal(ring.complete,true);assert.equal(ring.consumed,30*60*48000);assert.equal(ring.data,storage);assert.equal(ring.pending,0);
});
test('capture buffers recycle, flush partial frames, and reject stale stream commands',()=>{
  const h=worklet('voiceCapture');h.send({type:'start',stream:1,rate:16000});
  for(let n=0;n<100;n++){
    h.node.process([[new Float32Array(128)]],[[new Float32Array(128)]]);
    for(const m of h.messages.splice(0))if(m.type==='audio')h.send({type:'recycle',stream:1,buffer:m.buffer});
  }
  h.send({type:'flush',stream:1});assert.ok(h.messages.some(m=>m.type==='flushed'));assert.equal(h.node.active,false);
  h.send({type:'start',stream:2,rate:48000});h.send({type:'abort',stream:1});assert.equal(h.node.active,true);
});
test('playback underflow produces silence without advancing media and completion follows EOS',()=>{
  const h=worklet('voicePlayback');h.send({type:'start',stream:1,rate:48000});const output=new Float32Array(128);
  h.node.process([],[[output]]);assert.equal(h.node.ring.consumed,0);assert.ok(output.every(x=>x===0));
  h.send({type:'audio',stream:1,buffer:new Float32Array(960).fill(.5).buffer});
  for(let i=0;i<30;i++)h.node.process([],[[output]]);
  assert.equal(h.node.ring.consumed,0); // Short clips wait for EOS instead of padding to 200 ms.
  h.send({type:'end',stream:1});for(let i=0;i<8;i++)h.node.process([],[[output]]);
  assert.equal(h.messages.filter(m=>m.type==='finished').length,1);assert.equal(h.messages.at(-1).samples,960);
  for(let i=0;i<100;i++)h.node.process([],[[output]]);assert.equal(h.messages.filter(m=>m.type==='finished').length,1);
  h.send({type:'start',stream:2,rate:48000});h.send({type:'clear',stream:1});h.send({type:'audio',stream:1,buffer:new Float32Array(960).buffer});assert.equal(h.node.ring.written,0);
});
test('renderer reports sample credits every 50 ms and never advances them during a stall',()=>{
  for(const rate of [24000,48000]){
    const h=worklet('voicePlayback',48000),output=new Float32Array(128);
    h.send({type:'start',stream:1,rate});
    h.send({type:'audio',stream:1,buffer:new Float32Array(rate*.3).buffer});
    for(let n=0;n<19;n++)h.node.process([],[[output]]);
    assert.equal(h.messages.filter(m=>m.type==='progress').length,1);
    assert.ok(h.messages.at(-1).samples>0);
    for(let n=0;n<200;n++)h.node.process([],[[output]]);
    const consumed=h.node.ring.consumed;
    for(let n=0;n<200;n++)h.node.process([],[[output]]);
    assert.equal(h.node.ring.consumed,consumed);
    assert.equal(h.messages.at(-1).samples,consumed);
    for(let cycle=0;cycle<200;cycle++){
      h.send({type:'audio',stream:1,buffer:new Float32Array(rate*.28).buffer});
      for(let n=0;n<150;n++)h.node.process([],[[output]]);
    }
    h.send({type:'end',stream:1});
    for(let n=0;n<25;n++)h.node.process([],[[output]]);
    assert.ok(!h.messages.some(m=>m.type==='error'));
    assert.equal(h.messages.filter(m=>m.type==='finished').length,1);
    assert.equal(h.messages.at(-1).samples,h.node.ring.written);
  }
});

test('startup and recovery accumulate 200 ms, while clear cancels buffered audio immediately', () => {
  const h = worklet('voicePlayback'), output = new Float32Array(128);
  const process = () => h.node.process([], [[output]]);
  const push = (samples) => h.send({type: 'audio', stream: 1, buffer: new Float32Array(samples).fill(.5).buffer});
  h.send({type: 'start', stream: 1, rate: 48000});
  push(9552); // 199 ms is not enough.
  process(); assert.equal(h.node.ring.consumed, 0);
  push(48); process(); assert.equal(h.node.ring.consumed, 128);
  for (let i = 0; i < 80; i++) process();
  assert.equal(h.node.underruns, 1);
  assert.equal(h.node.ring.consumed, 9600);
  push(4800); // A small refill must not immediately stutter again.
  for (let i = 0; i < 80; i++) process();
  assert.equal(h.node.ring.consumed, 9600);
  assert.equal(h.node.underruns, 1);
  push(4800); process(); assert.equal(h.node.ring.consumed, 9728);
  h.send({type: 'clear', stream: 1}); process();
  assert.ok(output.every(x => x === 0));
  assert.equal(h.node.ring.consumed, 9728);
  assert.ok(!h.messages.some(m => m.type === 'finished'));
});

test('a full server window can arrive in one burst without overflowing playback', () => {
  const h = worklet('voicePlayback');
  h.send({type: 'start', stream: 1, rate: 48000});
  for (let i = 0; i < 50; i++) h.send({type: 'audio', stream: 1, buffer: new Float32Array(960).buffer});
  assert.equal(h.node.ring.pending, 48000);
  assert.ok(!h.messages.some(m => m.type === 'error'));
  h.send({type: 'audio', stream: 1, buffer: new Float32Array(10000).buffer});
  assert.ok(h.messages.some(m => m.type === 'error'));
});

// Virtual time: 20 ms TTS packets, ordered WebSocket delivery, delayed renderer
// acknowledgements and the real render worklet. No timers or network services.
function simulatePlayback({windowSeconds, roundTripMs, rate = 48000, jitterMs = 0, pauseAtMs = Infinity, pauseMs = 0}) {
  const h = worklet('voicePlayback'), output = new Float32Array(128);
  const total = rate * 10, frame = rate / 50;
  let sent = 0, credits = 0, lastDelivery = 0, ended = false, firstSampleAt;
  let maxQueue = 0;
  const audio = [], acknowledgements = [];
  h.send({type: 'start', stream: 1, rate});
  for (let tick = 0; tick < 15000; tick++) {
    const now = tick * 128 * 1000 / 48000;
    while (acknowledgements.length && acknowledgements[0].at <= now) credits = acknowledgements.shift().samples;
    // A real-time producer can generate at most one frame per 20 ms.
    while (sent < total && sent + frame <= (now / 1000 * rate + frame) && sent + frame - credits <= rate * windowSeconds) {
      const sequence = sent / frame;
      const jitter = sequence % 31 >= 20 ? jitterMs : 0;
      const nominal = now + roundTripMs / 2 + jitter;
      const delivery = nominal >= pauseAtMs && nominal < pauseAtMs + pauseMs ? pauseAtMs + pauseMs : nominal;
      lastDelivery = Math.max(lastDelivery, delivery); // TCP preserves order, including catch-up bursts.
      audio.push({at: lastDelivery, samples: frame});
      sent += frame;
    }
    while (audio.length && audio[0].at <= now) {
      const packet = audio.shift();
      h.send({type: 'audio', stream: 1, buffer: new Float32Array(packet.samples).fill(.25).buffer});
    }
    if (sent === total && !audio.length && !ended) { h.send({type: 'end', stream: 1}); ended = true; }
    maxQueue = Math.max(maxQueue, h.node.ring.pending);
    h.node.process([], [[output]]);
    if (firstSampleAt === undefined && h.node.ring.consumed) firstSampleAt = now;
    for (const message of h.messages.splice(0)) {
      assert.notEqual(message.type, 'error', message.message);
      if (message.type === 'finished') return {underruns: h.node.underruns, samples: message.samples, maxQueue, firstSampleAt};
      if (message.type === 'progress') acknowledgements.push({at: now + roundTripMs / 2, samples: message.samples});
    }
  }
  assert.fail('Playback failed to drain within 40 seconds');
}

test('a ten-second reply stays continuous with 500 ms RTT and 150 ms arrival jitter', (t) => {
  const server = readFileSync(new URL('../../xagent/interfaces/voice/ws.py', import.meta.url), 'utf8');
  const windowSeconds = Number(server.match(/^PLAYBACK_WINDOW_SECONDS = ([\d.]+)/m)[1]);
  assert.ok(windowSeconds > playbackConfig.PLAYBACK_BUFFER_SECONDS);
  assert.ok(windowSeconds < playbackConfig.MAX_PLAYBACK_QUEUE_SECONDS);
  for (const rate of [24000, 48000]) for (const roundTripMs of [20, 500]) {
    const result = simulatePlayback({windowSeconds, roundTripMs, rate, jitterMs: 150});
    assert.equal(result.underruns, 0);
    assert.equal(result.samples, rate * 10);
    assert.ok(result.maxQueue <= rate * windowSeconds);
    t.diagnostic(`${rate} Hz, RTT ${roundTripMs} ms: zero underruns, first audio ${result.firstSampleAt.toFixed(1)} ms`);
  }
  const oldWindow = simulatePlayback({windowSeconds: .3, roundTripMs: 500, jitterMs: 150});
  assert.ok(oldWindow.underruns > 0, 'The old send window must reproduce feedback starvation');
  t.diagnostic(`300 ms send window on the same WAN trace: ${oldWindow.underruns} underruns`);
});

test('a long network outage recovers without lost samples or invented render credit', () => {
  const result = simulatePlayback({windowSeconds: 1, roundTripMs: 500, pauseAtMs: 4000, pauseMs: 1500});
  assert.ok(result.underruns > 0);
  assert.equal(result.samples, 480000);
  assert.ok(result.maxQueue <= 48000);
});

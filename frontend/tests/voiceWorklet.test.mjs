import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const source=readFileSync(new URL('../src/lib/audioDsp.js',import.meta.url),'utf8');
const {AudioRing}=await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
function worklet(name,rate=48000){
  let Constructor;const messages=[];
  const source=readFileSync(new URL(`../src/lib/${name}.worklet.js`,import.meta.url),'utf8').replace(/^import .*;\n/,'');
  vm.runInNewContext(source,{AudioRing,sampleRate:rate,Float32Array,Math,Array,
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
  assert.equal(h.node.ring.consumed,0); // Initial 60 ms jitter buffer.
  h.send({type:'end',stream:1});for(let i=0;i<8;i++)h.node.process([],[[output]]);
  assert.equal(h.messages.filter(m=>m.type==='finished').length,1);assert.equal(h.messages.at(-1).samples,960);
  for(let i=0;i<100;i++)h.node.process([],[[output]]);assert.equal(h.messages.filter(m=>m.type==='finished').length,1);
  h.send({type:'start',stream:2,rate:48000});h.send({type:'clear',stream:1});h.send({type:'audio',stream:1,buffer:new Float32Array(960).buffer});assert.equal(h.node.ring.written,0);
});

import assert from 'node:assert/strict';
import test from 'node:test';
import {moduleUrl,flush} from './voiceTestModules.mjs';
const {VoiceTransport}=await import(moduleUrl('voiceTransport'));
const {packAudio,unpackAudio}=await import(moduleUrl('voiceAudio'));
function harness(t){
  const old=globalThis.WebSocket,sockets=[],messages=[];
  class Socket{static OPEN=1;readyState=0;bufferedAmount=0;sent=[];constructor(){sockets.push(this);}send(data){this.sent.push(data);}close(){this.readyState=3;}open(){this.readyState=1;this.onopen?.();}frame(frame){this.onmessage?.({data:frame instanceof ArrayBuffer?frame:JSON.stringify(frame)});}}
  globalThis.WebSocket=Socket;
  const port=()=>({sent:[],start(){},close(){},postMessage(data){this.sent.push(data);},frame(data){this.onmessage({data});}});
  const capture=port(),playback=port(),engine=new VoiceTransport((m)=>messages.push(m));
  t.after(()=>{engine.close();globalThis.WebSocket=old;});
  return{engine,capture,playback,messages,sockets,async connect(outputDelayMs=0){await engine.command({id:1,type:'init',socketUrl:'ws://localhost/voice',capture,playback,outputDelayMs});const s=sockets[0];s.open();s.frame({type:'ready',audio_protocol:2,codec:'pcm_s16le',input_sample_rate:16000,output_sample_rate:24000,input_channels:1,output_channels:1});return s;}};
}
test('worker sends only the explicit codec profile and capture stream packets',async(t)=>{
  const h=harness(t),s=await h.connect();assert.equal(JSON.parse(s.sent[0]).codec,'pcm_s16le');
  await h.engine.command({id:2,type:'capture_start'});h.capture.frame({type:'started',stream:1});
  h.capture.frame({type:'audio',stream:1,samples:320,buffer:new Float32Array(320).buffer});
  await h.engine.command({id:3,type:'capture_end'});h.capture.frame({type:'flushed',stream:1});await flush();
  assert.equal(unpackAudio(s.sent.find(m=>m instanceof ArrayBuffer)).samples,320);
  assert.ok(s.sent.filter(m=>typeof m==='string').map(JSON.parse).some(m=>m.type==='input_end'));
  assert.ok(h.messages.some(m=>m.type==='response'&&m.id===3));
});
test('playback confirmation waits for renderer consumption, stale streams cannot finish a new reply',async(t)=>{
  const h=harness(t),s=await h.connect();
  const start=(stream)=>s.frame({type:'playback_start',codec:'pcm_s16le',sample_rate:24000,channels:1,stream_id:stream,playback_id:String(stream)});
  start(1);s.frame(packAudio({stream:1,sequence:0,timestamp:0,samples:480,discard:0,payload:new ArrayBuffer(960)}));s.frame({type:'playback_done',playback_id:'1'});await flush();
  assert.ok(!s.sent.filter(m=>typeof m==='string').map(JSON.parse).some(m=>m.type==='playback_finished'));
  start(2);h.playback.frame({type:'finished',stream:1,samples:480});await flush();
  assert.ok(!s.sent.filter(m=>typeof m==='string').map(JSON.parse).some(m=>m.type==='playback_finished'));
  s.frame(packAudio({stream:2,sequence:0,timestamp:0,samples:480,discard:0,payload:new ArrayBuffer(960)}));s.frame({type:'playback_done',playback_id:'2'});await flush();h.playback.frame({type:'finished',stream:2,samples:480});await new Promise(r=>setTimeout(r,5));
  assert.equal(s.sent.filter(m=>typeof m==='string').map(JSON.parse).filter(m=>m.type==='playback_finished').at(-1).playback_id,'2');
});
test('protocol mismatch and codec errors are fatal instead of triggering format changes',async(t)=>{
  const h=harness(t);await h.engine.command({id:1,type:'init',socketUrl:'ws://localhost',capture:h.capture,playback:h.playback,outputDelayMs:0});h.sockets[0].open();h.sockets[0].frame({type:'ready',audio_protocol:1});
  assert.ok(h.messages.some(m=>m.type==='response'&&m.error&&m.fatal));assert.equal(h.sockets.length,1);
});
test('late processor errors from aborted streams do not end new capture or playback',async(t)=>{
  const h=harness(t),s=await h.connect();
  await h.engine.command({id:2,type:'capture_start'});await h.engine.command({id:3,type:'capture_start'});
  h.capture.frame({type:'error',stream:1,message:'Old capture overload'});
  s.frame({type:'playback_start',codec:'pcm_s16le',sample_rate:24000,channels:1,stream_id:1,playback_id:'old'});
  s.frame({type:'playback_start',codec:'pcm_s16le',sample_rate:24000,channels:1,stream_id:2,playback_id:'new'});
  h.playback.frame({type:'error',stream:1,message:'Old playback overload'});await flush();
  assert.equal(s.readyState,1);assert.ok(!h.messages.some(m=>m.type==='event'&&m.name==='error'));
  h.playback.frame({type:'error',stream:2,message:'Current playback overload'});
  assert.equal(s.readyState,3);assert.ok(h.messages.some(m=>m.type==='event'&&m.name==='error'));
});
test('rendered send credits are independent of output latency and audible ledger progress',async(t)=>{
  const h=harness(t),s=await h.connect(180);
  s.frame({type:'playback_start',codec:'pcm_s16le',sample_rate:24000,channels:1,stream_id:1,playback_id:'reply'});
  for(let i=0;i<15;i++)s.frame(packAudio({stream:1,sequence:i,timestamp:i*20000,samples:480,discard:0,payload:new ArrayBuffer(960)}));
  h.playback.frame({type:'progress',stream:1,samples:2400});
  const reports=()=>s.sent.filter(m=>typeof m==='string').map(JSON.parse).filter(m=>m.type==='playback_progress');
  assert.equal(reports().at(-1).rendered_samples,2400);
  assert.equal(reports().at(-1).played_samples,0);
  h.playback.frame({type:'progress',stream:1,samples:6000});
  assert.equal(reports().at(-1).rendered_samples,6000);
  assert.equal(reports().at(-1).played_samples,1680);
  assert.ok(!s.sent.filter(m=>typeof m==='string').map(JSON.parse).some(m=>m.type==='playback_finished'));
});

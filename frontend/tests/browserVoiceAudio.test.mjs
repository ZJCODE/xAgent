import assert from 'node:assert/strict';
import test from 'node:test';
import {moduleUrl,flush} from './voiceTestModules.mjs';
const {BrowserVoiceAudio}=await import(moduleUrl('browserVoiceAudio'));
function harness(t){
  const contexts=[],workers=[],tracks=[],events=[];
  const node=()=>({port:{},connect(){},disconnect(){this.disconnected=true;}});
  class Context{sampleRate=48000;baseLatency=.01;outputLatency=.02;audioWorklet={addModule:async()=>{}};destination={};constructor(){contexts.push(this);}resume(){return Promise.resolve();}close(){this.closed=true;return Promise.resolve();}createMediaStreamSource(){return node();}}
  class Worker{constructor(){workers.push(this);}postMessage(command){if(command.id)queueMicrotask(()=>this.onmessage?.({data:{type:'response',id:command.id,result:command.type==='init'?{audio_protocol:2,codec:'pcm_s16le',input_sample_rate:16000,output_sample_rate:24000,input_channels:1,output_channels:1}:undefined}}));}terminate(){this.terminated=true;}}
  const h={contexts,workers,tracks,events,onMedia:null};
  const globals={isSecureContext:true,AudioContext:Context,AudioWorkletNode:class{constructor(){Object.assign(this,node());}},Worker,navigator:{mediaDevices:{getUserMedia:()=>{
    if(h.onMedia)return h.onMedia();const track={stop(){this.stopped=true;}};tracks.push(track);return Promise.resolve({getTracks:()=>[track]});
  }}}};
  const saved=new Map();for(const [key,value]of Object.entries(globals)){saved.set(key,Object.getOwnPropertyDescriptor(globalThis,key));Object.defineProperty(globalThis,key,{configurable:true,writable:true,value});}
  const audio=new BrowserVoiceAudio((name,value)=>events.push({name,value}));
  t.after(()=>{audio.close();for(const [key,value]of saved)value?Object.defineProperty(globalThis,key,value):delete globalThis[key];});
  return{...h,audio,async connect(){await audio.connect('ws://localhost/voice','http:');}};
}
test('one context and worker own audio; microphone opens only on demand and closes deterministically',async(t)=>{
  const h=harness(t);await h.connect();assert.equal(h.tracks.length,0);await h.audio.setCapture(true);assert.equal(h.tracks.length,1);
  await Promise.all([h.audio.setCapture(false),h.audio.setCapture(false)]);assert.equal(h.tracks[0].stopped,true);
  await h.audio.setCapture(true);h.audio.close();assert.ok(h.tracks.every(t=>t.stopped));assert.equal(h.workers[0].terminated,true);assert.equal(h.contexts[0].closed,true);
});
test('late microphone permission after leaving cannot attach to the new session',async(t)=>{
  const h=harness(t);await h.connect();let grant;h.onMedia=()=>new Promise(resolve=>{grant=resolve;});
  // The harness object and original closure share the same media hook through navigator.
  const old= navigator.mediaDevices.getUserMedia;navigator.mediaDevices.getUserMedia=()=>new Promise(resolve=>{grant=resolve;});
  const pending=h.audio.setCapture(true);h.audio.close();const track={stop(){this.stopped=true;}};grant({getTracks:()=>[track]});await pending;assert.equal(track.stopped,true);navigator.mediaDevices.getUserMedia=old;
});
test('missing AudioWorklet fails explicitly without opening a legacy capture path',async(t)=>{
  const h=harness(t);globalThis.AudioWorkletNode=undefined;assert.throws(()=>h.audio.prime(),/AudioWorklet/);assert.equal(h.workers.length,0);
});

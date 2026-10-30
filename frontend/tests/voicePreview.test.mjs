import assert from 'node:assert/strict';
import test from 'node:test';
import {moduleUrl, flush} from './voiceTestModules.mjs';
const {VoicePreviewPlayer}=await import(moduleUrl('voicePreview'));

function harness(t){
  const old=globalThis.AudioContext,contexts=[],states=[],errors=[],requests=[];
  class Context {
    destination={}; sources=[]; closed=false; decodeGate=null;
    constructor(){contexts.push(this);}
    resume(){this.resumed=true;return Promise.resolve();}
    decodeAudioData(){return this.decodeGate || Promise.resolve({duration:1});}
    createBufferSource(){const source={start(){this.started=true;},stop(){this.stopped=true;},connect(){},disconnect(){this.disconnected=true;}};this.sources.push(source);return source;}
    close(){this.closed=true;return Promise.resolve();}
  }
  globalThis.AudioContext=Context;
  const load=(selection,signal)=>new Promise((resolve,reject)=>requests.push({selection,signal,resolve,reject}));
  const player=new VoicePreviewPlayer(load,state=>states.push(state),message=>errors.push(message));
  t.after(()=>{player.stop();globalThis.AudioContext=old;});
  return {player,contexts,states,errors,requests};
}

test('preview primes audio from the gesture, uses draft settings and cleans up after completion',async t=>{
  const h=harness(t),selection={voice:'Maya',speed:1.25,voice_api_key:'draft-key'};
  const play=h.player.play(selection);
  assert.equal(h.contexts[0].resumed,true);
  assert.equal(h.states.at(-1),'loading');
  assert.deepEqual(h.requests[0].selection,selection);
  h.requests[0].resolve(new ArrayBuffer(48));await play;
  const source=h.contexts[0].sources[0];
  assert.equal(source.started,true);assert.equal(h.states.at(-1),'playing');
  source.onended();
  assert.equal(h.states.at(-1),'idle');assert.equal(h.contexts[0].closed,true);
  assert.equal(source.disconnected,true);
});

test('stopping cancels a pending request and its late response cannot play',async t=>{
  const h=harness(t),play=h.player.play({voice:'Maya',speed:1});
  h.player.stop();assert.equal(h.requests[0].signal.aborted,true);
  h.requests[0].resolve(new ArrayBuffer(48));await play;
  assert.equal(h.contexts[0].sources.length,0);assert.equal(h.states.at(-1),'idle');
  assert.equal(h.contexts[0].closed,true);assert.ok(h.errors.every(message=>!message));
});

test('a cancelled old request cannot overwrite a new preview or report an old error',async t=>{
  const h=harness(t),first=h.player.play({voice:'Maya',speed:1});
  const second=h.player.play({voice:'Daniel',speed:1.3});
  assert.equal(h.requests[0].signal.aborted,true);
  h.requests[1].resolve(new ArrayBuffer(48));await second;
  h.requests[0].reject(new Error('old failure'));await first;
  assert.equal(h.states.at(-1),'playing');assert.ok(h.errors.every(message=>!message));
  assert.equal(h.contexts[0].sources.length,0);assert.equal(h.contexts[1].sources.length,1);
});

test('stopping during audio decoding prevents late playback',async t=>{
  const h=harness(t),play=h.player.play({voice:'Maya',speed:1});
  let decoded;h.contexts[0].decodeGate=new Promise(resolve=>{decoded=resolve;});
  h.requests[0].resolve(new ArrayBuffer(48));await flush();
  h.player.stop();decoded({duration:1});await play;
  assert.equal(h.contexts[0].sources.length,0);assert.equal(h.states.at(-1),'idle');
});

test('provider failure releases audio resources and remains retryable',async t=>{
  const h=harness(t),first=h.player.play({voice:'Maya',speed:1});
  h.requests[0].reject(new Error('Try another key.'));await first;
  assert.equal(h.contexts[0].closed,true);assert.equal(h.states.at(-1),'idle');
  assert.equal(h.errors.at(-1),'Try another key.');
  const retry=h.player.play({voice:'Maya',speed:1});h.requests[1].resolve(new ArrayBuffer(48));await retry;
  assert.equal(h.states.at(-1),'playing');assert.equal(h.errors.at(-1),'');
});

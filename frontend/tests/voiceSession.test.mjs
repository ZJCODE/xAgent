import assert from 'node:assert/strict';
import test from 'node:test';
import {moduleUrl, flush} from './voiceTestModules.mjs';
const {VoiceSessionController} = await import(moduleUrl('voiceSessionController'));
const {AudioSessionError} = await import(moduleUrl('browserVoiceAudio'));

function harness(t, overrides={}) {
  const oldWindow=globalThis.window;
  globalThis.window={location:{protocol:'http:'}};
  const clients=[], connections=[], timers=new Map(); let id=0;
  const oldSet=globalThis.setTimeout,oldClear=globalThis.clearTimeout;
  globalThis.setTimeout=(fn,delay)=>{timers.set(++id,{fn,delay});return id;};
  globalThis.clearTimeout=(key)=>timers.delete(key);
  const deps={settings:async()=>({configured:true,restart_required:false}),devices:async()=>({runtime_available:true,api_available:true,api_url:'http://localhost:8013'}),ticket:async()=>({session_id:'test',token:'voice_once'}),connectApi:async()=>{},startAgent:async()=>{},restartAgent:async()=>{},...overrides};
  const controller=new VoiceSessionController('',async()=>{},deps,(event)=>{
    const client={event,closed:false,prime(){},async connect(url){connections.push(url);if(overrides.onConnect)await overrides.onConnect();return{codec:'opus'};},async setCapture(value){this.capture=value;event('capture',value?'recording':'closed');},close(){this.closed=true;this.capture=false;}};
    clients.push(client);return client;
  });
  t.after(()=>{controller.dispose();globalThis.window=oldWindow;globalThis.setTimeout=oldSet;globalThis.clearTimeout=oldClear;});
  return{controller,clients,connections,timers,async timer(){const [key,value]=[...timers].sort((a,b)=>a[1].delay-b[1].delay)[0];timers.delete(key);value.fn();await flush();return value.delay;}};
}

test('default hold mode prepares a connection and only records while pressed',async(t)=>{
  const h=harness(t);await h.controller.open();
  assert.equal(h.controller.state.connection,'live');assert.equal(h.clients[0].capture,false);
  h.controller.press();assert.equal(h.clients[0].capture,true);
  h.controller.release();assert.equal(h.clients[0].capture,false);
});
test('continuous mode starts explicitly, survives multiple turns, and mutes without stopping playback',async(t)=>{
  const h=harness(t);await h.controller.open();h.controller.setMode('continuous');await flush();
  assert.equal(h.connections.length,1);assert.equal(h.controller.state.connection,'idle');
  h.controller.start();await flush();const client=h.clients.at(-1);
  assert.equal(client.capture,true);
  for(let i=0;i<3;i++){client.event('transcript',{role:'user',text:`Q${i}`,final:true});client.event('playback','playing');client.event('transcript',{role:'assistant',text:`A${i}`,final:true});}
  h.controller.mute(true);assert.equal(client.capture,false);assert.equal(client.closed,false);assert.equal(h.controller.state.playback,'playing');
  h.controller.mute(false);assert.equal(client.capture,true);h.controller.release();assert.equal(client.capture,true);
  h.controller.end();assert.equal(client.closed,true);assert.equal(h.controller.state.lines.length,6);assert.equal(h.timers.size,0);
});
test('background ends continuous capture and return requires an explicit Continue',async(t)=>{
  const h=harness(t);h.controller.setMode('continuous');await flush();h.controller.start();await flush();
  h.controller.hide();const count=h.connections.length;assert.equal(h.clients.at(-1).closed,true);
  h.controller.show();await flush();assert.equal(h.connections.length,count);assert.equal(h.controller.state.resumable,true);
  h.controller.hide();h.controller.show();assert.equal(h.controller.state.resumable,true);
  h.controller.start();await flush();assert.equal(h.connections.length,count+1);
});
test('late tickets and callbacks from a switched mode cannot change the new session',async(t)=>{
  let ticket;const h=harness(t,{ticket:()=>new Promise((resolve)=>{ticket=resolve;})});
  const opening=h.controller.open();await flush();h.controller.setMode('continuous');await flush();
  ticket({session_id:'late',token:'late'});await opening;assert.equal(h.connections.length,0);
  assert.equal(h.controller.state.connection,'idle');
});
test('transient network errors retry with backoff and stop after three retries',async(t)=>{
  const h=harness(t,{onConnect:async()=>{throw new AudioSessionError('Network unavailable',false);}});await h.controller.open();
  assert.equal(await h.timer(),1000);assert.equal(await h.timer(),2000);assert.equal(await h.timer(),4000);
  assert.equal(h.controller.state.connection,'failed');assert.equal(h.connections.length,4);assert.equal(h.timers.size,0);
});
test('codec and permission failures terminate without automatic format switching',async(t)=>{
  const h=harness(t);h.controller.setMode('continuous');await flush();h.controller.start();await flush();
  const client=h.clients.at(-1);client.event('failure',{message:'Codec failed',fatal:true});
  assert.equal(h.controller.state.connection,'failed');assert.equal(h.controller.state.started,false);assert.equal(h.timers.size,0);
  client.event('transcript',{role:'user',text:'late',final:true});assert.equal(h.controller.state.lines.length,0);
});
test('reconnect restores continuous mute state and drops partial transcripts',async(t)=>{
  const h=harness(t);h.controller.setMode('continuous');await flush();h.controller.start();await flush();
  const old=h.clients.at(-1);h.controller.mute(true);old.event('transcript',{role:'assistant',delta:'partial',final:false});old.event('failure',{message:'Connection lost',fatal:false});
  assert.equal(h.controller.state.connection,'reconnecting');
  await h.timer();assert.equal(h.clients.at(-1).capture,false);assert.equal(h.controller.state.muted,true);assert.equal(h.controller.state.lines.length,0);
});
test('missing setup, stopped Agent and restart requirements never open audio',async(t)=>{
  for(const [phase,overrides] of [
    ['unconfigured',{settings:async()=>({configured:false})}],
    ['stopped',{devices:async()=>({runtime_available:false})}],
    ['restart',{settings:async()=>({configured:true,restart_required:true})}],
  ]){
    await t.test(phase,async(t)=>{const h=harness(t,overrides);await h.controller.open();assert.equal(h.controller.state.connection,phase);assert.equal(h.clients.length,0);assert.equal(h.timers.size,0);});
  }
});
test('ending while a continuous ticket is pending cancels its microphone intent',async(t)=>{
  let grant;const h=harness(t,{ticket:()=>new Promise(resolve=>{grant=resolve;})});
  h.controller.setMode('continuous');await flush();h.controller.start();await flush();h.controller.end();
  grant({session_id:'late',token:'late'});await flush();
  assert.equal(h.connections.length,0);assert.equal(h.controller.state.started,false);assert.equal(h.controller.state.connection,'idle');assert.equal(h.clients[0].closed,true);
});

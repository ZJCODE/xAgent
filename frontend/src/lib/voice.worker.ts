import {VoiceTransport, type VoiceCommand} from './voiceTransport';
const scope = globalThis as unknown as {postMessage: (message: unknown) => void; onmessage: (event: MessageEvent<VoiceCommand>) => void};
const transport = new VoiceTransport((message) => scope.postMessage(message));
scope.onmessage = ({data}) => { void transport.command(data); };

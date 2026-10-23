import { useCallback, useEffect, useRef, useState } from "react";
import { createDevicePairing, removeDevice } from "./api";
import { useAgentSession } from "../context/AgentSessionContext";
import { capturePcm16, PcmStreamPlayer } from "./voiceAudio";
import {
  applyVoiceTranscript,
  clearWebVoiceDevice,
  createWebVoiceDeviceId,
  loadWebVoiceDevice,
  normalizeDeviceToken,
  saveWebVoiceDevice,
  VoiceDeviceConnection,
  voiceFloorLabel,
  type VoiceSocket,
  type StoredWebVoiceDevice,
  type VoiceTranscriptLine,
} from "./voiceDevice";
import { makeId } from "./format";
import { webSocketUrl } from "./webSocketUrl";

export type VoicePhase = "idle" | "connecting" | "live";

function credentialRejected(code: number, reason: string): boolean {
  if (code !== 1008) return false;
  return /authentication failed|invalid pairing|already paired|credential/i.test(reason);
}

export function useVoiceChat(userId: string) {
  const { selectedAgent } = useAgentSession();
  const [open, setOpen] = useState(false);
  const [phase, setPhase] = useState<VoicePhase>("idle");
  const [floor, setFloor] = useState("");
  const [lines, setLines] = useState<VoiceTranscriptLine[]>([]);
  const [error, setError] = useState("");
  const [talking, setTalking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [record, setRecord] = useState<StoredWebVoiceDevice | null>(null);
  const connectionRef = useRef<VoiceDeviceConnection | null>(null);
  const audioRef = useRef<AudioContext | null>(null);
  const playerRef = useRef<PcmStreamPlayer | null>(null);
  const userIdRef = useRef(userId);
  const agentRef = useRef(selectedAgent);
  const recordRef = useRef<StoredWebVoiceDevice | null>(null);
  const pendingIdRef = useRef("");
  const tokenRef = useRef("");
  userIdRef.current = userId;
  agentRef.current = selectedAgent;
  recordRef.current = record;

  const ensureAudio = useCallback(() => {
    if (!audioRef.current || audioRef.current.state === "closed") {
      const ctx = new AudioContext({ latencyHint: "interactive" });
      audioRef.current = ctx;
      playerRef.current = new PcmStreamPlayer(ctx, (bytes) => connectionRef.current?.notePlaybackProgress(bytes));
    }
    void audioRef.current.resume();
    playerRef.current?.resume();
    return audioRef.current;
  }, []);

  useEffect(() => {
    setRecord(loadWebVoiceDevice(localStorage, selectedAgent));
    pendingIdRef.current = "";
    const connection = new VoiceDeviceConnection({
      socketUrl: webSocketUrl,
      createSocket: (url) => new WebSocket(url) as unknown as VoiceSocket,
      player: {
        begin: (sampleRate, channels) => playerRef.current?.begin(sampleRate, channels),
        push: (pcm) => playerRef.current?.push(pcm),
        clear: () => playerRef.current?.clear(),
        resume: () => playerRef.current?.resume(),
      },
      startCapture: async (onPcm) => capturePcm16(ensureAudio(), onPcm),
      listener: {
        onPhase: (next) => {
          setPhase(next);
          if (next !== "live") setTalking(false);
        },
        onFloor: setFloor,
        onTranscript: (event) => {
          setLines((current) => applyVoiceTranscript(current, event, () => makeId("voice")));
        },
        onReady: (info) => {
          const agent = agentRef.current;
          const issued = info.credential || "";
          const credential = issued || (tokenRef.current.startsWith("dvc_") ? tokenRef.current : "");
          if (!agent || !info.deviceId || !credential.startsWith("dvc_")) return;
          const next = { agent, deviceId: info.deviceId, credential };
          saveWebVoiceDevice(localStorage, next);
          setRecord(next);
          pendingIdRef.current = "";
        },
        onClose: ({ code, reason }) => {
          const saved = recordRef.current;
          if (
            saved &&
            tokenRef.current === saved.credential &&
            agentRef.current &&
            credentialRejected(code, reason)
          ) {
            clearWebVoiceDevice(localStorage, agentRef.current);
            setRecord(null);
          }
          setError(reason);
          setFloor("");
        },
        onTalking: setTalking,
        onError: setError,
      },
    });
    connectionRef.current = connection;
    return () => {
      connection.disconnect();
      connectionRef.current = null;
      playerRef.current?.close();
      playerRef.current = null;
      void audioRef.current?.close();
      audioRef.current = null;
      setPhase("idle");
      setTalking(false);
    };
  }, [ensureAudio, selectedAgent]);

  const deviceIdForNewPair = useCallback(() => {
    if (recordRef.current?.deviceId) return recordRef.current.deviceId;
    if (!pendingIdRef.current) pendingIdRef.current = createWebVoiceDeviceId();
    return pendingIdRef.current;
  }, []);

  const connectToken = useCallback((deviceId: string, token: string) => {
    setError("");
    setLines([]);
    setFloor("");
    tokenRef.current = normalizeDeviceToken(token);
    try {
      ensureAudio();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start audio.");
      return;
    }
    connectionRef.current?.connect({ deviceId, token: tokenRef.current, userId: userIdRef.current });
  }, [ensureAudio]);

  const connectSaved = useCallback(() => {
    const saved = recordRef.current;
    if (!saved) return;
    connectToken(saved.deviceId, saved.credential);
  }, [connectToken]);

  const pairBrowser = useCallback(async () => {
    setBusy(true);
    setError("");
    try {
      const pairing = await createDevicePairing();
      connectToken(deviceIdForNewPair(), pairing.pairing_code);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not create a pairing code.");
    } finally {
      setBusy(false);
    }
  }, [connectToken, deviceIdForNewPair]);

  const connectCode = useCallback((code: string) => {
    const token = normalizeDeviceToken(code);
    if (!token) {
      setError("Enter a pairing code.");
      return;
    }
    connectToken(deviceIdForNewPair(), token);
  }, [connectToken, deviceIdForNewPair]);

  const disconnect = useCallback(() => {
    connectionRef.current?.disconnect();
    setFloor("");
    setTalking(false);
  }, []);

  const unpair = useCallback(async () => {
    const saved = recordRef.current;
    disconnect();
    pendingIdRef.current = "";
    if (agentRef.current) clearWebVoiceDevice(localStorage, agentRef.current);
    setRecord(null);
    if (!saved) return;
    try {
      await removeDevice(saved.deviceId);
    } catch (err) {
      setError(err instanceof Error ? `${err.message} Remove ${saved.deviceId} on the Devices page if it is still listed.` : "Could not remove the paired browser.");
    }
  }, [disconnect]);

  const beginTalk = useCallback(() => {
    setError("");
    void connectionRef.current?.beginTalk();
  }, []);

  const endTalk = useCallback(() => {
    connectionRef.current?.endTalk();
  }, []);

  return {
    open,
    setOpen,
    phase,
    floor,
    floorLabel: floor ? voiceFloorLabel(floor) : phase === "live" ? "Listening" : "",
    lines,
    error,
    talking,
    busy,
    paired: Boolean(record?.credential),
    deviceId: record?.deviceId || "",
    connectSaved,
    pairBrowser,
    connectCode,
    disconnect,
    unpair,
    beginTalk,
    endTalk,
  };
}

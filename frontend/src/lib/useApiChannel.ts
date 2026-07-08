import { useCallback, useEffect, useState } from "react";
import { useAgentSession } from "../context/AgentSessionContext";
import { getChannels, startChannel } from "./api";
import type { ChannelStatus } from "../types";

export function useApiChannel() {
  const { selectedAgent, refresh: refreshAgents } = useAgentSession();
  const [apiChannel, setApiChannel] = useState<ChannelStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      const data = await getChannels();
      const api = data.channels.find((channel) => channel.id === "api") ?? null;
      setApiChannel(api);
      setError("");
      return api;
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      return null;
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    setLoading(true);
    void load();
  }, [load, selectedAgent]);

  useEffect(() => {
    if (starting || apiChannel?.status === "running") return;
    const interval = window.setInterval(() => void load(), 5000);
    return () => window.clearInterval(interval);
  }, [apiChannel?.status, load, starting]);

  useEffect(() => {
    if (!starting) return;
    const interval = window.setInterval(() => void load(), 1000);
    return () => window.clearInterval(interval);
  }, [load, starting]);

  useEffect(() => {
    if (!starting || apiChannel?.status !== "running") return;
    setStarting(false);
  }, [apiChannel?.status, starting]);

  const start = useCallback(async () => {
    setStarting(true);
    setError("");
    try {
      await startChannel("api");
      await load();
      await refreshAgents();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setStarting(false);
    }
  }, [load, refreshAgents]);

  return {
    apiChannel,
    loading,
    starting,
    error,
    start,
    refresh: load,
  };
}

import { useCallback, useEffect, useState } from "react";
import { useAgentSession } from "../context/AgentSessionContext";
import { getRuntime, startRuntime } from "./api";
import type { AgentRuntimeStatus } from "../types";

export function useApiChannel() {
  const { selectedAgent, refresh: refreshAgents } = useAgentSession();
  const [runtime, setRuntime] = useState<AgentRuntimeStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      const data = await getRuntime();
      setRuntime(data);
      setError("");
      return data;
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setRuntime(null);
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
    const interval = window.setInterval(() => void load(), starting ? 1000 : 5000);
    return () => window.clearInterval(interval);
  }, [load, starting]);

  const start = useCallback(async () => {
    setStarting(true);
    setError("");
    try {
      await startRuntime();
      await load();
      await refreshAgents();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setStarting(false);
    }
  }, [load, refreshAgents]);

  return { runtime, loading, starting, error, start, refresh: load };
}

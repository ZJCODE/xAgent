import { useEffect, useState } from "react";
import { useAgentSession } from "../context/AgentSessionContext";
import { getHealth } from "./api";

export type ApiHealth = "checking" | "online" | "degraded" | "offline";

export function useApiHealth(): ApiHealth {
  const { selectedAgent, loading: agentsLoading } = useAgentSession();
  const [health, setHealth] = useState<ApiHealth>("checking");

  useEffect(() => {
    if (agentsLoading) {
      setHealth("checking");
      return;
    }
    let cancelled = false;
    const check = async () => {
      try {
        const result = await getHealth();
        if (!cancelled) setHealth(["running", "healthy"].includes(result.status) ? "online" : result.status === "degraded" ? "degraded" : "offline");
      } catch {
        if (!cancelled) setHealth("offline");
      }
    };
    void check();
    const interval = window.setInterval(() => void check(), 5000);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [selectedAgent, agentsLoading]);

  return health;
}

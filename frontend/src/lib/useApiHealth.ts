import { useEffect, useState } from "react";
import { useAgentSession } from "../context/AgentSessionContext";
import { getHealth } from "./api";

export type ApiHealth = "checking" | "online" | "offline";

export function useApiHealth(): ApiHealth {
  const { agents, selectedAgent, loading: agentsLoading } = useAgentSession();
  const [health, setHealth] = useState<ApiHealth>("checking");

  useEffect(() => {
    if (agentsLoading) {
      setHealth("checking");
      return;
    }

    const selected = agents.find((agent) => agent.name === selectedAgent);
    if (selected?.channel_running) {
      setHealth("online");
      return;
    }

    let cancelled = false;
    getHealth()
      .then(() => {
        if (!cancelled) setHealth("online");
      })
      .catch(() => {
        if (!cancelled) setHealth("offline");
      });
    return () => {
      cancelled = true;
    };
  }, [agents, selectedAgent, agentsLoading]);

  return health;
}

import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { getAgents, selectAgent } from "../lib/api";
import type { AgentSummary } from "../types";

interface AgentSessionContextValue {
  agents: AgentSummary[];
  selectedAgent: string;
  activeAgent: string;
  loading: boolean;
  error: string;
  switchAgent: (name: string) => Promise<void>;
  refresh: () => Promise<void>;
}

const AgentSessionContext = createContext<AgentSessionContextValue | null>(null);

export function AgentSessionProvider({ children }: { children: ReactNode }) {
  const [agents, setAgents] = useState<AgentSummary[]>([]);
  const [selectedAgent, setSelectedAgent] = useState("");
  const [activeAgent, setActiveAgent] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      const data = await getAgents();
      setAgents(data.agents);
      setSelectedAgent(data.selected_agent);
      setActiveAgent(data.active_agent);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const switchAgent = useCallback(async (name: string) => {
    if (name === selectedAgent) return;
    await selectAgent(name);
    // A full reload keeps every page's local state consistent with the
    // newly selected agent, rather than trying to reset each page's state.
    window.location.reload();
  }, [selectedAgent]);

  const value = useMemo(
    () => ({ agents, selectedAgent, activeAgent, loading, error, switchAgent, refresh }),
    [agents, selectedAgent, activeAgent, loading, error, switchAgent, refresh],
  );

  return <AgentSessionContext.Provider value={value}>{children}</AgentSessionContext.Provider>;
}

export function useAgentSession() {
  const value = useContext(AgentSessionContext);
  if (!value) throw new Error("useAgentSession must be used inside AgentSessionProvider");
  return value;
}

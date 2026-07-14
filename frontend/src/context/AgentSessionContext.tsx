import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { createAgent, deleteAgent, getAgents, selectAgent } from "../lib/api";
import type { AgentSummary, CreateAgentInput } from "../types";
import { useUnsavedChanges } from "./UnsavedChangesContext";

interface AgentSessionContextValue {
  agents: AgentSummary[];
  selectedAgent: string;
  activeAgent: string;
  loading: boolean;
  error: string;
  switchAgent: (name: string) => Promise<void>;
  createAgent: (input: CreateAgentInput) => Promise<void>;
  deleteAgent: (name: string, confirm: string) => Promise<void>;
  refresh: () => Promise<void>;
}

const AgentSessionContext = createContext<AgentSessionContextValue | null>(null);

export function AgentSessionProvider({ children }: { children: ReactNode }) {
  const { confirmDiscard } = useUnsavedChanges();
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
    if (!confirmDiscard()) return;
    await selectAgent(name);
    window.location.reload();
  }, [confirmDiscard, selectedAgent]);

  const createAgentEntry = useCallback(async (input: CreateAgentInput) => {
    await createAgent(input);
    window.location.reload();
  }, []);

  const deleteAgentEntry = useCallback(async (name: string, confirm: string) => {
    await deleteAgent(name, confirm);
    window.location.reload();
  }, []);

  const value = useMemo(
    () => ({
      agents,
      selectedAgent,
      activeAgent,
      loading,
      error,
      switchAgent,
      createAgent: createAgentEntry,
      deleteAgent: deleteAgentEntry,
      refresh,
    }),
    [agents, selectedAgent, activeAgent, loading, error, switchAgent, createAgentEntry, deleteAgentEntry, refresh],
  );

  return <AgentSessionContext.Provider value={value}>{children}</AgentSessionContext.Provider>;
}

export function useAgentSession() {
  const value = useContext(AgentSessionContext);
  if (!value) throw new Error("useAgentSession must be used inside AgentSessionProvider");
  return value;
}

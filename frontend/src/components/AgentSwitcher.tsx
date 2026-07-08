import { Bot, ChevronDown, Plus } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useAgentSession } from "../context/AgentSessionContext";
import { classNames } from "../lib/format";
import { Button, StatusBadge } from "./ui";
import { CreateAgentWizard } from "./CreateAgentWizard";

export function AgentSwitcher() {
  const { agents, selectedAgent, loading, error, refresh, switchAgent } = useAgentSession();
  const [open, setOpen] = useState(false);
  const [wizardOpen, setWizardOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  const current = useMemo(
    () => agents.find((agent) => agent.name === selectedAgent) ?? agents[0],
    [agents, selectedAgent],
  );

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    window.addEventListener("mousedown", onPointerDown);
    return () => window.removeEventListener("mousedown", onPointerDown);
  }, [open]);

  const openWizard = () => {
    setOpen(false);
    setWizardOpen(true);
  };

  if (loading) {
    return (
      <div className="agent-switcher">
        <span className="agent-switcher-label">Agent</span>
        <div className="agent-switcher-trigger muted">Loading...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="agent-switcher">
        <span className="agent-switcher-label">Agent</span>
        <div className="agent-switcher-empty">
          <div className="agent-switcher-empty-icon" aria-hidden="true">
            <Bot size={18} />
          </div>
          <p className="agent-switcher-empty-title">Cannot reach service</p>
          <p className="agent-switcher-empty-copy">{error}</p>
          <Button type="button" variant="primary" className="agent-switcher-empty-button" onClick={() => void refresh()}>
            Retry
          </Button>
        </div>
      </div>
    );
  }

  if (agents.length === 0) {
    return (
      <div className="agent-switcher">
        <span className="agent-switcher-label">Agent</span>
        <div className="agent-switcher-empty">
          <div className="agent-switcher-empty-icon" aria-hidden="true">
            <Bot size={18} />
          </div>
          <p className="agent-switcher-empty-title">Create your first agent</p>
          <p className="agent-switcher-empty-copy">Set up an agent to start chatting and managing channels.</p>
          <Button type="button" variant="primary" className="agent-switcher-empty-button" onClick={openWizard}>
            <Plus size={14} />
            Create agent
          </Button>
        </div>
        <CreateAgentWizard open={wizardOpen} onClose={() => setWizardOpen(false)} />
      </div>
    );
  }

  return (
    <div className="agent-switcher" ref={rootRef}>
      <span className="agent-switcher-label">Agent</span>
      <button
        type="button"
        className={classNames("agent-switcher-trigger", open && "open")}
        aria-expanded={open}
        aria-haspopup="listbox"
        onClick={() => setOpen((value) => !value)}
      >
        <span className="agent-switcher-current">
          <strong>{current?.name ?? "Select agent"}</strong>
        </span>
        <ChevronDown size={15} />
      </button>

      {open ? (
        <div className="agent-switcher-menu" role="listbox">
          {agents.map((agent) => (
            <button
              key={agent.name}
              type="button"
              role="option"
              aria-selected={agent.selected}
              className={classNames("agent-switcher-option", agent.selected && "selected")}
              onClick={() => {
                setOpen(false);
                if (!agent.selected) void switchAgent(agent.name);
              }}
            >
              <div className="agent-switcher-option-copy">
                <strong>{agent.name}</strong>
              </div>
              <div className="agent-switcher-option-meta">
                {agent.channel_running ? (
                  <StatusBadge tone="good" className="agent-switcher-badge">
                    Running
                  </StatusBadge>
                ) : null}
              </div>
            </button>
          ))}
          <button type="button" className="agent-switcher-new" onClick={openWizard}>
            <Plus size={14} />
            New agent
          </button>
        </div>
      ) : null}

      <CreateAgentWizard open={wizardOpen} onClose={() => setWizardOpen(false)} />
    </div>
  );
}

import { classNames } from "../lib/format";
import { initialOf } from "./protocol";
import { useWorld } from "./WorldContext";
import { WorldSwitcher } from "./WorldSwitcher";
import type { NeighborAgent } from "./protocol";

function agentStatus(agent: NeighborAgent, here: boolean): { label: string; tone: string } {
  if (here) return { label: "Present", tone: "is-here" };
  if (agent.world_ready) return { label: "Ready", tone: "is-ready" };
  if (agent.running) return { label: "Restart API", tone: "is-stale" };
  return { label: "Offline", tone: "" };
}

export function WorldSidebar() {
  const {
    gateError,
    agentError,
    present,
    neighbors,
    memberId,
    sidebarOpen,
    knockAgent,
  } = useWorld();

  const sorted = [...neighbors].sort((left, right) => {
    const rank = (agent: NeighborAgent) => {
      const here = present.some((item) => item.member_id === agent.name);
      if (here) return 0;
      if (agent.world_ready) return 1;
      if (agent.running) return 2;
      return 3;
    };
    return rank(left) - rank(right);
  });

  return (
    <aside className={classNames("app-sidebar world-sidebar", sidebarOpen && "open")}>
      <div className="world-sidebar-top">
        <WorldSwitcher />
        {gateError ? <p className="world-error">{gateError}</p> : null}
      </div>

      <div className="world-sidebar-scroll">
        <section className="world-section">
          <h2>Present</h2>
          {present.length ? (
            <ul className="world-roster">
              {present.map((person) => {
                const name = person.display_name || person.member_id;
                return (
                  <li key={person.member_id} className="world-roster-item">
                    <span className="world-avatar">{initialOf(name)}</span>
                    <span className="world-roster-name">
                      {person.member_id === memberId ? "you" : name}
                    </span>
                  </li>
                );
              })}
            </ul>
          ) : (
            <p className="world-hint">(empty)</p>
          )}
        </section>

        <section className="world-section">
          <h2>Local agents</h2>
          {sorted.length ? (
            <ul className="world-agents">
              {sorted.map((agent) => {
                const here = present.some((item) => item.member_id === agent.name);
                const status = agentStatus(agent, here);
                return (
                  <li key={agent.name} className={classNames("world-agent", status.tone)}>
                    <span className="world-agent-avatar">{initialOf(agent.title || agent.name)}</span>
                    <div className="world-agent-copy">
                      <strong>{agent.title || agent.name}</strong>
                      {status.label !== "Offline" ? <span>{status.label}</span> : null}
                    </div>
                    {here ? (
                      <button type="button" className="world-agent-action" onClick={() => void knockAgent(agent, "leave")}>
                        Dismiss
                      </button>
                    ) : agent.world_ready ? (
                      <button type="button" className="world-agent-action is-primary" onClick={() => void knockAgent(agent, "join")}>
                        Invite
                      </button>
                    ) : null}
                  </li>
                );
              })}
            </ul>
          ) : (
            <p className="world-hint">No agents found in ~/.xagent</p>
          )}
          {agentError ? <p className="world-error">{agentError}</p> : null}
        </section>
      </div>
    </aside>
  );
}

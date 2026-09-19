import { Bot, Pencil } from "lucide-react";
import { classNames } from "../lib/format";
import { DEFAULT_AGENT_WEB_URL } from "../lib/ports";
import { initialOf } from "./protocol";
import { useWorld } from "./WorldContext";
import { WorldSwitcher } from "./WorldSwitcher";
import type { NeighborAgent, WorldMember } from "./protocol";

function agentStatus(agent: NeighborAgent, here: boolean): { label: string; tone: string } {
  if (here) return { label: "Present", tone: "is-here" };
  if (agent.world_ready) return { label: "Ready", tone: "is-ready" };
  if (agent.running) return { label: "Restart API", tone: "is-stale" };
  return { label: "Offline", tone: "" };
}

function RosterList({
  people,
  memberId,
  displayOf,
}: {
  people: WorldMember[];
  memberId: string;
  displayOf: (id: string) => string;
}) {
  if (!people.length) {
    return <p className="world-hint">(empty)</p>;
  }
  return (
    <ul className="world-roster">
      {people.map((person) => {
        const name = person.display_name || displayOf(person.member_id);
        const mine = person.member_id === memberId;
        return (
          <li key={person.member_id} className="world-roster-item">
            <span className="world-avatar">{initialOf(name)}</span>
            <span className="world-roster-name">{mine ? `you · ${name}` : name}</span>
          </li>
        );
      })}
    </ul>
  );
}

export function WorldSidebar() {
  const {
    gateError,
    agentError,
    present,
    neighbors,
    memberId,
    identity,
    sidebarOpen,
    knockAgent,
    displayOf,
    openIdentityDialog,
  } = useWorld();

  const agentNames = new Set(neighbors.map((agent) => agent.name));
  const peoplePresent = present.filter((item) => !agentNames.has(item.member_id));
  const agentsPresent = present.filter((item) => agentNames.has(item.member_id));

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
        {identity ? (
          <button type="button" className="world-hint world-identity-edit" onClick={openIdentityDialog}>
            <Pencil size={12} aria-hidden />
            <span>
              You: <strong>{identity.display_name}</strong> ({identity.member_id})
            </span>
          </button>
        ) : (
          <button type="button" className="world-hint world-identity-edit" onClick={openIdentityDialog}>
            Set your name
          </button>
        )}
        {gateError ? <p className="world-error">{gateError}</p> : null}
      </div>

      <div className="world-sidebar-scroll">
        <section className="world-section">
          <h2>People</h2>
          <RosterList people={peoplePresent} memberId={memberId} displayOf={displayOf} />
        </section>

        {agentsPresent.length ? (
          <section className="world-section">
            <h2>Agents here</h2>
            <RosterList people={agentsPresent} memberId={memberId} displayOf={displayOf} />
          </section>
        ) : null}

        <section className="world-section">
          <h2>Local agents</h2>
          <p className="world-hint world-agent-hint">
            Invite joins this world. Dismiss leaves the world without stopping the agent API.
          </p>
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
                      <button
                        type="button"
                        className="world-agent-action"
                        onClick={() => void knockAgent(agent, "leave")}
                      >
                        Dismiss
                      </button>
                    ) : agent.world_ready ? (
                      <button
                        type="button"
                        className="world-agent-action is-primary"
                        onClick={() => void knockAgent(agent, "join")}
                      >
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

      <div className="sidebar-footer">
        <a
          className="ui-button ui-button-ghost ui-icon-button"
          href={DEFAULT_AGENT_WEB_URL}
          target="_blank"
          rel="noreferrer"
          title="Open agent web"
          aria-label="Open agent web"
        >
          <Bot size={16} />
        </a>
      </div>
    </aside>
  );
}

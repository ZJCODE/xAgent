import { Button } from "../components/ui";
import { classNames } from "../lib/format";
import { initialOf } from "./protocol";
import { useWorld } from "./WorldContext";
import type { NeighborAgent } from "./protocol";

function agentStatus(agent: NeighborAgent, here: boolean): { label: string; tone: string } {
  if (here) return { label: "在场", tone: "is-here" };
  if (agent.world_ready) return { label: "可进大厅", tone: "is-ready" };
  if (agent.running) return { label: "需重启 API", tone: "is-stale" };
  return { label: "未运行", tone: "" };
}

export function WorldSidebar() {
  const {
    draftName,
    setDraftName,
    connected,
    gateError,
    agentError,
    worldLabel,
    roomTitle,
    roomId,
    setting,
    present,
    neighbors,
    memberId,
    sidebarOpen,
    enterHall,
    leaveHall,
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
        <div className="agent-switcher">
          <div className="agent-switcher-label">World</div>
          <div className="agent-switcher-current">
            <strong>world</strong>
            <span>{worldLabel}</span>
          </div>
        </div>
        <label className="world-field">
          <span>你的名字</span>
          <input
            className="world-input"
            value={draftName}
            disabled={connected}
            autoComplete="off"
            spellCheck={false}
            onChange={(event) => setDraftName(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") void enterHall();
            }}
          />
        </label>
        {gateError ? <p className="world-error">{gateError}</p> : null}
      </div>

      <div className="world-sidebar-scroll">
        <section className="world-section">
          <h2>房间</h2>
          <div className="world-room-card">
            <strong>
              {roomTitle} · {roomId}
            </strong>
            <p>{setting || "进入后会看到房间设定"}</p>
          </div>
        </section>

        <section className="world-section">
          <h2>在场</h2>
          {present.length ? (
            <ul className="world-roster">
              {present.map((person) => {
                const name = person.display_name || person.member_id;
                return (
                  <li key={person.member_id} className="world-roster-item">
                    <span className="world-avatar">{initialOf(name)}</span>
                    <span className="world-roster-name">
                      {name}
                      {person.member_id === memberId ? " · 你" : ""}
                    </span>
                  </li>
                );
              })}
            </ul>
          ) : (
            <p className="world-hint">（空）</p>
          )}
        </section>

        <section className="world-section">
          <h2>本地 agent</h2>
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
                      {status.label !== "未运行" ? <span>{status.label}</span> : null}
                    </div>
                    {here ? (
                      <button type="button" className="world-agent-action" onClick={() => void knockAgent(agent, "leave")}>
                        请回
                      </button>
                    ) : agent.world_ready ? (
                      <button type="button" className="world-agent-action is-primary" onClick={() => void knockAgent(agent, "join")}>
                        请来
                      </button>
                    ) : null}
                  </li>
                );
              })}
            </ul>
          ) : (
            <p className="world-hint">没有在 ~/.xagent 里找到 agent</p>
          )}
          {agentError ? <p className="world-error">{agentError}</p> : null}
        </section>
      </div>

      <div className="sidebar-footer">
        {connected ? (
          <Button type="button" variant="secondary" onClick={leaveHall}>
            离开
          </Button>
        ) : (
          <Button type="button" variant="primary" onClick={() => void enterHall()}>
            进入大厅
          </Button>
        )}
      </div>
    </aside>
  );
}

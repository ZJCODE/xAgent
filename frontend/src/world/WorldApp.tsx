import { Menu, Wifi, WifiOff } from "lucide-react";
import { IconButton, StatusBadge } from "../components/ui";
import { classNames } from "../lib/format";
import { PersonIdentityDialog } from "./PersonIdentityDialog";
import { WorldChat } from "./WorldChat";
import { isAgentKind, memberKind } from "./protocol";
import { WorldProvider, useWorld } from "./WorldContext";
import { WorldSidebar } from "./WorldSidebar";

function WorldLayout() {
  const { worldName, present, joined, status, statusKind, sidebarOpen, setSidebarOpen } = useWorld();
  const count = present.length;
  const peopleCount = present.filter((p) => !isAgentKind(memberKind(p))).length;
  const agentCount = present.filter((p) => isAgentKind(memberKind(p))).length;
  const subtitle = joined
    ? agentCount > 0 || present.some((p) => p.kind)
      ? `${peopleCount} people · ${agentCount} agents (${count} present)`
      : `${count} present`
    : "Select or create a world";
  const tone = statusKind === "ok" ? "good" : statusKind === "bad" ? "danger" : statusKind === "info" ? "info" : "muted";

  return (
    <div className="app-shell world-shell">
      <button
        type="button"
        className={classNames("world-backdrop", sidebarOpen && "open")}
        aria-label="Close sidebar"
        onClick={() => setSidebarOpen(false)}
      />
      <WorldSidebar />
      <div className="app-main">
        <header className="chat-toolbar">
          <div className="world-toolbar-start">
            <IconButton
              type="button"
              className="world-menu-button"
              title="Open sidebar"
              aria-label="Open sidebar"
              onClick={() => setSidebarOpen(true)}
            >
              <Menu size={16} />
            </IconButton>
            <div className="page-title-block">
              <h2>{worldName}</h2>
              <p>{subtitle}</p>
            </div>
          </div>
          <div className="chat-status-cluster">
            <StatusBadge tone={tone} className="chat-status-badge" title={status}>
              {statusKind === "ok" || statusKind === "info" ? <Wifi size={14} /> : <WifiOff size={14} />}
              <span className="status-badge-label">{joined ? "Present" : status}</span>
            </StatusBadge>
          </div>
        </header>
        <WorldChat />
      </div>
    </div>
  );
}

export function WorldApp() {
  return (
    <WorldProvider>
      <WorldLayout />
      <PersonIdentityDialog />
    </WorldProvider>
  );
}

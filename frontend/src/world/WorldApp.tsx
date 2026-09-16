import { Menu, Moon, Sun, Wifi, WifiOff } from "lucide-react";
import { IconButton, StatusBadge } from "../components/ui";
import { useTheme } from "../context/ThemeContext";
import { classNames } from "../lib/format";
import { WorldChat } from "./WorldChat";
import { WorldProvider, useWorld } from "./WorldContext";
import { WorldSidebar } from "./WorldSidebar";

function WorldLayout() {
  const { dark, toggleTheme } = useTheme();
  const { worldName, present, joined, status, statusKind, sidebarOpen, setSidebarOpen } = useWorld();
  const count = present.length;
  const subtitle = joined
    ? `${count} present`
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
            <IconButton type="button" onClick={toggleTheme} title="Toggle theme" aria-label="Toggle theme">
              {dark ? <Sun size={16} /> : <Moon size={16} />}
            </IconButton>
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
    </WorldProvider>
  );
}

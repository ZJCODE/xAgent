import { Menu, Moon, Sun, Wifi, WifiOff } from "lucide-react";
import { IconButton, StatusBadge } from "../components/ui";
import { useTheme } from "../context/ThemeContext";
import { classNames } from "../lib/format";
import { WorldChat } from "./WorldChat";
import { WorldProvider, useWorld } from "./WorldContext";
import { WorldSidebar } from "./WorldSidebar";

function WorldLayout() {
  const { dark, toggleTheme } = useTheme();
  const {
    roomTitle,
    setting,
    present,
    presentInRoom,
    status,
    statusKind,
    sidebarOpen,
    setSidebarOpen,
  } = useWorld();
  const count = present.length;
  const subtitle = presentInRoom
    ? setting
      ? `${count} 人在场 · ${setting}`
      : `${count} 人在场`
    : "从左侧进入后即可说话";
  const tone = statusKind === "ok" ? "good" : statusKind === "bad" ? "danger" : statusKind === "info" ? "info" : "muted";

  return (
    <div className="app-shell world-shell">
      <button
        type="button"
        className={classNames("world-backdrop", sidebarOpen && "open")}
        aria-label="关闭侧栏"
        onClick={() => setSidebarOpen(false)}
      />
      <WorldSidebar />
      <div className="app-main">
        <header className="chat-toolbar">
          <div className="world-toolbar-start">
            <IconButton
              type="button"
              className="world-menu-button"
              title="打开侧栏"
              aria-label="打开侧栏"
              onClick={() => setSidebarOpen(true)}
            >
              <Menu size={16} />
            </IconButton>
            <div className="page-title-block">
              <h2>{roomTitle}</h2>
              <p>{subtitle}</p>
            </div>
          </div>
          <div className="chat-status-cluster">
            <StatusBadge tone={tone} className="chat-status-badge">
              {statusKind === "ok" || statusKind === "info" ? <Wifi size={14} /> : <WifiOff size={14} />}
              <span className="status-badge-label">{status}</span>
            </StatusBadge>
            <IconButton type="button" onClick={toggleTheme} title="切换主题" aria-label="切换主题">
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

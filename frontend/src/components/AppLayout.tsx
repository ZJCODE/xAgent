import {
  Activity,
  Bot,
  Database,
  Files,
  ListTodo,
  MessageSquareText,
  Moon,
  Package,
  RadioTower,
  Sun,
  Wifi,
  WifiOff,
} from "lucide-react";
import type { ReactNode } from "react";
import { useAgentSession } from "../context/AgentSessionContext";
import { useTheme } from "../context/ThemeContext";
import { classNames } from "../lib/format";
import type { RoutePath } from "../types";
import { IconButton, StatusBadge } from "./ui";

interface AppLayoutProps {
  route: RoutePath;
  health: "checking" | "online" | "offline";
  chatStatus: "idle" | "sending";
  onNavigate: (route: RoutePath) => void;
  onToggleTheme: () => void;
  children: ReactNode;
}

const navItems: Array<{ route: RoutePath; label: string; icon: ReactNode }> = [
  { route: "/", label: "Chat", icon: <MessageSquareText size={15} /> },
  { route: "/memory", label: "Memory", icon: <Database size={15} /> },
  { route: "/message", label: "Messages", icon: <Activity size={15} /> },
  { route: "/workspace", label: "Workspace", icon: <Files size={15} /> },
  { route: "/skills", label: "Skills", icon: <Package size={15} /> },
  { route: "/tasks", label: "Tasks", icon: <ListTodo size={15} /> },
  { route: "/channels", label: "Channels", icon: <RadioTower size={15} /> },
  { route: "/agent", label: "Agent", icon: <Bot size={15} /> },
];

export function AppLayout({
  route,
  health,
  chatStatus,
  onNavigate,
  onToggleTheme,
  children,
}: AppLayoutProps) {
  const { dark } = useTheme();
  const { agents, selectedAgent, switchAgent } = useAgentSession();

  return (
    <div className="app-shell">
      <aside className="app-sidebar">
        {agents.length > 0 ? (
          <div className="agent-switcher">
            <label htmlFor="agent-switcher-select">Agent</label>
            <select
              id="agent-switcher-select"
              value={selectedAgent}
              onChange={(event) => void switchAgent(event.target.value)}
            >
              {agents.map((agent) => (
                <option key={agent.name} value={agent.name}>
                  {agent.name}
                </option>
              ))}
            </select>
          </div>
        ) : null}
        <nav className="app-nav" aria-label="Primary">
          {navItems.map((item) => (
            <button
              key={item.route}
              type="button"
              className={classNames("nav-link", route === item.route && "active")}
              onClick={() => onNavigate(item.route)}
            >
              {item.icon}
              <span>{item.label}</span>
            </button>
          ))}
        </nav>
      </aside>

      <div className="app-main">
        <header className="app-topbar">
          <div className="status-cluster">
            <StatusBadge
              tone={health === "online" ? "good" : health === "offline" ? "danger" : "muted"}
              title="Reflects the selected agent's api channel (chat). Other tabs work independently."
            >
              {health === "online" ? <Wifi size={14} /> : <WifiOff size={14} />}
              {health === "checking" ? "Checking" : health === "online" ? "API Online" : "API Offline"}
            </StatusBadge>
            <StatusBadge tone={chatStatus === "sending" ? "info" : "muted"}>
              <Activity size={14} />
              {chatStatus === "sending" ? "Chat running" : "Chat idle"}
            </StatusBadge>
          </div>
          <div className="topbar-actions">
            <IconButton type="button" onClick={onToggleTheme} title="Toggle theme" aria-label="Toggle theme">
              {dark ? <Sun size={16} /> : <Moon size={16} />}
            </IconButton>
          </div>
        </header>
        <main className="app-content">{children}</main>
      </div>
    </div>
  );
}

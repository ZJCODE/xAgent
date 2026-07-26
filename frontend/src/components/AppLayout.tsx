import {
  Brain,
  CircleHelp,
  History,
  LayoutDashboard,
  ListTodo,
  MessageSquareText,
  Moon,
  RadioTower,
  ShieldAlert,
  SlidersHorizontal,
  Sun,
  WifiOff,
} from "lucide-react";
import { useState, type ReactNode } from "react";
import { useAgentSession } from "../context/AgentSessionContext";
import { useConnectivity } from "../context/ConnectivityContext";
import { useTheme } from "../context/ThemeContext";
import { classNames } from "../lib/format";
import type { RoutePath } from "../types";
import { AgentSwitcher } from "./AgentSwitcher";
import { HelpDialog } from "./HelpDialog";
import { Button, IconButton } from "./ui";

const navGroups: Array<{
  label: string;
  items: Array<{ route: RoutePath; label: string; icon: ReactNode }>;
}> = [
  {
    label: "Agent",
    items: [
      { route: "/", label: "Overview", icon: <LayoutDashboard size={17} /> },
      { route: "/chat", label: "Chat", icon: <MessageSquareText size={17} /> },
      { route: "/messages", label: "Messages", icon: <History size={17} /> },
      { route: "/memory", label: "Memory", icon: <Brain size={17} /> },
    ],
  },
  {
    label: "System",
    items: [
      { route: "/tasks", label: "Tasks", icon: <ListTodo size={17} /> },
      { route: "/channels", label: "Channels", icon: <RadioTower size={17} /> },
      { route: "/deliveries", label: "Deliveries", icon: <ShieldAlert size={17} /> },
      { route: "/settings", label: "Settings", icon: <SlidersHorizontal size={17} /> },
    ],
  },
];

export function AppLayout({
  route,
  onNavigate,
  children,
}: {
  route: RoutePath;
  onNavigate: (route: RoutePath) => void;
  children: ReactNode;
}) {
  const { dark, toggleTheme } = useTheme();
  const { agents, selectedAgent } = useAgentSession();
  const { webStatus, retry } = useConnectivity();
  const [helpOpen, setHelpOpen] = useState(false);
  const currentAgent = agents.find((agent) => agent.name === selectedAgent) || agents[0];

  return (
    <div className="app-shell">
      <aside className="app-sidebar">
        <div className="app-brand" aria-label="xAgent">
          <span className="app-brand-mark">x</span>
          <span><strong>xAgent</strong><small>local control</small></span>
        </div>

        <AgentSwitcher />

        <nav className="app-nav" aria-label="Primary navigation">
          {navGroups.map((group) => (
            <div className="nav-group" key={group.label}>
              <span className="nav-group-label">{group.label}</span>
              {group.items.map((item) => (
                <button
                  key={item.route}
                  type="button"
                  className={classNames("nav-link", route === item.route && "active")}
                  aria-current={route === item.route ? "page" : undefined}
                  onClick={() => onNavigate(item.route)}
                >
                  {item.icon}<span>{item.label}</span>
                </button>
              ))}
            </div>
          ))}
        </nav>

        <div className="sidebar-runtime">
          <span className={`status-dot ${currentAgent?.runtime_running ? "status-running" : "status-stopped"}`} />
          <span>
            <strong>{currentAgent?.runtime_running ? "Runtime active" : "Runtime stopped"}</strong>
            <small>{currentAgent?.runtime_running && currentAgent.pid ? `PID ${currentAgent.pid}` : "Manage from Overview"}</small>
          </span>
        </div>

        <div className="sidebar-footer">
          <IconButton type="button" onClick={() => setHelpOpen(true)} title="About local control" aria-label="About local control">
            <CircleHelp size={17} />
          </IconButton>
          <IconButton type="button" onClick={toggleTheme} title="Toggle theme" aria-label="Toggle theme">
            {dark ? <Sun size={17} /> : <Moon size={17} />}
          </IconButton>
        </div>
      </aside>

      <div className="app-main">
        {webStatus === "offline" ? (
          <div className="connectivity-banner" role="status">
            <WifiOff size={16} />
            <span><strong>Web service disconnected.</strong> The browser cannot reach the local xAgent control surface.</span>
            <Button type="button" onClick={() => void retry()}>Retry</Button>
          </div>
        ) : null}
        <main className="app-content">{children}</main>
      </div>

      <HelpDialog open={helpOpen} currentAgent={currentAgent?.name || ""} onClose={() => setHelpOpen(false)} />
    </div>
  );
}

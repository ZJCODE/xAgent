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
  WifiOff,
} from "lucide-react";
import type { ReactNode } from "react";
import { useConnectivity } from "../context/ConnectivityContext";
import { useTheme } from "../context/ThemeContext";
import { classNames } from "../lib/format";
import type { RoutePath } from "../types";
import { AgentSwitcher } from "./AgentSwitcher";
import { Button, IconButton } from "./ui";

interface AppLayoutProps {
  route: RoutePath;
  onNavigate: (route: RoutePath) => void;
  children: ReactNode;
}

const navItems: Array<{ route: RoutePath; label: string; icon: ReactNode }> = [
  { route: "/", label: "Chat", icon: <MessageSquareText size={15} /> },
  { route: "/message", label: "Messages", icon: <Activity size={15} /> },
  { route: "/workspace", label: "Workspace", icon: <Files size={15} /> },
  { route: "/memory", label: "Memory", icon: <Database size={15} /> },
  { route: "/skills", label: "Skills", icon: <Package size={15} /> },
  { route: "/tasks", label: "Tasks", icon: <ListTodo size={15} /> },
  { route: "/channels", label: "Channels", icon: <RadioTower size={15} /> },
  { route: "/agent", label: "Agent", icon: <Bot size={15} /> },
];

export function AppLayout({ route, onNavigate, children }: AppLayoutProps) {
  const { dark, toggleTheme } = useTheme();
  const { webStatus, retry } = useConnectivity();
  const showConnectivityBanner = webStatus === "offline";

  return (
    <div className="app-shell">
      <aside className="app-sidebar">
        <AgentSwitcher />
        <nav className="app-nav" aria-label="Primary">
          {navItems.map((item) => (
            <button
              key={item.route}
              type="button"
              className={classNames("nav-link", route === item.route && "active")}
              aria-label={item.label}
              title={item.label}
              aria-current={route === item.route ? "page" : undefined}
              onClick={() => onNavigate(item.route)}
            >
              {item.icon}
              <span>{item.label}</span>
            </button>
          ))}
        </nav>
        <div className="sidebar-footer">
          <IconButton type="button" onClick={toggleTheme} title="Toggle theme" aria-label="Toggle theme">
            {dark ? <Sun size={16} /> : <Moon size={16} />}
          </IconButton>
        </div>
      </aside>

      <div className="app-main">
        {showConnectivityBanner ? (
          <div className="connectivity-banner" role="status">
            <WifiOff size={15} />
            <span>Cannot reach the local xAgent service.</span>
            <Button type="button" variant="secondary" className="connectivity-banner-button" onClick={() => void retry()}>
              Retry
            </Button>
          </div>
        ) : null}
        <main className="app-content">{children}</main>
      </div>
    </div>
  );
}

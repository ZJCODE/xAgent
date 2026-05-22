import { Activity, Bot, Database, Files, MessageSquareText, Moon, Package, Wifi, WifiOff } from "lucide-react";
import type { ReactNode } from "react";
import { useChat } from "../context/ChatContext";
import { classNames } from "../lib/format";
import type { RoutePath } from "../types";

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
  const { clearVisiblePanels } = useChat();

  return (
    <div className="relative h-full overflow-hidden bg-app text-zinc-950 dark:text-zinc-50">
      <div className="hero-grid fixed inset-0 pointer-events-none opacity-70" />
      <div className="relative h-full max-w-7xl mx-auto px-3 sm:px-5 py-4 sm:py-6 flex flex-col">
        <section className="panel-surface flex-1 min-h-0 rounded-[28px] overflow-hidden flex flex-col">
          <header className="border-b border-black/5 dark:border-white/10 px-4 sm:px-6 py-4">
            <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
              <div className="flex min-w-0 flex-col gap-3 sm:flex-row sm:items-center">
                <h1 className="text-xl sm:text-2xl font-semibold tracking-tight">xAgent</h1>
                <nav className="flex flex-wrap items-center gap-1">
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
              </div>

              <div className="flex flex-wrap items-center gap-2">
                <span className="ghost-button status-pill" title="Server status">
                  {health === "online" ? <Wifi size={14} /> : <WifiOff size={14} />}
                  {health === "checking" ? "Checking" : health === "online" ? "Online" : "Offline"}
                </span>
                <span className={classNames("ghost-button status-pill", chatStatus === "sending" && "status-live")}>
                  <Activity size={14} />
                  {chatStatus === "sending" ? "Chat running" : "Chat idle"}
                </span>
                {route === "/" && (
                  <button type="button" className="ghost-button icon-text-button" onClick={clearVisiblePanels}>
                    Clear Chat
                  </button>
                )}
                <button type="button" className="ghost-button icon-button" onClick={onToggleTheme} title="Toggle theme">
                  <Moon size={16} />
                </button>
              </div>
            </div>
          </header>
          <main className="flex-1 min-h-0 overflow-hidden">{children}</main>
        </section>
      </div>
    </div>
  );
}

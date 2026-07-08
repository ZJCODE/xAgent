import { useEffect, useMemo, useState } from "react";
import { AgentSessionProvider, useAgentSession } from "./context/AgentSessionContext";
import { ChatProvider, useChat } from "./context/ChatContext";
import { ThemeProvider, useTheme } from "./context/ThemeContext";
import { getHealth } from "./lib/api";
import type { RoutePath } from "./types";
import { AgentPage } from "./pages/AgentPage";
import { ChannelPage } from "./pages/ChannelPage";
import { ChatPage } from "./pages/ChatPage";
import { MemoryPage } from "./pages/MemoryPage";
import { MessagePage } from "./pages/MessagePage";
import { SkillsPage } from "./pages/SkillsPage";
import { TasksPage } from "./pages/TasksPage";
import { WorkspacePage } from "./pages/WorkspacePage";
import { AppLayout } from "./components/AppLayout";

const routeSet = new Set<RoutePath>(["/", "/memory", "/message", "/workspace", "/skills", "/tasks", "/channels", "/agent"]);

function normalizeRoute(pathname: string): RoutePath {
  return routeSet.has(pathname as RoutePath) ? (pathname as RoutePath) : "/";
}

function RoutedApp() {
  const [route, setRoute] = useState<RoutePath>(() => normalizeRoute(window.location.pathname));
  const [health, setHealth] = useState<"checking" | "online" | "offline">("checking");
  const { toggleTheme } = useTheme();
  const { status } = useChat();
  const { agents, selectedAgent, loading: agentsLoading, refresh: refreshAgents } = useAgentSession();

  useEffect(() => {
    const onPopState = () => setRoute(normalizeRoute(window.location.pathname));
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  useEffect(() => {
    const interval = window.setInterval(() => void refreshAgents(), 5000);
    return () => window.clearInterval(interval);
  }, [refreshAgents]);

  useEffect(() => {
    if (agentsLoading) {
      setHealth("checking");
      return;
    }

    const selected = agents.find((agent) => agent.name === selectedAgent);
    if (selected?.channel_running) {
      setHealth("online");
      return;
    }

    let cancelled = false;
    getHealth()
      .then(() => {
        if (!cancelled) setHealth("online");
      })
      .catch(() => {
        if (!cancelled) setHealth("offline");
      });
    return () => {
      cancelled = true;
    };
  }, [agents, selectedAgent, agentsLoading]);

  const navigate = (nextRoute: RoutePath) => {
    if (nextRoute === route) return;
    window.history.pushState(null, "", nextRoute);
    setRoute(nextRoute);
  };

  const page = useMemo(() => {
    switch (route) {
      case "/memory":
        return <MemoryPage />;
      case "/message":
        return <MessagePage />;
      case "/workspace":
        return <WorkspacePage />;
      case "/skills":
        return <SkillsPage />;
      case "/tasks":
        return <TasksPage />;
      case "/channels":
        return <ChannelPage />;
      case "/agent":
        return <AgentPage />;
      case "/":
      default:
        return <ChatPage />;
    }
  }, [route]);

  return (
    <AppLayout
      route={route}
      health={health}
      chatStatus={status}
      onNavigate={navigate}
      onToggleTheme={toggleTheme}
    >
      {page}
    </AppLayout>
  );
}

export default function App() {
  return (
    <ThemeProvider>
      <AgentSessionProvider>
        <ChatProvider>
          <RoutedApp />
        </ChatProvider>
      </AgentSessionProvider>
    </ThemeProvider>
  );
}

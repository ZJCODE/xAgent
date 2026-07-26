import { useCallback, useEffect, useMemo, useState } from "react";
import { AgentSessionProvider, useAgentSession } from "./context/AgentSessionContext";
import { ChatProvider } from "./context/ChatContext";
import { ConnectivityProvider } from "./context/ConnectivityContext";
import { ThemeProvider } from "./context/ThemeContext";
import { UnsavedChangesProvider } from "./context/UnsavedChangesContext";
import { AppLayout } from "./components/AppLayout";
import { ChannelPage } from "./pages/ChannelPage";
import { ChatPage } from "./pages/ChatPage";
import { DeliveriesPage } from "./pages/DeliveriesPage";
import { MemoryPage } from "./pages/MemoryPage";
import { MessagesPage } from "./pages/MessagesPage";
import { OverviewPage } from "./pages/OverviewPage";
import { SettingsPage } from "./pages/SettingsPage";
import { TasksPage } from "./pages/TasksPage";
import { WelcomePage } from "./pages/WelcomePage";
import type { RoutePath } from "./types";

const routes = new Set<RoutePath>([
  "/",
  "/chat",
  "/messages",
  "/memory",
  "/tasks",
  "/channels",
  "/deliveries",
  "/settings",
]);
const route = (path: string): RoutePath => routes.has(path as RoutePath) ? path as RoutePath : "/";

function RoutedApp() {
  const [current, setCurrent] = useState<RoutePath>(() => route(window.location.pathname));
  const { agents, loading, refresh } = useAgentSession();

  useEffect(() => {
    const pop = () => setCurrent(route(window.location.pathname));
    window.addEventListener("popstate", pop);
    return () => window.removeEventListener("popstate", pop);
  }, []);

  useEffect(() => {
    const timer = window.setInterval(() => void refresh(), 10000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const navigate = useCallback((next: RoutePath) => {
    if (next === current) return;
    window.history.pushState(null, "", next);
    setCurrent(next);
  }, [current]);

  const page = useMemo(() => {
    if (!loading && agents.length === 0) return <WelcomePage />;
    if (current === "/chat") return <ChatPage />;
    if (current === "/messages") return <MessagesPage />;
    if (current === "/memory") return <MemoryPage />;
    if (current === "/tasks") return <TasksPage />;
    if (current === "/channels") return <ChannelPage />;
    if (current === "/deliveries") return <DeliveriesPage />;
    if (current === "/settings") return <SettingsPage onNavigate={navigate} />;
    return <OverviewPage onNavigate={navigate} />;
  }, [agents.length, current, loading, navigate]);

  return <AppLayout route={current} onNavigate={navigate}>{page}</AppLayout>;
}

export default function App() {
  return (
    <ThemeProvider>
      <ConnectivityProvider>
        <UnsavedChangesProvider>
          <AgentSessionProvider>
            <ChatProvider><RoutedApp /></ChatProvider>
          </AgentSessionProvider>
        </UnsavedChangesProvider>
      </ConnectivityProvider>
    </ThemeProvider>
  );
}

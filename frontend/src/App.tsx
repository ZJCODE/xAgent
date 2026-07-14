import { useEffect, useMemo, useRef, useState } from "react";
import { AgentSessionProvider, useAgentSession } from "./context/AgentSessionContext";
import { ChatProvider } from "./context/ChatContext";
import { ConnectivityProvider } from "./context/ConnectivityContext";
import { ThemeProvider } from "./context/ThemeContext";
import { UnsavedChangesProvider, useUnsavedChanges } from "./context/UnsavedChangesContext";
import type { RoutePath } from "./types";
import { AgentPage } from "./pages/AgentPage";
import { ChannelPage } from "./pages/ChannelPage";
import { ChatPage } from "./pages/ChatPage";
import { MemoryPage } from "./pages/MemoryPage";
import { MessagePage } from "./pages/MessagePage";
import { SkillsPage } from "./pages/SkillsPage";
import { TasksPage } from "./pages/TasksPage";
import { WelcomePage } from "./pages/WelcomePage";
import { WorkspacePage } from "./pages/WorkspacePage";
import { AppLayout } from "./components/AppLayout";

const routeSet = new Set<RoutePath>(["/", "/memory", "/message", "/workspace", "/skills", "/tasks", "/channels", "/agent"]);

function normalizeRoute(pathname: string): RoutePath {
  return routeSet.has(pathname as RoutePath) ? (pathname as RoutePath) : "/";
}

function RoutedApp() {
  const [route, setRoute] = useState<RoutePath>(() => normalizeRoute(window.location.pathname));
  const locationRef = useRef(`${window.location.pathname}${window.location.search}${window.location.hash}`);
  const { agents, loading, refresh: refreshAgents } = useAgentSession();
  const { confirmDiscard } = useUnsavedChanges();
  const showWelcome = route === "/" && !loading && agents.length === 0;

  useEffect(() => {
    const onPopState = () => {
      const nextRoute = normalizeRoute(window.location.pathname);
      if (nextRoute !== route && !confirmDiscard()) {
        window.history.pushState(null, "", locationRef.current);
        return;
      }
      locationRef.current = `${window.location.pathname}${window.location.search}${window.location.hash}`;
      setRoute(nextRoute);
    };
    const onLocationChange = () => {
      locationRef.current = `${window.location.pathname}${window.location.search}${window.location.hash}`;
    };
    window.addEventListener("popstate", onPopState);
    window.addEventListener("xagent:locationchange", onLocationChange);
    return () => {
      window.removeEventListener("popstate", onPopState);
      window.removeEventListener("xagent:locationchange", onLocationChange);
    };
  }, [confirmDiscard, route]);

  useEffect(() => {
    const interval = window.setInterval(() => void refreshAgents(), 5000);
    return () => window.clearInterval(interval);
  }, [refreshAgents]);

  const navigate = (nextRoute: RoutePath) => {
    if (nextRoute === route) return;
    if (!confirmDiscard()) return;
    window.history.pushState(null, "", nextRoute);
    locationRef.current = nextRoute;
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
        return showWelcome ? <WelcomePage /> : <ChatPage />;
    }
  }, [route, showWelcome]);

  return (
    <AppLayout route={route} onNavigate={navigate}>
      {page}
    </AppLayout>
  );
}

export default function App() {
  return (
    <ThemeProvider>
      <ConnectivityProvider>
        <UnsavedChangesProvider>
          <AgentSessionProvider>
            <ChatProvider>
              <RoutedApp />
            </ChatProvider>
          </AgentSessionProvider>
        </UnsavedChangesProvider>
      </ConnectivityProvider>
    </ThemeProvider>
  );
}

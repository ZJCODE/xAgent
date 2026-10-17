import { BookOpen, RefreshCw } from "lucide-react";
import { useCallback, useRef, useState } from "react";
import { DeviceConnectionHelp } from "../components/DeviceConnectionHelp";
import { DevicePanel } from "../components/DevicePanel";
import { Button, IconButton, PageShell, PageToolbar } from "../components/ui";
import { useAgentSession } from "../context/AgentSessionContext";

export function DevicesPage() {
  const { agents, selectedAgent } = useAgentSession();
  const [helpOpen, setHelpOpen] = useState(false);
  const [apiUrl, setApiUrl] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const refreshRef = useRef<(() => Promise<void>) | null>(null);

  const handleRefresh = useCallback(async () => {
    if (!refreshRef.current) return;
    setRefreshing(true);
    try {
      await refreshRef.current();
    } finally {
      setRefreshing(false);
    }
  }, []);

  const fallbackApiUrl = agents.find((agent) => agent.name === selectedAgent)?.api_url || "";

  return (
    <PageShell className="channels-page devices-page">
      <PageToolbar
        title="Devices"
        subtitle="Pair once. Reconnect automatically for control, sensors, and room voice."
        actions={
          <div className="toolbar-actions">
            <Button type="button" variant="ghost" onClick={() => setHelpOpen(true)}>
              <BookOpen size={15} />
              Connection Help
            </Button>
            <IconButton
              type="button"
              onClick={() => void handleRefresh()}
              disabled={refreshing}
              title="Refresh devices"
            >
              <RefreshCw size={16} />
            </IconButton>
          </div>
        }
      />
      <div className="channels-content devices-content">
        <DevicePanel
          standalone
          onOpenHelp={() => setHelpOpen(true)}
          onApiUrlChange={setApiUrl}
          registerRefresh={(fn) => {
            refreshRef.current = fn;
          }}
        />
      </div>
      <DeviceConnectionHelp
        open={helpOpen}
        currentAgent={selectedAgent}
        apiUrl={apiUrl || fallbackApiUrl}
        onClose={() => setHelpOpen(false)}
      />
    </PageShell>
  );
}

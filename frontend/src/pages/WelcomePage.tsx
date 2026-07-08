import { Bot, LoaderCircle, RadioTower } from "lucide-react";
import { useEffect, useState } from "react";
import { CreateAgentWizard } from "../components/CreateAgentWizard";
import { Button, EmptyState } from "../components/ui";
import { useAgentSession } from "../context/AgentSessionContext";
import { useConnectivity } from "../context/ConnectivityContext";

export function WelcomePage() {
  const { agents, loading, error, refresh } = useAgentSession();
  const { webStatus, retry } = useConnectivity();
  const [wizardOpen, setWizardOpen] = useState(false);

  useEffect(() => {
    if (agents.length > 0) {
      setWizardOpen(false);
    }
  }, [agents.length]);

  if (webStatus === "offline") {
    return (
      <EmptyState icon={<RadioTower size={24} />} title="Cannot reach xAgent">
        <p className="welcome-copy">The desktop app could not connect to the local xAgent service.</p>
        <Button type="button" variant="primary" onClick={() => void retry()}>
          Retry connection
        </Button>
      </EmptyState>
    );
  }

  if (loading || webStatus === "checking") {
    return (
      <EmptyState icon={<LoaderCircle size={24} className="spin-icon" />} title="Loading xAgent">
        Preparing your workspace...
      </EmptyState>
    );
  }

  if (error) {
    return (
      <EmptyState icon={<RadioTower size={24} />} title="Cannot load agents">
        <p className="welcome-copy">{error}</p>
        <Button type="button" variant="primary" onClick={() => void refresh()}>
          Retry
        </Button>
      </EmptyState>
    );
  }

  return (
    <div className="welcome-page">
      <div className="welcome-card">
        <div className="welcome-icon" aria-hidden="true">
          <Bot size={28} />
        </div>
        <h1>Create your first agent</h1>
        <p className="welcome-copy">
          xAgent is ready. Create an agent to start chatting, managing memory, and connecting channels.
        </p>
        <Button type="button" variant="primary" onClick={() => setWizardOpen(true)}>
          Create agent
        </Button>
      </div>
      <CreateAgentWizard open={wizardOpen} onClose={() => setWizardOpen(false)} />
    </div>
  );
}

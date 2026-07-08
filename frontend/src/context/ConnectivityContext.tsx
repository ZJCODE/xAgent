import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { getWebHealth } from "../lib/api";

export type ConnectionStatus = "checking" | "online" | "offline";

interface ConnectivityContextValue {
  webStatus: ConnectionStatus;
  retry: () => Promise<void>;
}

const ConnectivityContext = createContext<ConnectivityContextValue | null>(null);

export function ConnectivityProvider({ children }: { children: ReactNode }) {
  const [webStatus, setWebStatus] = useState<ConnectionStatus>("checking");

  const checkHealth = useCallback(async ({ showChecking }: { showChecking: boolean }) => {
    if (showChecking) {
      setWebStatus("checking");
    }
    try {
      const health = await getWebHealth();
      setWebStatus(health.web ? "online" : "offline");
    } catch {
      setWebStatus("offline");
    }
  }, []);

  const retry = useCallback(() => checkHealth({ showChecking: true }), [checkHealth]);

  useEffect(() => {
    void checkHealth({ showChecking: true });
    const interval = window.setInterval(() => void checkHealth({ showChecking: false }), 5000);
    return () => window.clearInterval(interval);
  }, [checkHealth]);

  const value = useMemo(
    () => ({
      webStatus,
      retry,
    }),
    [webStatus, retry],
  );

  return <ConnectivityContext.Provider value={value}>{children}</ConnectivityContext.Provider>;
}

export function useConnectivity() {
  const value = useContext(ConnectivityContext);
  if (!value) {
    throw new Error("useConnectivity must be used inside ConnectivityProvider");
  }
  return value;
}

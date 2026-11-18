import { BookOpen, Copy, Mic, Play, Plus, Plug, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { useAgentSession } from "../context/AgentSessionContext";
import {
  cancelDevicePairing,
  connectChannel,
  createDevicePairing,
  createReusableDevicePairing,
  disableReusableDevicePairing,
  getDevices,
  removeDevice,
  startRuntime,
} from "../lib/api";
import type { DevicePairing, DeviceStatus } from "../types";
import { Button, IconButton, Panel, PanelHeader, StatusBadge } from "./ui";
import { DeviceConnectionHelp } from "./DeviceConnectionHelp";
import { copyText } from "../lib/clipboard";
import { DeviceDetailsForm } from "./DeviceDetailsForm";

export interface DevicePanelProps {
  standalone?: boolean;
  onOpenHelp?: () => void;
  onApiUrlChange?: (apiUrl: string) => void;
  registerRefresh?: (fn: () => Promise<void>) => void;
}

export function DevicePanel({
  standalone = false,
  onOpenHelp,
  onApiUrlChange,
  registerRefresh,
}: DevicePanelProps = {}) {
  const { agents, selectedAgent, refresh: refreshAgents } = useAgentSession();
  const [helpOpen, setHelpOpen] = useState(false);
  const [status, setStatus] = useState<DeviceStatus | null>(null);
  const [pairing, setPairing] = useState<DevicePairing | null>(null);
  const [reusableCode, setReusableCode] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [editingId, setEditingId] = useState<string | null>(null);
  const previousIds = useRef<string[]>([]);
  const reusableTitleId = useId();
  const reusableCodeId = useId();

  const load = useCallback(async () => {
    const data = await getDevices();
    setStatus(data);
    if (!data.reusable_pairing_enabled) setReusableCode(null);
    if (data.api_url && onApiUrlChange) {
      onApiUrlChange(data.api_url);
    }
    return data;
  }, [onApiUrlChange]);

  const act = useCallback(async (operation: () => Promise<void>) => {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await operation();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    if (!registerRefresh) return;
    registerRefresh(async () => {
      await act(async () => {
        await load();
      });
    });
  }, [act, load, registerRefresh]);

  useEffect(() => {
    setStatus(null);
    setPairing(null);
    setReusableCode(null);
    setError("");
    setNotice("");
    setHelpOpen(false);
    setEditingId(null);
    void load().catch((err) => setError(String(err.message || err)));
    const interval = window.setInterval(() => {
      void load().catch(() => {});
    }, 5000);
    return () => window.clearInterval(interval);
  }, [load, selectedAgent]);

  useEffect(() => {
    if (!pairing) return;
    const interval = window.setInterval(() => {
      if (Date.now() >= Date.parse(pairing.expires_at)) {
        setPairing(null);
        setNotice("Pairing code expired. Add a device to create a new code.");
        return;
      }
      void load()
        .then((data) => {
          if (data.devices.some((device) => !previousIds.current.includes(device.device_id))) {
            setPairing(null);
            setNotice("Device paired. It will reconnect automatically.");
          } else if (!data.api_available) {
            setPairing(null);
          }
        })
        .catch((err) => setError(String(err.message || err)));
    }, 2000);
    return () => window.clearInterval(interval);
  }, [load, pairing]);

  const openHelp = () => {
    if (onOpenHelp) {
      onOpenHelp();
    } else {
      setHelpOpen(true);
    }
  };

  return (
    <Panel className="device-panel">
      {!standalone ? (
        <PanelHeader
          title="Devices"
          meta="Pair once. Reconnect automatically."
          actions={
            <div className="toolbar-actions">
              <Button type="button" variant="ghost" onClick={openHelp}>
                <BookOpen size={15} /> Connection Help
              </Button>
              <IconButton
                title="Refresh devices"
                disabled={busy}
                onClick={() =>
                  void act(async () => {
                    await load();
                  })
                }
              >
                <RefreshCw size={16} />
              </IconButton>
            </div>
          }
        />
      ) : null}
      <div className="device-panel-body">
        {error && <div className="error-strip">{error}</div>}
        {notice && <div className="success-strip">{notice}</div>}
        {!status ? (
          <p className="wizard-hint">Loading devices…</p>
        ) : (
          <>
            {!status.runtime_available && (
              <div className="device-readiness-banner">
                <p className="wizard-hint">
                  Start the Agent runtime to pair or remove devices. You can still edit saved details.
                </p>
                <Button
                  type="button"
                  variant="secondary"
                  disabled={busy}
                  onClick={() =>
                    void act(async () => {
                      await startRuntime();
                      await refreshAgents();
                      await load();
                      setNotice("Agent started.");
                    })
                  }
                >
                  <Play size={13} /> Start Agent
                </Button>
              </div>
            )}
            {status.runtime_available && !status.api_available && (
              <div className="device-readiness-banner">
                <p className="wizard-hint">Connect the API channel before adding a device.</p>
                <Button
                  type="button"
                  variant="secondary"
                  disabled={busy}
                  onClick={() =>
                    void act(async () => {
                      await connectChannel("api");
                      await load();
                      setNotice("API channel connected.");
                    })
                  }
                >
                  <Plug size={13} /> Connect API
                </Button>
              </div>
            )}
            <div className="device-add-row">
              <Button
                variant="primary"
                disabled={busy || !status.api_available}
                onClick={() =>
                  void act(async () => {
                    previousIds.current = status.devices.map((device) => device.device_id);
                    setPairing(await createDevicePairing());
                  })
                }
              >
                <Plus size={15} /> Add device
              </Button>
              <span className="wizard-hint">
                Each code pairs one new device (control, sensor, or voice) and expires in 5 minutes.
              </span>
            </div>
            {pairing && (
              <div className="device-pairing">
                <p>Enter this Agent address and pairing code on your device:</p>
                <label className="wizard-field">
                  <span>Agent address</span>
                  <input readOnly value={status.api_url} />
                </label>
                <label className="wizard-field">
                  <span>Pairing code</span>
                  <input readOnly value={pairing.pairing_code} />
                </label>
                <div className="toolbar-actions">
                  <Button
                    onClick={() =>
                      void act(async () => {
                        if (!(await copyText(pairing.pairing_code)))
                          throw new Error("Copy failed. Select the pairing code and copy it manually.");
                        setNotice("Pairing code copied.");
                      })
                    }
                  >
                    <Copy size={14} /> Copy code
                  </Button>
                  <Button
                    disabled={busy}
                    onClick={() =>
                      void act(async () => {
                        await cancelDevicePairing();
                        setPairing(null);
                      })
                    }
                  >
                    Cancel pairing
                  </Button>
                </div>
                <p className="wizard-hint">
                  For a device on another machine, use the Agent’s reachable address. Public connections use WSS.
                </p>
              </div>
            )}
            <div className="device-list">
              {!status.devices.length && <p className="wizard-hint">No paired devices yet.</p>}
              {status.devices.map((device) => (
                <div className="device-list-item" key={`${selectedAgent}:${device.device_id}`}>
                  <div className="device-list-row">
                    <div>
                      <strong>{device.name || device.device_id}</strong>
                      <p>
                        {device.device_id}
                        {device.location ? ` · ${device.location}` : ""}
                      </p>
                      {device.description && <p className="device-description">{device.description}</p>}
                      {device.capabilities?.length ? (
                        <p>{device.capabilities.map((cap) => cap.name).join(", ")}</p>
                      ) : null}
                    </div>
                    <div className="device-badges">
                      {device.voice_online ? (
                        <StatusBadge tone="info">
                          <Mic size={12} /> Voice
                        </StatusBadge>
                      ) : null}
                      <StatusBadge tone={device.online ? "good" : "muted"}>
                        {device.online ? "Online" : "Offline"}
                      </StatusBadge>
                    </div>
                    <Button
                      disabled={busy || editingId !== null}
                      onClick={() => {
                        setError("");
                        setNotice("");
                        setEditingId(device.device_id);
                      }}
                    >
                      Edit details
                    </Button>
                    <Button
                      variant="danger"
                      disabled={busy || editingId !== null || !status.runtime_available}
                      onClick={() =>
                        void act(async () => {
                          await removeDevice(device.device_id);
                          await load();
                          setNotice("Device removed. Pair again to reconnect.");
                        })
                      }
                    >
                      Remove
                    </Button>
                  </div>
                  {editingId === device.device_id && (
                    <DeviceDetailsForm
                      device={device}
                      onCancel={() => setEditingId(null)}
                      onSaved={async () => {
                        await load();
                        setEditingId(null);
                        setNotice("Device details saved.");
                      }}
                    />
                  )}
                </div>
              ))}
            </div>
            <section className="device-reusable-pairing" aria-labelledby={reusableTitleId}>
              <div className="device-reusable-header">
                <div className="device-reusable-copy">
                  <div className="device-reusable-title">
                    <h3 id={reusableTitleId}>Reusable pairing code</h3>
                    <StatusBadge tone={status.reusable_pairing_enabled ? "good" : "muted"}>
                      {status.reusable_pairing_enabled ? "Enabled" : "Disabled"}
                    </StatusBadge>
                  </div>
                  <p>
                    {typeof status.reusable_pairing_enabled === "boolean"
                      ? "Anyone with the code can add devices. Valid until reset or disabled."
                      : "Restart the Agent runtime to use reusable pairing."}
                  </p>
                </div>
                <div className="toolbar-actions">
                  <Button
                    disabled={busy || !status.api_available || typeof status.reusable_pairing_enabled !== "boolean"}
                    onClick={() =>
                      void act(async () => {
                        const result = await createReusableDevicePairing();
                        setReusableCode(result.pairing_code);
                        setStatus((current) => current ? { ...current, reusable_pairing_enabled: true } : current);
                      })
                    }
                  >
                    {status.reusable_pairing_enabled ? <RefreshCw size={14} /> : <Plus size={14} />}
                    {status.reusable_pairing_enabled ? "Reset code" : "Enable"}
                  </Button>
                  {status.reusable_pairing_enabled && (
                    <Button
                      variant="ghost"
                      disabled={busy || !status.runtime_available}
                      onClick={() =>
                        void act(async () => {
                          await disableReusableDevicePairing();
                          setReusableCode(null);
                          setStatus((current) => current ? { ...current, reusable_pairing_enabled: false } : current);
                          setNotice("Reusable pairing code disabled.");
                        })
                      }
                    >
                      Disable
                    </Button>
                  )}
                </div>
              </div>
              {reusableCode && (
                <div className="device-reusable-credentials">
                  <label className="wizard-field">
                    <span>Agent address</span>
                    <input readOnly value={status.api_url} />
                  </label>
                  <div className="wizard-field">
                    <label htmlFor={reusableCodeId}>Pairing code</label>
                    <div className="device-code-input">
                      <input id={reusableCodeId} readOnly value={reusableCode} spellCheck={false} />
                      <IconButton
                        title="Copy reusable pairing code"
                        disabled={busy}
                        onClick={() =>
                          void act(async () => {
                            if (!(await copyText(reusableCode)))
                              throw new Error("Copy failed. Select the pairing code and copy it manually.");
                            setNotice("Reusable pairing code copied.");
                          })
                        }
                      >
                        <Copy size={15} />
                      </IconButton>
                    </div>
                  </div>
                  <p>Save this code now. It is only shown when generated.</p>
                </div>
              )}
            </section>
          </>
        )}
      </div>
      {!onOpenHelp ? (
        <DeviceConnectionHelp
          open={helpOpen}
          currentAgent={selectedAgent}
          apiUrl={status?.api_url || agents.find((agent) => agent.name === selectedAgent)?.api_url || ""}
          onClose={() => setHelpOpen(false)}
        />
      ) : null}
    </Panel>
  );
}

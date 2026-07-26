import { RadioTower, RefreshCw, RotateCcw, Save } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { Button, IconButton, PageShell, PageToolbar, Panel, StatusBadge } from "../components/ui";
import { WizardField } from "../components/WizardField";
import { useAgentSession } from "../context/AgentSessionContext";
import { getSettings, restartRuntime, updateSettings } from "../lib/api";
import type { RoutePath, SettingsDocument, XAgentConfig } from "../types";

const providers: XAgentConfig["provider"]["name"][] = [
  "openai",
  "anthropic",
  "deepseek",
  "qwen",
  "minimax",
  "custom",
];

function cloneConfig(config: XAgentConfig): XAgentConfig {
  return structuredClone(config);
}

export function SettingsPage({ onNavigate }: { onNavigate: (route: RoutePath) => void }) {
  const { agents, selectedAgent, refresh: refreshAgents } = useAgentSession();
  const currentAgent = agents.find((agent) => agent.name === selectedAgent) || agents[0];
  const [document, setDocument] = useState<SettingsDocument | null>(null);
  const [config, setConfig] = useState<XAgentConfig | null>(null);
  const [rawText, setRawText] = useState("");
  const [rawDirty, setRawDirty] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState<"save" | "restart" | "">("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const acceptDocument = useCallback((next: SettingsDocument) => {
    setDocument(next);
    setConfig(next.settings);
    setRawText(JSON.stringify(next.settings, null, 2));
    setRawDirty(false);
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      acceptDocument(await getSettings());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
    }
  }, [acceptDocument]);

  useEffect(() => {
    setNotice("");
    void load();
  }, [load, selectedAgent]);

  const edit = (change: (draft: XAgentConfig) => void) => {
    if (!config) return;
    let base = config;
    if (rawDirty) {
      try {
        base = JSON.parse(rawText) as XAgentConfig;
      } catch {
        setError("Advanced JSON is invalid. Fix it or reset before editing other fields.");
        return;
      }
    }
    const next = cloneConfig(base);
    change(next);
    setConfig(next);
    setRawText(JSON.stringify(next, null, 2));
    setRawDirty(false);
    setError("");
    setNotice("");
  };

  const candidate = (): XAgentConfig | null => {
    if (!config) return null;
    if (!rawDirty) return config;
    try {
      return JSON.parse(rawText) as XAgentConfig;
    } catch {
      setError("Advanced JSON is not valid JSON.");
      return null;
    }
  };

  const save = async (restart: boolean) => {
    const next = candidate();
    if (!next) return;
    setSaving(restart ? "restart" : "save");
    setError("");
    setNotice("");
    try {
      const updated = await updateSettings(next);
      acceptDocument(updated);
      if (restart) {
        await restartRuntime();
        await refreshAgents();
        setNotice("Settings saved and Runtime restarted.");
      } else {
        setNotice(
          currentAgent?.runtime_running
            ? "Settings saved. Restart the Runtime to apply model and Runtime changes."
            : "Settings saved.",
        );
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setSaving("");
    }
  };

  const secretConfigured = Boolean(
    config && document && config.provider.api_key === document.secret_sentinel,
  );

  return (
    <PageShell>
      <PageToolbar
        eyebrow="Agent configuration"
        title="Settings"
        subtitle={`Inspect and edit ${currentAgent?.title || currentAgent?.name || "the selected Agent"}`}
        actions={
          <>
            <IconButton type="button" title="Reload settings" aria-label="Reload settings" onClick={() => void load()} disabled={loading || Boolean(saving)}>
              <RefreshCw size={15} />
            </IconButton>
            <Button type="button" onClick={() => void save(false)} disabled={!config || Boolean(saving)}>
              <Save size={14} />{saving === "save" ? "Saving…" : "Save"}
            </Button>
            <Button type="button" variant="primary" onClick={() => void save(true)} disabled={!config || Boolean(saving)}>
              <RotateCcw size={14} />{saving === "restart" ? "Restarting…" : "Save & restart"}
            </Button>
          </>
        }
      />

      <div className="page-body settings-body">
        {error ? <div className="error-strip">{error}</div> : null}
        {notice ? <div className="success-strip">{notice}</div> : null}
        {!config ? (
          <Panel className="settings-loading">{loading ? "Loading settings…" : "Settings are unavailable."}</Panel>
        ) : (
          <>
            <section className="settings-summary">
              <Panel>
                <span>Schema</span>
                <strong>v{config.schema_version}</strong>
              </Panel>
              <Panel>
                <span>Model</span>
                <strong>{config.provider.model}</strong>
              </Panel>
              <Panel>
                <span>Shell</span>
                <StatusBadge tone={config.tools.shell.enabled ? "good" : "muted"}>
                  {config.tools.shell.enabled ? "Built in" : "Disabled"}
                </StatusBadge>
              </Panel>
              <Panel>
                <span>Runtime</span>
                <StatusBadge tone={currentAgent?.runtime_running ? "good" : "muted"}>
                  {currentAgent?.runtime_running ? "Running" : "Stopped"}
                </StatusBadge>
              </Panel>
            </section>

            <div className="settings-grid">
              <Panel className="settings-card">
                <div className="section-heading">
                  <div><span className="section-kicker">Inference</span><h3>Model provider</h3></div>
                </div>
                <div className="settings-form-grid">
                  <WizardField label="Provider">
                    <select
                      value={config.provider.name}
                      onChange={(event) => edit((draft) => {
                        const name = event.target.value as XAgentConfig["provider"]["name"];
                        draft.provider.name = name;
                        if (name === "custom") {
                          draft.provider.model_api ||= "openai_chat_completions";
                        } else {
                          delete draft.provider.model_api;
                          delete draft.provider.supports_vision;
                        }
                      })}
                    >
                      {providers.map((provider) => <option key={provider} value={provider}>{provider}</option>)}
                    </select>
                  </WizardField>
                  <WizardField label="Model">
                    <input value={config.provider.model} onChange={(event) => edit((draft) => { draft.provider.model = event.target.value; })} />
                  </WizardField>
                  <WizardField label="Base URL" hint="Leave empty to use the provider default.">
                    <input value={config.provider.base_url || ""} placeholder="Provider default" onChange={(event) => edit((draft) => { draft.provider.base_url = event.target.value; })} />
                  </WizardField>
                  <WizardField label="API key" hint={secretConfigured ? "A key is configured. Leave empty to keep it." : "Stored only in the local config file."}>
                    <input
                      type="password"
                      autoComplete="new-password"
                      value={secretConfigured ? "" : config.provider.api_key || ""}
                      placeholder={secretConfigured ? "Configured — leave empty to keep" : "API key"}
                      onChange={(event) => edit((draft) => { draft.provider.api_key = event.target.value; })}
                    />
                  </WizardField>
                  <WizardField label="Maximum output tokens" hint="Empty uses the model/provider default.">
                    <input
                      type="number"
                      min="1"
                      value={config.provider.max_tokens || ""}
                      onChange={(event) => edit((draft) => {
                        if (event.target.value) draft.provider.max_tokens = Number(event.target.value);
                        else delete draft.provider.max_tokens;
                      })}
                    />
                  </WizardField>
                </div>
              </Panel>

              <Panel className="settings-card">
                <div className="section-heading">
                  <div><span className="section-kicker">Behavior</span><h3>Agent</h3></div>
                </div>
                <div className="settings-form-grid">
                  <WizardField label="History messages">
                    <input type="number" min="1" value={config.agent.max_history} onChange={(event) => edit((draft) => { draft.agent.max_history = Number(event.target.value); })} />
                  </WizardField>
                  <WizardField label="Maximum steps">
                    <input type="number" min="1" value={config.agent.max_iter} onChange={(event) => edit((draft) => { draft.agent.max_iter = Number(event.target.value); })} />
                  </WizardField>
                  <WizardField label="Recent memory days">
                    <input type="number" min="0" value={config.agent.memory_recent_days} onChange={(event) => edit((draft) => { draft.agent.memory_recent_days = Number(event.target.value); })} />
                  </WizardField>
                  <WizardField label="Subconscious activity" hint="0 keeps the subconscious disabled.">
                    <input type="number" min="0" max="1" step="0.05" value={config.agent.subconscious_activity} onChange={(event) => edit((draft) => { draft.agent.subconscious_activity = Number(event.target.value); })} />
                  </WizardField>
                </div>
              </Panel>

              <Panel className="settings-card">
                <div className="section-heading">
                  <div><span className="section-kicker">Runtime</span><h3>Execution limits</h3></div>
                </div>
                <div className="settings-form-grid">
                  <label className="settings-toggle">
                    <input type="checkbox" checked={config.runtime.heartbeat_enabled} onChange={(event) => edit((draft) => { draft.runtime.heartbeat_enabled = event.target.checked; })} />
                    <span><strong>Heartbeat</strong><small>Keep memory maintenance and scheduled runtime work active.</small></span>
                  </label>
                  <label className="settings-toggle">
                    <input type="checkbox" checked={config.tools.shell.enabled} onChange={(event) => edit((draft) => { draft.tools.shell.enabled = event.target.checked; })} />
                    <span><strong>Built-in Shell</strong><small>Read-only commands, constrained to the Agent workspace by code.</small></span>
                  </label>
                  <WizardField label="Heartbeat interval (seconds)">
                    <input type="number" min="1" value={config.runtime.heartbeat_interval_seconds} onChange={(event) => edit((draft) => { draft.runtime.heartbeat_interval_seconds = Number(event.target.value); })} />
                  </WizardField>
                  <WizardField label="Turn timeout (seconds)">
                    <input type="number" min="1" max="600" value={config.runtime.turn_timeout_seconds} onChange={(event) => edit((draft) => { draft.runtime.turn_timeout_seconds = Number(event.target.value); })} />
                  </WizardField>
                  <WizardField label="Tool timeout (seconds)">
                    <input type="number" min="1" max="300" value={config.runtime.tool_timeout_seconds} onChange={(event) => edit((draft) => { draft.runtime.tool_timeout_seconds = Number(event.target.value); })} />
                  </WizardField>
                </div>
              </Panel>

              <Panel className="settings-card settings-channels-card">
                <div className="section-heading">
                  <div><span className="section-kicker">Connections</span><h3>Channels</h3></div>
                </div>
                <p>Credentials, QR setup, live status, and hot start/stop controls live on the Channels page.</p>
                <Button type="button" onClick={() => onNavigate("/channels")}><RadioTower size={14} />Manage channels</Button>
              </Panel>
            </div>

            <details className="settings-advanced">
              <summary>Advanced JSON</summary>
              <div>
                <p>Complete schema v2 configuration. Secret values remain masked; unchanged masks preserve the stored secret.</p>
                <textarea
                  value={rawText}
                  spellCheck={false}
                  aria-label="Advanced JSON configuration"
                  onChange={(event) => {
                    setRawText(event.target.value);
                    setRawDirty(true);
                    setNotice("");
                  }}
                />
              </div>
            </details>
          </>
        )}
      </div>
    </PageShell>
  );
}

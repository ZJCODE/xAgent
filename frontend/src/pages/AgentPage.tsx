import { Save, RotateCcw, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { Button, EmptyState, IconButton, PageShell, PageToolbar, Panel, PanelHeader } from "../components/ui";
import { useAgentSession } from "../context/AgentSessionContext";
import {
  clearMemory,
  clearMessages,
  clearWorkspace,
  getAgentConfig,
  getAgentIdentity,
  getAgentInfo,
  updateAgentConfig,
  updateAgentIdentity,
} from "../lib/api";
import type { AgentConfig, AgentIdentity, AgentInfo } from "../types";

function stringValue(value: unknown): string {
  return typeof value === "string" && value.trim() ? value : "";
}

function RuntimeValue({ value, fallback = "Unavailable" }: { value?: unknown; fallback?: string }) {
  return <span className="data-chip path-chip">{stringValue(value) || fallback}</span>;
}

function MaintenanceRow({
  title,
  description,
  onClear,
}: {
  title: string;
  description: string;
  onClear: () => void;
}) {
  return (
    <div className="maintenance-row">
      <div>
        <h4>{title}</h4>
        <p>{description}</p>
      </div>
      <Button type="button" variant="danger" onClick={onClear}>
        <Trash2 size={14} />
        Clear
      </Button>
    </div>
  );
}

function FieldLike({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="wizard-field">
      <span>{label}</span>
      {children}
    </label>
  );
}

type MaintenanceAction = "memory" | "messages" | "workspace";

const MAINTENANCE_COPY: Record<
  MaintenanceAction,
  { title: string; description: string; confirmLabel: string; path?: (info: AgentInfo | null) => string | undefined }
> = {
  memory: {
    title: "Clear memory?",
    description: "This permanently removes all stored memory files for this agent.",
    confirmLabel: "Clear memory",
    path: (info) => info?.memory_dir,
  },
  messages: {
    title: "Clear messages?",
    description: "This permanently removes all message history for this agent and reloads runtime info.",
    confirmLabel: "Clear messages",
  },
  workspace: {
    title: "Clear workspace?",
    description: "This permanently removes all files from the local workspace.",
    confirmLabel: "Clear workspace",
    path: (info) => info?.workspace_dir,
  },
};

export function AgentPage() {
  const { agents, selectedAgent, deleteAgent } = useAgentSession();
  const currentAgent = agents.find((agent) => agent.name === selectedAgent);
  const [info, setInfo] = useState<AgentInfo | null>(null);
  const [identity, setIdentity] = useState<AgentIdentity | null>(null);
  const [editorValue, setEditorValue] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [activeTab, setActiveTab] = useState<"identity" | "config">("identity");
  const [configData, setConfigData] = useState<AgentConfig | null>(null);
  const [configEditorValue, setConfigEditorValue] = useState("");
  const [configSaving, setConfigSaving] = useState(false);
  const [configNotice, setConfigNotice] = useState("");
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleteConfirm, setDeleteConfirm] = useState("");
  const [deleteAcknowledged, setDeleteAcknowledged] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [maintenanceOpen, setMaintenanceOpen] = useState<MaintenanceAction | null>(null);
  const [maintenanceAcknowledged, setMaintenanceAcknowledged] = useState(false);
  const [maintenanceClearing, setMaintenanceClearing] = useState(false);

  const dirty = useMemo(() => editorValue !== (identity?.identity || ""), [editorValue, identity]);
  const dirtyConfig = useMemo(
    () => configEditorValue !== (configData?.config || ""),
    [configEditorValue, configData],
  );
  const editable = Boolean(info?.identity_editable);

  const load = async () => {
    setError("");
    try {
      const [agentInfo, identityData] = await Promise.all([
        getAgentInfo(),
        getAgentIdentity().catch(() => null),
      ]);
      setInfo(agentInfo);
      setIdentity(identityData);
      setEditorValue(identityData?.identity || agentInfo.identity || "");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const loadConfig = async () => {
    try {
      const data = await getAgentConfig();
      setConfigData(data);
      setConfigEditorValue(data.config);
      setConfigNotice("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  useEffect(() => {
    setConfigData(null);
    setConfigEditorValue("");
    setConfigNotice("");
    void load();
  }, [selectedAgent]);

  useEffect(() => {
    if (activeTab === "config") {
      void loadConfig();
    }
  }, [activeTab, selectedAgent]);

  const saveIdentity = async () => {
    const value = editorValue.trim();
    if (!value) {
      setError("Identity cannot be empty");
      return;
    }
    setSaving(true);
    setError("");
    try {
      const updated = await updateAgentIdentity(value);
      setIdentity(updated);
      setEditorValue(updated.identity);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  const saveConfig = async () => {
    const value = configEditorValue.trim();
    if (!value) {
      setError("Config cannot be empty");
      return;
    }
    setConfigSaving(true);
    setError("");
    setConfigNotice("");
    try {
      const updated = await updateAgentConfig(value);
      setConfigData(updated);
      setConfigEditorValue(updated.config);
      setConfigNotice("Config saved. Provider, model, search, and image generation changes require a restart to take effect.");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setConfigSaving(false);
    }
  };

  const runMaintenanceClear = async () => {
    if (!maintenanceOpen || !maintenanceAcknowledged) return;
    setMaintenanceClearing(true);
    setError("");
    try {
      if (maintenanceOpen === "memory") await clearMemory();
      if (maintenanceOpen === "messages") {
        await clearMessages();
        await load();
      }
      if (maintenanceOpen === "workspace") await clearWorkspace();
      setMaintenanceOpen(null);
      setMaintenanceAcknowledged(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setMaintenanceClearing(false);
    }
  };

  const runDeleteAgent = async () => {
    if (!selectedAgent || deleteConfirm !== selectedAgent || !deleteAcknowledged) return;
    setDeleting(true);
    setError("");
    try {
      await deleteAgent(selectedAgent, deleteConfirm);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setDeleting(false);
    }
  };

  if (!selectedAgent) {
    return (
      <PageShell className="agent-page">
        <PageToolbar title="Agent" subtitle="Runtime identity and local maintenance" />
        <EmptyState title="No agent selected">
          Create an agent from the sidebar to configure identity, config, and maintenance options.
        </EmptyState>
      </PageShell>
    );
  }

  return (
    <PageShell className="agent-page">
      <PageToolbar
        title="Agent"
        subtitle="Runtime identity and local maintenance"
      />
      {error ? <div className="error-strip">{error}</div> : null}

      <div className="agent-grid">
        <Panel className="info-panel">
          <PanelHeader title="Runtime" />
          <dl>
            <dt>Provider</dt>
            <dd className="chip-list">
              <RuntimeValue value={info?.provider} />
            </dd>
            <dt>Model</dt>
            <dd className="chip-list">
              <RuntimeValue value={info?.model} />
            </dd>
            <dt>Tools</dt>
            <dd className="chip-list">
              {info?.tools?.length ? info.tools.map((tool) => <span key={tool} className="data-chip">{tool}</span>) : "None"}
            </dd>
          </dl>
        </Panel>

        <Panel className="identity-panel">
          <div className="agent-tab-bar">
            <button
              type="button"
              className={`agent-tab ${activeTab === "identity" ? "active" : ""}`}
              onClick={() => {
                setActiveTab("identity");
                setError("");
                setConfigNotice("");
              }}
            >
              identity.md
            </button>
            <button
              type="button"
              className={`agent-tab ${activeTab === "config" ? "active" : ""}`}
              onClick={() => {
                setActiveTab("config");
                setError("");
              }}
            >
              config.yaml
            </button>
          </div>

          {activeTab === "identity" && (
            <>
              <PanelHeader
                title="identity.md"
                meta={identity?.path || info?.identity_file}
                actions={
                  <div className="toolbar-actions">
                    <Button type="button" disabled={!editable || !dirty || saving} onClick={saveIdentity}>
                      <Save size={15} />
                      Save
                    </Button>
                    <IconButton
                      type="button"
                      disabled={!dirty || saving}
                      onClick={() => setEditorValue(identity?.identity || "")}
                      title="Revert"
                    >
                      <RotateCcw size={16} />
                    </IconButton>
                  </div>
                }
              />
              <textarea
                className="identity-editor"
                value={editorValue}
                disabled={!editable || saving}
                onChange={(event) => setEditorValue(event.target.value)}
              />
            </>
          )}

          {activeTab === "config" && (
            <>
              <PanelHeader
                title="config.yaml"
                meta={configData?.path}
                actions={
                  <div className="toolbar-actions">
                    <Button type="button" disabled={!dirtyConfig || configSaving} onClick={saveConfig}>
                      <Save size={15} />
                      Save
                    </Button>
                    <IconButton
                      type="button"
                      disabled={!dirtyConfig || configSaving}
                      onClick={() => setConfigEditorValue(configData?.config || "")}
                      title="Revert"
                    >
                      <RotateCcw size={16} />
                    </IconButton>
                  </div>
                }
              />
              {configNotice && <div className="success-strip">{configNotice}</div>}
              <textarea
                className="identity-editor"
                value={configEditorValue}
                disabled={configSaving}
                onChange={(event) => setConfigEditorValue(event.target.value)}
              />
            </>
          )}
        </Panel>

        <Panel className="danger-panel">
          <PanelHeader title="Maintenance" />
          <div className="maintenance-list">
            <MaintenanceRow
              title="Memory"
              description="Remove stored memory files."
              onClear={() => setMaintenanceOpen("memory")}
            />
            <MaintenanceRow
              title="Messages"
              description="Remove message history and reload runtime info."
              onClear={() => setMaintenanceOpen("messages")}
            />
            <MaintenanceRow
              title="Workspace"
              description="Remove files from the local workspace."
              onClear={() => setMaintenanceOpen("workspace")}
            />
            <div className="maintenance-row">
              <div>
                <h4>Delete agent</h4>
                <p>
                  Permanently remove {selectedAgent || "this agent"} and all local data at{" "}
                  {currentAgent?.path || "its directory"}.
                </p>
              </div>
              <Button type="button" variant="danger" disabled={!selectedAgent} onClick={() => setDeleteOpen(true)}>
                <Trash2 size={14} />
                Delete
              </Button>
            </div>
          </div>
        </Panel>
      </div>

      {maintenanceOpen ? (
        <ConfirmDialog
          open
          title={MAINTENANCE_COPY[maintenanceOpen].title}
          description={
            <>
              <p>{MAINTENANCE_COPY[maintenanceOpen].description}</p>
              {MAINTENANCE_COPY[maintenanceOpen].path?.(info) ? (
                <code className="confirm-path">{MAINTENANCE_COPY[maintenanceOpen].path?.(info)}</code>
              ) : null}
            </>
          }
          confirmLabel={maintenanceClearing ? "Clearing..." : MAINTENANCE_COPY[maintenanceOpen].confirmLabel}
          confirmDisabled={maintenanceClearing || !maintenanceAcknowledged}
          onCancel={() => {
            if (maintenanceClearing) return;
            setMaintenanceOpen(null);
            setMaintenanceAcknowledged(false);
          }}
          onConfirm={() => void runMaintenanceClear()}
        >
          <label className="wizard-checkbox">
            <input
              type="checkbox"
              checked={maintenanceAcknowledged}
              onChange={(event) => setMaintenanceAcknowledged(event.target.checked)}
            />
            <span>I understand this cannot be undone</span>
          </label>
        </ConfirmDialog>
      ) : null}

      <ConfirmDialog
        open={deleteOpen}
        title={`Delete agent "${selectedAgent}"?`}
        description={
          <>
            {currentAgent?.channel_running ? (
              <div className="warning-strip">The API channel is running. It will be stopped before deletion.</div>
            ) : null}
            <p>
              This permanently removes config, identity, memory, messages, workspace, skills, tasks, and run state at:
            </p>
            <code className="confirm-path">{currentAgent?.path}</code>
          </>
        }
        confirmLabel={deleting ? "Deleting..." : "Delete agent"}
        confirmDisabled={
          deleting || deleteConfirm !== selectedAgent || !deleteAcknowledged || !selectedAgent
        }
        onCancel={() => {
          if (deleting) return;
          setDeleteOpen(false);
          setDeleteConfirm("");
          setDeleteAcknowledged(false);
        }}
        onConfirm={() => void runDeleteAgent()}
      >
        <label className="wizard-checkbox">
          <input
            type="checkbox"
            checked={deleteAcknowledged}
            onChange={(event) => setDeleteAcknowledged(event.target.checked)}
          />
          <span>I understand this cannot be undone</span>
        </label>
        <FieldLike label="Type agent name to confirm">
          <input
            value={deleteConfirm}
            placeholder={selectedAgent}
            onChange={(event) => setDeleteConfirm(event.target.value)}
          />
        </FieldLike>
      </ConfirmDialog>
    </PageShell>
  );
}

import { RefreshCw, Save, RotateCcw, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Button, IconButton, PageShell, PageToolbar, Panel, PanelHeader } from "../components/ui";
import {
  clearMemory,
  clearMessages,
  clearWorkspace,
  getAgentIdentity,
  getAgentInfo,
  updateAgentIdentity,
} from "../lib/api";
import type { AgentIdentity, AgentInfo } from "../types";

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

export function AgentPage() {
  const [info, setInfo] = useState<AgentInfo | null>(null);
  const [identity, setIdentity] = useState<AgentIdentity | null>(null);
  const [editorValue, setEditorValue] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const dirty = useMemo(() => editorValue !== (identity?.identity || ""), [editorValue, identity]);
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

  useEffect(() => {
    void load();
  }, []);

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

  const runClearMemory = async () => {
    if (!window.confirm("Clear all memory files?")) return;
    await clearMemory();
  };

  const runClearMessages = async () => {
    if (!window.confirm("Clear all messages?")) return;
    await clearMessages();
    await load();
  };

  const runClearWorkspace = async () => {
    if (!window.confirm("Clear all workspace files?")) return;
    await clearWorkspace();
  };

  return (
    <PageShell className="agent-page">
      <PageToolbar
        title="Agent"
        subtitle="Runtime identity and local maintenance"
        actions={
          <Button type="button" onClick={load}>
            <RefreshCw size={15} />
            Refresh
          </Button>
        }
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
        </Panel>

        <Panel className="danger-panel">
          <PanelHeader title="Maintenance" />
          <div className="maintenance-list">
            <MaintenanceRow
              title="Memory"
              description="Remove stored memory files."
              onClear={runClearMemory}
            />
            <MaintenanceRow
              title="Messages"
              description="Remove message history and reload runtime info."
              onClear={runClearMessages}
            />
            <MaintenanceRow
              title="Workspace"
              description="Remove files from the local workspace."
              onClear={runClearWorkspace}
            />
          </div>
        </Panel>
      </div>
    </PageShell>
  );
}

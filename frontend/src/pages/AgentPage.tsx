import { RefreshCw, Save, RotateCcw, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import {
  clearMemory,
  clearMessages,
  getAgentIdentity,
  getAgentInfo,
  getMessagesStats,
  updateAgentIdentity,
} from "../lib/api";
import { formatTimestamp } from "../lib/format";
import type { AgentIdentity, AgentInfo, MessagesStats } from "../types";

export function AgentPage() {
  const [info, setInfo] = useState<AgentInfo | null>(null);
  const [identity, setIdentity] = useState<AgentIdentity | null>(null);
  const [stats, setStats] = useState<MessagesStats | null>(null);
  const [editorValue, setEditorValue] = useState("");
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");

  const dirty = useMemo(() => editorValue !== (identity?.identity || ""), [editorValue, identity]);
  const editable = Boolean(info?.identity_editable);

  const load = async () => {
    setError("");
    try {
      const [agentInfo, identityData, messageStats] = await Promise.all([
        getAgentInfo(),
        getAgentIdentity().catch(() => null),
        getMessagesStats().catch(() => null),
      ]);
      setInfo(agentInfo);
      setIdentity(identityData);
      setStats(messageStats);
      setEditorValue(identityData?.identity || agentInfo.identity || "");
      setStatus("Loaded");
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
      setStatus("Saved");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  const runClearMemory = async () => {
    if (!window.confirm("Clear all memory files?")) return;
    await clearMemory();
    setStatus("Memory cleared");
  };

  const runClearMessages = async () => {
    if (!window.confirm("Clear all messages?")) return;
    await clearMessages();
    setStatus("Messages cleared");
    await load();
  };

  return (
    <div className="console-page agent-page">
      <section className="console-toolbar">
        <div>
          <h2>Agent</h2>
          <p>{info?.model || "Runtime configuration"}</p>
        </div>
        <button type="button" className="ghost-button icon-text-button" onClick={load}>
          <RefreshCw size={15} />
          Refresh
        </button>
      </section>
      {error ? <div className="error-strip">{error}</div> : null}
      {status ? <div className="success-strip">{status}</div> : null}

      <div className="agent-grid">
        <section className="info-panel">
          <h3>Runtime</h3>
          <dl>
            <dt>Workspace</dt>
            <dd>{info?.workspace_dir}</dd>
            <dt>Memory</dt>
            <dd>{info?.memory_dir}</dd>
            <dt>Identity</dt>
            <dd>{info?.identity_path || "Unavailable"}</dd>
            <dt>Messages</dt>
            <dd>
              {stats?.total ?? 0} total
              {stats?.latest_timestamp ? `, latest ${formatTimestamp(stats.latest_timestamp)}` : ""}
            </dd>
            <dt>Tools</dt>
            <dd>{info?.tools?.join(", ") || "None"}</dd>
          </dl>
        </section>

        <section className="identity-panel">
          <div className="content-heading">
            <div>
              <h3>identity.md</h3>
              <span>{identity?.path || info?.identity_file}</span>
            </div>
            <div className="toolbar-actions">
              <button type="button" className="ghost-button icon-text-button" disabled={!editable || !dirty || saving} onClick={saveIdentity}>
                <Save size={15} />
                Save
              </button>
              <button
                type="button"
                className="ghost-button icon-button"
                disabled={!dirty || saving}
                onClick={() => setEditorValue(identity?.identity || "")}
                title="Revert"
              >
                <RotateCcw size={16} />
              </button>
            </div>
          </div>
          <textarea
            className="identity-editor"
            value={editorValue}
            disabled={!editable || saving}
            onChange={(event) => setEditorValue(event.target.value)}
          />
        </section>

        <section className="danger-panel">
          <h3>Maintenance</h3>
          <div className="flex flex-wrap gap-2">
            <button type="button" className="danger-button" onClick={runClearMemory}>
              <Trash2 size={15} />
              Clear Memory
            </button>
            <button type="button" className="danger-button" onClick={runClearMessages}>
              <Trash2 size={15} />
              Clear Messages
            </button>
          </div>
        </section>
      </div>
    </div>
  );
}

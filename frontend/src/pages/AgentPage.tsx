import { RefreshCw, Save, RotateCcw, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import {
  clearMemory,
  clearMessages,
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

  return (
    <div className="console-page agent-page">
      <section className="console-toolbar">
        <div>
          <h2>Agent</h2>
        </div>
        <button type="button" className="ghost-button icon-text-button" onClick={load}>
          <RefreshCw size={15} />
          Refresh
        </button>
      </section>
      {error ? <div className="error-strip">{error}</div> : null}

      <div className="agent-grid">
        <section className="info-panel">
          <h3>Runtime</h3>
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

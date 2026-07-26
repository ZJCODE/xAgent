import { Brain, CalendarDays, FileText, RefreshCw, Search, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { Markdown } from "../components/Markdown";
import {
  EmptyState,
  IconButton,
  PageShell,
  PageToolbar,
} from "../components/ui";
import { useAgentSession } from "../context/AgentSessionContext";
import { getMemory, getMemoryFile } from "../lib/api";
import { formatTimestamp } from "../lib/format";
import type { MemoryEntry, MemoryFile } from "../types";

const scopes = ["all", "daily", "weekly", "monthly", "yearly"] as const;

export function MemoryPage() {
  const { selectedAgent } = useAgentSession();
  const [entries, setEntries] = useState<MemoryEntry[]>([]);
  const [selected, setSelected] = useState<MemoryFile | null>(null);
  const [query, setQuery] = useState("");
  const [appliedQuery, setAppliedQuery] = useState("");
  const [scope, setScope] = useState<(typeof scopes)[number]>("all");
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState("");
  const selectedPath = useRef("");

  const openFile = useCallback(async (entry: MemoryEntry) => {
    setDetailLoading(true);
    setError("");
    try {
      const file = await getMemoryFile(entry.path);
      selectedPath.current = file.path;
      setSelected(file);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setDetailLoading(false);
    }
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data = await getMemory({ scope, query: appliedQuery });
      setEntries(data.entries);
      setTotal(data.total);
      if (!data.entries.some((entry) => entry.path === selectedPath.current)) {
        if (data.entries[0]) await openFile(data.entries[0]);
        else {
          selectedPath.current = "";
          setSelected(null);
        }
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
      setEntries([]);
      selectedPath.current = "";
      setSelected(null);
    } finally {
      setLoading(false);
    }
  }, [appliedQuery, openFile, scope]);

  useEffect(() => {
    void load();
  }, [load, selectedAgent]);

  const submitSearch = (event?: FormEvent) => {
    event?.preventDefault();
    setAppliedQuery(query.trim());
  };

  return (
    <PageShell className="memory-page">
      <PageToolbar
        eyebrow="Long-term memory"
        title="Memory"
        subtitle={`${total.toLocaleString()} Markdown diary files · read only`}
        actions={
          <IconButton type="button" aria-label="Refresh memory" title="Refresh memory" onClick={() => void load()}>
            <RefreshCw size={16} />
          </IconButton>
        }
      />

      <div className="memory-workbench">
        <aside className="memory-browser">
          <form className="memory-search" onSubmit={submitSearch}>
            <Search size={15} />
            <input
              value={query}
              aria-label="Search memory"
              placeholder="Search memory"
              onChange={(event) => setQuery(event.target.value)}
            />
            {query ? (
              <IconButton
                type="button"
                aria-label="Clear memory search"
                title="Clear memory search"
                onClick={() => {
                  setQuery("");
                  setAppliedQuery("");
                }}
              >
                <X size={14} />
              </IconButton>
            ) : null}
          </form>
          <div className="scope-tabs" aria-label="Memory scope">
            {scopes.map((item) => (
              <button
                type="button"
                key={item}
                className={scope === item ? "active" : ""}
                aria-pressed={scope === item}
                onClick={() => setScope(item)}
              >
                {item}
              </button>
            ))}
          </div>
          <div className="memory-file-list">
            {entries.map((entry) => (
              <button
                type="button"
                className={selected?.path === entry.path ? "memory-file active" : "memory-file"}
                key={entry.path}
                onClick={() => void openFile(entry)}
              >
                <span className="memory-file-icon">
                  {entry.scope === "daily" ? <CalendarDays size={16} /> : <FileText size={16} />}
                </span>
                <span>
                  <strong>{entry.title}</strong>
                  <small>{entry.excerpt || entry.path}</small>
                  <em>{entry.scope} · {formatTimestamp(entry.modified_at)}</em>
                </span>
              </button>
            ))}
            {!entries.length ? (
              <EmptyState icon={<Brain size={20} />} title={loading ? "Loading memory…" : appliedQuery ? "No matching memory" : "No memory yet"} />
            ) : null}
          </div>
        </aside>

        <main className="memory-reader">
          {error ? <div className="error-strip">{error}</div> : null}
          {selected ? (
            <>
              <header className="memory-reader-header">
                <div>
                  <span>{selected.path}</span>
                  <h2>{selected.title}</h2>
                </div>
                <time>Updated {formatTimestamp(selected.modified_at)}</time>
              </header>
              <article className="memory-document">
                {detailLoading ? <p className="muted-copy">Loading memory…</p> : <Markdown content={selected.content} />}
              </article>
            </>
          ) : (
            <EmptyState icon={<Brain size={24} />} title={loading ? "Loading memory…" : "Select a memory"}>
              Daily entries and their weekly, monthly, and yearly summaries live here.
            </EmptyState>
          )}
        </main>
      </div>
    </PageShell>
  );
}

import { RefreshCw, Search, X } from "lucide-react";
import { useEffect, useState } from "react";
import { FileTree } from "../components/FileTree";
import { Markdown } from "../components/Markdown";
import { getAgentInfo, getMemoryTree, readMemoryFile, searchMemory } from "../lib/api";
import { formatTimestamp } from "../lib/format";
import type { AgentInfo, FileNode, FileReadResult, SearchResult } from "../types";

export function MemoryPage() {
  const [info, setInfo] = useState<AgentInfo | null>(null);
  const [tree, setTree] = useState<FileNode[]>([]);
  const [selected, setSelected] = useState<FileReadResult | null>(null);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResult[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = async () => {
    setLoading(true);
    setError("");
    try {
      const [agentInfo, memoryTree] = await Promise.all([getAgentInfo(), getMemoryTree()]);
      setInfo(agentInfo);
      setTree(memoryTree.tree || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, []);

  const selectFile = async (node: FileNode) => {
    setError("");
    try {
      setSelected(await readMemoryFile(node.path));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const runSearch = async () => {
    const text = query.trim();
    if (!text) return;
    setError("");
    try {
      const data = await searchMemory(text);
      setResults(data.results || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <div className="console-page">
      <section className="console-toolbar">
        <div className="min-w-0">
          <h2>Memory</h2>
          <p>{info?.memory_dir || "Time-scoped markdown memory"}</p>
        </div>
        <div className="toolbar-actions">
          <input
            className="search-input"
            placeholder="Search memory"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") void runSearch();
            }}
          />
          <button type="button" className="ghost-button icon-text-button" onClick={runSearch}>
            <Search size={15} />
            Search
          </button>
          <button
            type="button"
            className="ghost-button icon-button"
            onClick={() => {
              setQuery("");
              setResults([]);
            }}
            title="Clear search"
          >
            <X size={16} />
          </button>
          <button type="button" className="ghost-button icon-button" onClick={load} title="Refresh">
            <RefreshCw size={16} />
          </button>
        </div>
      </section>

      {error ? <div className="error-strip">{error}</div> : null}
      <div className="browser-layout">
        <aside className="browser-sidebar">
          {results.length ? (
            <div className="space-y-2">
              {results.map((item) => (
                <button key={item.path} type="button" className="search-result" onClick={() => void selectFile(item)}>
                  <strong>{item.path}</strong>
                  {item.snippet ? <span>{item.snippet}</span> : null}
                </button>
              ))}
            </div>
          ) : loading ? (
            <div className="empty-state">Loading...</div>
          ) : (
            <FileTree nodes={tree} selectedPath={selected?.path} onSelect={selectFile} />
          )}
        </aside>
        <main className="browser-content">
          {selected ? (
            <>
              <div className="content-heading">
                <h3>{selected.name || selected.path}</h3>
                <span>{formatTimestamp(selected.modified)}</span>
              </div>
              <Markdown content={selected.content} />
            </>
          ) : (
            <div className="empty-state h-full">Select a memory file</div>
          )}
        </main>
      </div>
    </div>
  );
}

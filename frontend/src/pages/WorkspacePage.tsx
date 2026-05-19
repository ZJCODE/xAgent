import { RefreshCw, Search, X } from "lucide-react";
import { useEffect, useState } from "react";
import { FileTree } from "../components/FileTree";
import { Markdown } from "../components/Markdown";
import { getAgentInfo, getWorkspaceTree, readWorkspaceFile, searchWorkspace, workspaceBlobUrl } from "../lib/api";
import { formatBytes, formatTimestamp } from "../lib/format";
import type { AgentInfo, FileNode, FileReadResult, SearchResult } from "../types";

export function WorkspacePage() {
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
      const [agentInfo, workspaceTree] = await Promise.all([getAgentInfo(), getWorkspaceTree()]);
      setInfo(agentInfo);
      setTree(workspaceTree.tree || []);
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
      setSelected(await readWorkspaceFile(node.path));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const runSearch = async () => {
    const text = query.trim();
    if (!text) return;
    setError("");
    try {
      const data = await searchWorkspace(text);
      setResults(data.results || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const isImage = selected?.mime_type?.startsWith("image/");

  return (
    <div className="console-page">
      <section className="console-toolbar">
        <div className="min-w-0">
          <h2>Workspace</h2>
          <p>{info?.workspace_dir || "Agent workspace files"}</p>
        </div>
        <div className="toolbar-actions">
          <input
            className="search-input"
            placeholder="Search workspace"
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
                <div>
                  <h3>{selected.name || selected.path}</h3>
                  <span>
                    {formatBytes(selected.size)} {formatTimestamp(selected.modified)}
                  </span>
                </div>
                <span>{selected.mime_type}</span>
              </div>
              {selected.text && selected.path.endsWith(".md") ? (
                <Markdown content={selected.content} />
              ) : selected.text ? (
                <pre className="file-pre">{selected.content}</pre>
              ) : isImage ? (
                <img className="workspace-preview-image" src={workspaceBlobUrl(selected.path)} alt={selected.name} />
              ) : (
                <div className="empty-state">Binary file preview is unavailable</div>
              )}
            </>
          ) : (
            <div className="empty-state h-full">Select a workspace file</div>
          )}
        </main>
      </div>
    </div>
  );
}

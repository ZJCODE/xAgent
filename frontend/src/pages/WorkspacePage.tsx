import { RefreshCw, Search, X } from "lucide-react";
import { useEffect, useState } from "react";
import { FileTree } from "../components/FileTree";
import { Markdown } from "../components/Markdown";
import { BrowserLayout, Button, EmptyState, IconButton, PageShell, PageToolbar, SearchField } from "../components/ui";
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
    <PageShell>
      <PageToolbar
        title="Workspace"
        subtitle={info?.workspace_dir || "Agent workspace files"}
        actions={
          <>
            <SearchField
              placeholder="Search workspace"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              onSubmit={() => void runSearch()}
            />
            <Button type="button" onClick={runSearch}>
              <Search size={15} />
              Search
            </Button>
            <IconButton
              type="button"
              onClick={() => {
                setQuery("");
                setResults([]);
              }}
              title="Clear search"
            >
              <X size={16} />
            </IconButton>
            <IconButton type="button" onClick={load} title="Refresh">
              <RefreshCw size={16} />
            </IconButton>
          </>
        }
      />

      {error ? <div className="error-strip">{error}</div> : null}
      <BrowserLayout
        sidebar={
          results.length ? (
            <div className="space-y-2">
              {results.map((item) => (
                <button key={item.path} type="button" className="search-result" onClick={() => void selectFile(item)}>
                  <strong>{item.path}</strong>
                  {item.snippet ? <span>{item.snippet}</span> : null}
                </button>
              ))}
            </div>
          ) : loading ? (
            <EmptyState title="Loading..." />
          ) : (
            <FileTree nodes={tree} selectedPath={selected?.path} onSelect={selectFile} />
          )
        }
      >
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
                <EmptyState title="Binary file preview is unavailable" />
              )}
            </>
          ) : (
            <EmptyState title="Select a workspace file" className="h-full" />
          )}
      </BrowserLayout>
    </PageShell>
  );
}

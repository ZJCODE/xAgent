import { RefreshCw, Search, X } from "lucide-react";
import { useEffect, useState } from "react";
import { FileTree } from "../components/FileTree";
import { Markdown } from "../components/Markdown";
import { BrowserLayout, Button, EmptyState, IconButton, PageShell, PageToolbar, SearchField } from "../components/ui";
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
    <PageShell>
      <PageToolbar
        title="Memory"
        subtitle={info?.memory_dir || "Time-scoped markdown memory"}
        actions={
          <>
            <SearchField
              placeholder="Search memory"
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
                <h3>{selected.name || selected.path}</h3>
                <span>{formatTimestamp(selected.modified)}</span>
              </div>
              <Markdown content={selected.content} />
            </>
          ) : (
            <EmptyState title="Select a memory file" className="h-full" />
          )}
      </BrowserLayout>
    </PageShell>
  );
}

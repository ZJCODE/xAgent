import { RefreshCw, Search, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { FileTree } from "../components/FileTree";
import { Markdown } from "../components/Markdown";
import { BrowserLayout, Button, EmptyState, IconButton, PageShell, PageToolbar, SearchField } from "../components/ui";
import { getAgentInfo, getMemoryTree, readMemoryFile, searchMemory } from "../lib/api";
import { formatTimestamp } from "../lib/format";
import type { AgentInfo, FileNode, FileReadResult, SearchResult } from "../types";

const TIME_SCOPES = new Set(["daily", "weekly", "monthly", "yearly"]);
const NOTES_SCOPE = "notes";
const WIKI_LINK_PATTERN = /\[\[([^[\]]+)\]\]/g;

function slugifyNote(value: string): string {
  return value
    .trim()
    .toLowerCase()
    .replace(/_/g, "-")
    .replace(/[^\p{L}\p{N}.-]+/gu, "-")
    .replace(/-{2,}/g, "-")
    .replace(/^[-._]+|[-._]+$/g, "")
    .slice(0, 64);
}

function parseWikiLinks(content: string, ownSlug = ""): string[] {
  const seen = new Set<string>();
  const links: string[] = [];
  for (const match of content.matchAll(WIKI_LINK_PATTERN)) {
    const slug = slugifyNote((match[1].split("|", 1)[0] || "").trim());
    if (!slug || slug === ownSlug || seen.has(slug)) continue;
    seen.add(slug);
    links.push(slug);
  }
  return links;
}

function isActiveNotePath(path: string): boolean {
  const parts = path.split("/").filter(Boolean);
  return parts[0] === NOTES_SCOPE && !parts.includes("archive") && path.endsWith(".md");
}

export function MemoryPage() {
  const [info, setInfo] = useState<AgentInfo | null>(null);
  const [tree, setTree] = useState<FileNode[]>([]);
  const [selected, setSelected] = useState<FileReadResult | null>(null);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResult[]>([]);
  const [searchActive, setSearchActive] = useState(false);
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
    setSearchActive(true);
    try {
      const data = await searchMemory(text);
      setResults(data.results || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const { timeNodes, relNodes, noteNodes } = useMemo(() => {
    const time: FileNode[] = [];
    const rel: FileNode[] = [];
    const notes: FileNode[] = [];
    for (const node of tree) {
      if (TIME_SCOPES.has(node.name)) {
        time.push(node);
      } else if (node.name === NOTES_SCOPE) {
        notes.push(node);
      } else {
        rel.push(node);
      }
    }
    return { timeNodes: time, relNodes: rel, noteNodes: notes };
  }, [tree]);

  const noteSlug = selected && isActiveNotePath(selected.path)
    ? selected.path.replace(/^notes\//, "").replace(/\.md$/, "")
    : "";
  const noteLinks = selected && noteSlug
    ? (selected.links ?? parseWikiLinks(selected.content, noteSlug))
    : [];
  const noteBacklinks = selected && noteSlug ? (selected.backlinks ?? []) : [];

  const openNote = (slug: string) => {
    void selectFile({ name: `${slug}.md`, path: `notes/${slug}.md`, type: "file" });
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
                setSearchActive(false);
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
          searchActive ? (
            results.length ? (
              <div className="space-y-2">
                {results.map((item) => (
                  <button key={item.path} type="button" className="search-result" onClick={() => void selectFile(item)}>
                    <strong>{item.path}</strong>
                    {item.snippet ? <span>{item.snippet}</span> : null}
                  </button>
                ))}
              </div>
            ) : (
              <EmptyState title="No matching files" />
            )
          ) : loading ? (
            <EmptyState title="Loading..." />
          ) : (
            <div className="space-y-3">
              {timeNodes.length > 0 && (
                <div>
                  <div className="text-[10px] font-semibold uppercase tracking-wider text-zinc-400 mb-1 px-1">Time</div>
                  <FileTree nodes={timeNodes} selectedPath={selected?.path} onSelect={selectFile} />
                </div>
              )}
              {relNodes.length > 0 && (
                <div>
                  <div className="text-[10px] font-semibold uppercase tracking-wider text-zinc-400 mb-1 px-1">Relationships</div>
                  <FileTree nodes={relNodes} selectedPath={selected?.path} onSelect={selectFile} />
                </div>
              )}
              {noteNodes.length > 0 && (
                <div>
                  <div className="text-[10px] font-semibold uppercase tracking-wider text-zinc-400 mb-1 px-1">Notes</div>
                  <FileTree nodes={noteNodes} selectedPath={selected?.path} onSelect={selectFile} />
                </div>
              )}
            </div>
          )
        }
      >
          {selected ? (
            <>
              <div className="content-heading">
                <h3>{selected.name || selected.path}</h3>
                <span>{formatTimestamp(selected.modified)}</span>
              </div>
              {noteSlug && (noteLinks.length > 0 || noteBacklinks.length > 0) ? (
                <div className="note-wiki-links">
                  {noteLinks.length > 0 ? (
                    <div className="note-wiki-links-group">
                      <span className="note-wiki-links-label">Links</span>
                      {noteLinks.map((slug) => (
                        <button
                          key={`link-${slug}`}
                          type="button"
                          className="note-wiki-link"
                          onClick={() => openNote(slug)}
                        >
                          [[{slug}]]
                        </button>
                      ))}
                    </div>
                  ) : null}
                  {noteBacklinks.length > 0 ? (
                    <div className="note-wiki-links-group">
                      <span className="note-wiki-links-label">Backlinks</span>
                      {noteBacklinks.map((slug) => (
                        <button
                          key={`back-${slug}`}
                          type="button"
                          className="note-wiki-link"
                          onClick={() => openNote(slug)}
                        >
                          [[{slug}]]
                        </button>
                      ))}
                    </div>
                  ) : null}
                </div>
              ) : null}
              <Markdown content={selected.content} />
            </>
          ) : (
            <EmptyState title="Select a memory file" className="h-full" />
          )}
      </BrowserLayout>
    </PageShell>
  );
}

import { Download, Edit3, Eye, RefreshCw, RotateCcw, Save, Search, Trash2, Upload, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { FileTree } from "../components/FileTree";
import { Markdown } from "../components/Markdown";
import { TextEditor } from "../components/TextEditor";
import { BrowserLayout, Button, EmptyState, IconButton, PageShell, PageToolbar, SearchField } from "../components/ui";
import { useUnsavedChanges } from "../context/UnsavedChangesContext";
import {
  deleteWorkspaceFile,
  getAgentInfo,
  getWorkspaceTree,
  readWorkspaceFile,
  searchWorkspace,
  uploadWorkspaceFile,
  workspaceBlobUrl,
  writeWorkspaceFile,
} from "../lib/api";
import { formatBytes, formatTimestamp } from "../lib/format";
import type { AgentInfo, FileNode, FileReadResult, SearchResult } from "../types";

export function WorkspacePage() {
  const { setDirty } = useUnsavedChanges();
  const uploadInputRef = useRef<HTMLInputElement>(null);
  const [info, setInfo] = useState<AgentInfo | null>(null);
  const [tree, setTree] = useState<FileNode[]>([]);
  const [selected, setSelected] = useState<FileReadResult | null>(null);
  const [editorValue, setEditorValue] = useState("");
  const [viewMode, setViewMode] = useState<"preview" | "edit">("preview");
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResult[]>([]);
  const [searchActive, setSearchActive] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<FileNode | null>(null);

  const dirty = Boolean(selected?.text && editorValue !== selected.content);

  useEffect(() => {
    setDirty(dirty);
    return () => setDirty(false);
  }, [dirty, setDirty]);

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

  const guardLocalDiscard = () => {
    if (!dirty) return true;
    if (!window.confirm("Discard unsaved changes?")) return false;
    setDirty(false);
    setEditorValue(selected?.content || "");
    setViewMode("preview");
    return true;
  };

  const selectFile = async (node: FileNode) => {
    if (node.type === "dir") return;
    if (!guardLocalDiscard()) return;
    setError("");
    setNotice("");
    try {
      const file = await readWorkspaceFile(node.path);
      setSelected(file);
      setEditorValue(file.content || "");
      setViewMode(file.text ? "preview" : "preview");
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
      const data = await searchWorkspace(text);
      setResults(data.results || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const saveEditor = async () => {
    if (!selected?.text || !dirty || saving) return;
    setSaving(true);
    setError("");
    setNotice("");
    try {
      await writeWorkspaceFile(selected.path, editorValue);
      const file = await readWorkspaceFile(selected.path);
      setSelected(file);
      setEditorValue(file.content || "");
      setNotice(`Saved ${file.path}`);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  const discardEditor = () => {
    if (!selected) return;
    setEditorValue(selected.content || "");
    setViewMode("preview");
  };

  const uploadTargetDir = useMemo(() => {
    if (!selected?.path) return "";
    if (selected.type === "dir") return `${selected.path}/`;
    const parts = selected.path.split("/");
    if (parts.length <= 1) return "";
    return `${parts.slice(0, -1).join("/")}/`;
  }, [selected]);

  const handleUpload = async (fileList: FileList | null) => {
    const file = fileList?.[0];
    if (!file) return;
    if (!guardLocalDiscard()) return;
    setUploading(true);
    setError("");
    setNotice("");
    try {
      const uploaded = await uploadWorkspaceFile(file, uploadTargetDir || undefined);
      setNotice(`Uploaded ${uploaded.path}`);
      await load();
      if (uploaded.type === "file") {
        await selectFile(uploaded);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setUploading(false);
      if (uploadInputRef.current) uploadInputRef.current.value = "";
    }
  };

  const confirmDelete = async () => {
    if (!deleteTarget || deleting) return;
    setDeleting(true);
    setError("");
    setNotice("");
    try {
      const recursive = deleteTarget.type === "dir";
      await deleteWorkspaceFile(deleteTarget.path, recursive);
      if (selected && (selected.path === deleteTarget.path || selected.path.startsWith(`${deleteTarget.path}/`))) {
        setSelected(null);
        setEditorValue("");
        setDirty(false);
      }
      setNotice(`Deleted ${deleteTarget.path}`);
      setDeleteTarget(null);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setDeleting(false);
    }
  };

  const renderTreeActions = (node: FileNode) => (
    <IconButton
      type="button"
      variant="danger"
      title={`Delete ${node.path}`}
      aria-label={`Delete ${node.path}`}
      onClick={(event) => {
        event.stopPropagation();
        if (!guardLocalDiscard()) return;
        setDeleteTarget(node);
      }}
    >
      <Trash2 size={12} />
    </IconButton>
  );

  const isImage = selected?.mime_type?.startsWith("image/");

  return (
    <PageShell>
      <PageToolbar
        title="Workspace"
        subtitle={info?.workspace_dir || "Agent workspace files"}
        actions={
          <>
            <input
              ref={uploadInputRef}
              type="file"
              className="hidden"
              onChange={(event) => void handleUpload(event.target.files)}
            />
            <Button type="button" disabled={uploading} onClick={() => uploadInputRef.current?.click()}>
              <Upload size={15} />
              {uploading ? "Uploading..." : "Upload"}
            </Button>
            <SearchField
              placeholder="Search workspace"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              onSubmit={() => void runSearch()}
            />
            <Button type="button" onClick={() => void runSearch()}>
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
            <IconButton type="button" onClick={() => void load()} title="Refresh">
              <RefreshCw size={16} />
            </IconButton>
          </>
        }
      />

      {error ? <div className="error-strip">{error}</div> : null}
      {notice ? <div className="success-strip">{notice}</div> : null}
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
            <FileTree
              nodes={tree}
              selectedPath={selected?.path}
              onSelect={selectFile}
              renderActions={renderTreeActions}
            />
          )
        }
      >
        {selected ? (
          <>
            <div className="content-heading">
              <div>
                <h3>
                  {selected.name || selected.path}
                  {dirty ? " •" : ""}
                </h3>
                <span>
                  {formatBytes(selected.size)} {formatTimestamp(selected.modified)}
                </span>
              </div>
              <div className="toolbar-actions">
                {selected.text ? (
                  <>
                    <Button
                      type="button"
                      variant={viewMode === "preview" ? "primary" : "ghost"}
                      onClick={() => setViewMode("preview")}
                    >
                      <Eye size={14} />
                      Preview
                    </Button>
                    <Button
                      type="button"
                      variant={viewMode === "edit" ? "primary" : "ghost"}
                      onClick={() => setViewMode("edit")}
                    >
                      <Edit3 size={14} />
                      Edit
                    </Button>
                    <Button type="button" disabled={!dirty || saving} onClick={() => void saveEditor()}>
                      <Save size={14} />
                      {saving ? "Saving..." : "Save"}
                    </Button>
                    <IconButton
                      type="button"
                      disabled={!dirty || saving}
                      onClick={discardEditor}
                      title="Discard changes"
                      aria-label="Discard changes"
                    >
                      <RotateCcw size={15} />
                    </IconButton>
                  </>
                ) : (
                  <Button
                    type="button"
                    variant="ghost"
                    onClick={() => window.open(workspaceBlobUrl(selected.path), "_blank", "noopener,noreferrer")}
                  >
                    <Download size={14} />
                    Download
                  </Button>
                )}
                <IconButton
                  type="button"
                  variant="danger"
                  title={`Delete ${selected.path}`}
                  aria-label={`Delete ${selected.path}`}
                  onClick={() => {
                    if (!guardLocalDiscard()) return;
                    setDeleteTarget(selected);
                  }}
                >
                  <Trash2 size={15} />
                </IconButton>
                <span>{selected.mime_type}</span>
              </div>
            </div>
            {viewMode === "edit" && selected.text ? (
              <TextEditor
                value={editorValue}
                path={selected.path}
                onChange={setEditorValue}
                onSave={() => void saveEditor()}
              />
            ) : selected.text ? (
              selected.path.endsWith(".md") ? (
                <Markdown content={selected.content} />
              ) : (
                <pre className="file-pre">{selected.content}</pre>
              )
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

      <ConfirmDialog
        open={Boolean(deleteTarget)}
        title={`Delete ${deleteTarget?.name || "entry"}?`}
        description={
          <>
            <p>
              This permanently deletes the selected{" "}
              {deleteTarget?.type === "dir" ? "directory and all of its contents" : "file"}.
            </p>
            <code className="confirm-path">{deleteTarget?.path}</code>
          </>
        }
        confirmLabel={deleting ? "Deleting..." : "Delete"}
        confirmDisabled={deleting}
        onCancel={() => {
          if (!deleting) setDeleteTarget(null);
        }}
        onConfirm={() => void confirmDelete()}
      />
    </PageShell>
  );
}

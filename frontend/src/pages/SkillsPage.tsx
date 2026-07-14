import {
  CheckCircle2,
  Edit3,
  Eye,
  FilePlus2,
  FolderPlus,
  Pencil,
  Plus,
  Power,
  PowerOff,
  RefreshCw,
  RotateCcw,
  Save,
  Search,
  Trash2,
  X,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { ConflictEditor, TextEditor } from "../components/TextEditor";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { FileTree } from "../components/FileTree";
import { Markdown } from "../components/Markdown";
import {
  BrowserLayout,
  Button,
  EmptyState,
  IconButton,
  PageShell,
  PageToolbar,
  SearchField,
  StatusBadge,
} from "../components/ui";
import { useUnsavedChanges } from "../context/UnsavedChangesContext";
import {
  ApiError,
  createSkill,
  createSkillEntry,
  deleteSkillEntry,
  deleteSkillPath,
  getSkillsInfo,
  getSkillsTree,
  moveSkillEntry,
  readSkillFile,
  searchSkills,
  updateSkillState,
  writeSkillFile,
} from "../lib/api";
import { formatBytes, formatTimestamp } from "../lib/format";
import type {
  FileNode,
  FileReadResult,
  SearchResult,
  SkillMetadata,
  SkillValidationIssue,
  SkillsInfo,
} from "../types";

interface NewSkillForm {
  name: string;
  description: string;
  body: string;
}

interface EntryDialogState {
  mode: "file" | "directory" | "move";
  parentPath: string;
  name: string;
  node?: FileNode;
}

const emptyForm: NewSkillForm = { name: "", description: "", body: "" };

function skillForPath(skills: SkillMetadata[], path?: string): SkillMetadata | undefined {
  if (!path) return undefined;
  const root = path.split("/")[0];
  return skills.find((skill) => skill.path === root || skill.name === root);
}

function isSkillMarkdown(path?: string): boolean {
  return path === "SKILL.md" || path?.endsWith("/SKILL.md") === true;
}

function isRootSkillMarkdown(node: FileNode): boolean {
  return node.type === "file" && node.name === "SKILL.md" && node.path.split("/").length === 2;
}

function parentPath(path: string): string {
  const parts = path.split("/");
  parts.pop();
  return parts.join("/");
}

function stripYamlQuotes(value: string): string {
  const text = value.trim();
  if ((text.startsWith('"') && text.endsWith('"')) || (text.startsWith("'") && text.endsWith("'"))) {
    return text.slice(1, -1);
  }
  return text;
}

function parseSkillDocument(content: string): { body: string; fields: Record<string, string> } {
  const match = content.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?/);
  if (!match) return { body: content, fields: {} };
  const fields: Record<string, string> = {};
  for (const line of match[1].split(/\r?\n/)) {
    const field = line.match(/^([A-Za-z0-9_-]+):\s*(.*)$/);
    if (field) fields[field[1]] = stripYamlQuotes(field[2]);
  }
  return { body: content.slice(match[0].length), fields };
}

export function SkillsPage() {
  const { setDirty } = useUnsavedChanges();
  const [info, setInfo] = useState<SkillsInfo | null>(null);
  const [tree, setTree] = useState<FileNode[]>([]);
  const [skills, setSkills] = useState<SkillMetadata[]>([]);
  const [selected, setSelected] = useState<FileReadResult | null>(null);
  const [editorValue, setEditorValue] = useState("");
  const [viewMode, setViewMode] = useState<"preview" | "edit">("preview");
  const [conflict, setConflict] = useState<FileReadResult | null>(null);
  const [validationIssues, setValidationIssues] = useState<SkillValidationIssue[]>([]);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResult[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [creating, setCreating] = useState(false);
  const [saving, setSaving] = useState(false);
  const [form, setForm] = useState<NewSkillForm>(emptyForm);
  const [entryDialog, setEntryDialog] = useState<EntryDialogState | null>(null);
  const [entrySaving, setEntrySaving] = useState(false);
  const [deleteEntry, setDeleteEntry] = useState<FileNode | null>(null);

  const selectedSkill = useMemo(() => skillForPath(skills, selected?.path), [skills, selected]);
  const dirty = Boolean(selected?.text && editorValue !== selected.content);

  useEffect(() => {
    setDirty(dirty);
    return () => setDirty(false);
  }, [dirty, setDirty]);

  const refreshCatalog = async () => {
    const [skillsInfo, skillsTree] = await Promise.all([getSkillsInfo(), getSkillsTree()]);
    setInfo(skillsInfo);
    setTree(skillsTree.tree || []);
    setSkills(skillsTree.skills || skillsInfo.skills || []);
  };

  const load = async () => {
    setLoading(true);
    setError("");
    try {
      await refreshCatalog();
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
    setConflict(null);
    setValidationIssues([]);
    setViewMode("preview");
    return true;
  };

  const openFile = async (path: string, mode: "preview" | "edit" = "preview") => {
    const file = await readSkillFile(path);
    setSelected(file);
    setEditorValue(file.content);
    setConflict(null);
    setValidationIssues([]);
    setViewMode(file.text ? mode : "preview");
    return file;
  };

  const selectFile = async (node: FileNode) => {
    if (!guardLocalDiscard()) return;
    setCreating(false);
    setError("");
    setNotice("");
    try {
      await openFile(node.path);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const refreshPage = async () => {
    if (!guardLocalDiscard()) return;
    setError("");
    setNotice("");
    try {
      await refreshCatalog();
      if (selected) await openFile(selected.path, viewMode);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const runSearch = async () => {
    const text = query.trim();
    if (!text) return;
    setError("");
    try {
      const data = await searchSkills(text);
      setResults(data.results || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const submitNewSkill = async () => {
    const name = form.name.trim();
    const description = form.description.trim();
    if (!name || !description) {
      setError("Name and description are required");
      return;
    }
    setSaving(true);
    setError("");
    try {
      const created = await createSkill({ name, description, body: form.body });
      setForm(emptyForm);
      setCreating(false);
      await refreshCatalog();
      await openFile(created.skill.skill_file);
      setNotice(`Created ${created.skill.name}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  const saveEditor = async () => {
    if (!selected?.text || !dirty || saving) return;
    setSaving(true);
    setError("");
    setNotice("");
    setValidationIssues([]);
    try {
      const updated = await writeSkillFile({
        path: selected.path,
        content: editorValue,
        expected_revision: conflict?.revision || selected.revision,
        create_parents: false,
      });
      setSelected(updated);
      setEditorValue(updated.content);
      setConflict(null);
      setNotice(`Saved ${updated.path}`);
      await refreshCatalog();
    } catch (err) {
      if (err instanceof ApiError && err.code === "skill_validation_failed") {
        setValidationIssues(err.detail?.issues || []);
      } else if (err instanceof ApiError && err.code === "revision_conflict" && err.detail?.current) {
        setConflict(err.detail.current);
        setViewMode("edit");
        setNotice("This file changed on disk. Review both versions and save the merged result.");
      } else {
        setError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setSaving(false);
    }
  };

  const discardEditor = () => {
    const base = conflict || selected;
    setSelected(base);
    setEditorValue(base?.content || "");
    setConflict(null);
    setValidationIssues([]);
    setViewMode("preview");
    setNotice("");
  };

  const toggleSelectedSkill = async (skill: SkillMetadata) => {
    setError("");
    try {
      await updateSkillState(skill.name, !skill.enabled);
      await refreshCatalog();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const deleteSkill = async (skill: SkillMetadata) => {
    if (!guardLocalDiscard() || !window.confirm(`Delete skill ${skill.name}?`)) return;
    setError("");
    try {
      await deleteSkillPath(skill.path, true);
      setSelected(null);
      await refreshCatalog();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const beginCreateEntry = (node: FileNode, mode: "file" | "directory") => {
    if (!guardLocalDiscard()) return;
    const target = node.type === "dir" ? node.path : parentPath(node.path);
    setEntryDialog({ mode, parentPath: target, name: "" });
  };

  const beginMoveEntry = (node: FileNode) => {
    if (!guardLocalDiscard()) return;
    setEntryDialog({ mode: "move", node, parentPath: parentPath(node.path), name: node.name });
  };

  const submitEntryDialog = async () => {
    if (!entryDialog) return;
    const name = entryDialog.name.trim();
    const destinationParent = entryDialog.parentPath.trim().replace(/^\/+|\/+$/g, "");
    if (!name || !destinationParent) {
      setError("Name and parent path are required");
      return;
    }
    setEntrySaving(true);
    setError("");
    try {
      if (entryDialog.mode === "move" && entryDialog.node) {
        let revision = entryDialog.node.revision;
        if (entryDialog.node.type === "file" && !revision) {
          revision = (await readSkillFile(entryDialog.node.path)).revision;
        }
        const moved = await moveSkillEntry({
          path: entryDialog.node.path,
          new_parent_path: destinationParent,
          new_name: name,
          expected_revision: revision,
        });
        const previousPath = entryDialog.node.path;
        const nextPath = moved.entry.path;
        const selectedNextPath = selected?.path === previousPath || selected?.path.startsWith(`${previousPath}/`)
          ? `${nextPath}${selected.path.slice(previousPath.length)}`
          : null;
        await refreshCatalog();
        if (selectedNextPath) await openFile(selectedNextPath);
        setNotice(`Moved to ${nextPath}`);
      } else {
        const created = await createSkillEntry({
          parent_path: destinationParent,
          name,
          kind: entryDialog.mode === "directory" ? "directory" : "file",
        });
        await refreshCatalog();
        if (entryDialog.mode === "file") await openFile(created.entry.path, "edit");
        setNotice(`Created ${created.entry.path}`);
      }
      setEntryDialog(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setEntrySaving(false);
    }
  };

  const beginDeleteEntry = (node: FileNode) => {
    if (!guardLocalDiscard()) return;
    setDeleteEntry(node);
  };

  const submitDeleteEntry = async () => {
    if (!deleteEntry) return;
    setEntrySaving(true);
    setError("");
    try {
      let revision = deleteEntry.revision;
      if (deleteEntry.type === "file" && !revision) revision = (await readSkillFile(deleteEntry.path)).revision;
      await deleteSkillEntry(deleteEntry.path, deleteEntry.type === "dir", revision);
      if (selected?.path === deleteEntry.path || selected?.path.startsWith(`${deleteEntry.path}/`)) {
        setSelected(null);
        setEditorValue("");
      }
      setNotice(`Deleted ${deleteEntry.path}`);
      setDeleteEntry(null);
      await refreshCatalog();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setEntrySaving(false);
    }
  };

  const renderSkillStateAction = (skill: SkillMetadata) => (
    <Button type="button" className="skill-toggle-button" onClick={() => void toggleSelectedSkill(skill)}>
      {skill.enabled ? <PowerOff size={15} /> : <Power size={15} />}
      {skill.enabled ? "Disable Skill" : "Enable Skill"}
    </Button>
  );

  const renderSkillManagementActions = (skill: SkillMetadata) => (
    <div className="skill-actions">
      {renderSkillStateAction(skill)}
      <Button
        type="button"
        variant="danger"
        onClick={() => void deleteSkill(skill)}
        title={`Delete ${skill.name}`}
        aria-label={`Delete ${skill.name}`}
      >
        <Trash2 size={15} />
        Delete Skill
      </Button>
    </div>
  );

  const renderTreeActions = (node: FileNode) => {
    const skillRoot = node.type === "dir" && node.path.split("/").length === 1
      && skills.some((skill) => skill.path === node.path);
    const protectedNode = skillRoot || isRootSkillMarkdown(node);
    return (
      <>
        {node.type === "dir" && (skillRoot || node.path.split("/").length > 1) ? (
          <>
            <IconButton type="button" title={`New file in ${node.path}`} aria-label={`New file in ${node.path}`} onClick={() => beginCreateEntry(node, "file")}>
              <FilePlus2 size={13} />
            </IconButton>
            <IconButton type="button" title={`New folder in ${node.path}`} aria-label={`New folder in ${node.path}`} onClick={() => beginCreateEntry(node, "directory")}>
              <FolderPlus size={13} />
            </IconButton>
          </>
        ) : null}
        {!protectedNode && node.path.split("/").length > 1 ? (
          <>
            <IconButton type="button" title={`Rename or move ${node.path}`} aria-label={`Rename or move ${node.path}`} onClick={() => beginMoveEntry(node)}>
              <Pencil size={12} />
            </IconButton>
            <IconButton type="button" variant="danger" title={`Delete ${node.path}`} aria-label={`Delete ${node.path}`} onClick={() => beginDeleteEntry(node)}>
              <Trash2 size={12} />
            </IconButton>
          </>
        ) : null}
      </>
    );
  };

  const renderCreateForm = () => (
    <section className="skill-form-panel">
      <div className="content-heading">
        <h3>New Skill</h3>
        <IconButton type="button" onClick={() => setCreating(false)} title="Close"><X size={16} /></IconButton>
      </div>
      <div className="skill-form">
        <label className="form-field"><span>Name</span><input value={form.name} placeholder="my-skill" onChange={(event) => setForm((value) => ({ ...value, name: event.target.value }))} /></label>
        <label className="form-field"><span>Description</span><input value={form.description} placeholder="Does one focused job. Use when..." onChange={(event) => setForm((value) => ({ ...value, description: event.target.value }))} /></label>
        <label className="form-field"><span>Body</span><textarea value={form.body} placeholder="# My Skill\n\n## Instructions\n" onChange={(event) => setForm((value) => ({ ...value, body: event.target.value }))} /></label>
        <div className="toolbar-actions"><Button type="button" disabled={saving} onClick={() => void submitNewSkill()}><CheckCircle2 size={15} />Create</Button></div>
      </div>
    </section>
  );

  const renderOverview = () => (
    <>
      <div className="content-heading">
        <div><h3>Skills</h3><span>{info?.root || "skills"}</span></div>
        <div className="chip-list">
          <StatusBadge tone="good">{info?.enabled_count || 0} enabled</StatusBadge>
          <StatusBadge tone="muted">{info?.disabled_count || 0} disabled</StatusBadge>
          <StatusBadge tone={info?.invalid_count ? "danger" : "muted"}>{info?.invalid_count || 0} invalid</StatusBadge>
        </div>
      </div>
      {skills.length ? <div className="skill-list">{skills.map((skill) => (
        <section key={skill.path} className="skill-summary">
          <div className="min-w-0"><h4>{skill.name}</h4><p>{skill.description || "No description"}</p><div className="chip-list"><StatusBadge tone={skill.enabled ? "good" : "muted"}>{skill.enabled ? "enabled" : "disabled"}</StatusBadge><StatusBadge tone={skill.valid ? "good" : "danger"}>{skill.valid ? "valid" : "invalid"}</StatusBadge><span className="data-chip path-chip">{skill.skill_file}</span></div></div>
          {renderSkillManagementActions(skill)}
        </section>
      ))}</div> : <EmptyState title="No skills found" className="h-full" />}
    </>
  );

  const renderSkillDocument = (file: FileReadResult) => {
    const document = parseSkillDocument(file.content);
    const description = document.fields.description || selectedSkill?.description || "";
    const title = document.fields.name || selectedSkill?.name || file.name || file.path;
    const body = document.body.trim();
    const allowedTools = selectedSkill?.allowed_tools || document.fields["allowed-tools"];
    return (
      <article className="skill-document">
        <header className="skill-hero"><div className="skill-hero-main"><span className="skill-kicker">Agent Skill</span><h3>{title}</h3>{description ? <p>{description}</p> : null}</div>{selectedSkill ? <div className="skill-hero-status"><span className={`status-dot-chip ${selectedSkill.enabled ? "is-enabled" : "is-disabled"}`}>{selectedSkill.enabled ? "Enabled" : "Disabled"}</span><span className={`status-dot-chip ${selectedSkill.valid ? "is-valid" : "is-invalid"}`}>{selectedSkill.valid ? "Valid" : "Invalid"}</span></div> : null}</header>
        <dl className="skill-detail-grid"><div><dt>File</dt><dd>{file.path}</dd></div><div><dt>Size</dt><dd>{formatBytes(new Blob([file.content]).size)}</dd></div><div><dt>Updated</dt><dd>{formatTimestamp(file.modified)}</dd></div>{selectedSkill?.compatibility ? <div><dt>Compatibility</dt><dd>{selectedSkill.compatibility}</dd></div> : null}{selectedSkill?.license ? <div><dt>License</dt><dd>{selectedSkill.license}</dd></div> : null}{allowedTools ? <div><dt>Allowed Tools</dt><dd>{allowedTools}</dd></div> : null}</dl>
        {selectedSkill?.errors?.length ? <div className="skill-issues">{selectedSkill.errors.map((issue) => <div key={`${issue.path}-${issue.code}-${issue.message}`}><strong>{issue.code}</strong><span>{issue.message}</span></div>)}</div> : null}
        {body ? <section className="skill-body-panel"><Markdown content={body} className="skill-markdown" /></section> : <EmptyState title="No instructions in this skill" />}
      </article>
    );
  };

  const renderPreview = (file: FileReadResult) => file.text && isSkillMarkdown(file.path)
    ? renderSkillDocument(file)
    : file.text && file.path.endsWith(".md")
      ? <Markdown content={file.content} />
      : file.text
        ? <pre className="file-pre">{file.content}</pre>
        : <EmptyState title="Binary file preview is unavailable" />;

  const renderSelected = () => {
    if (!selected) return renderOverview();
    const previewFile = { ...selected, content: editorValue };
    const isSkillHome = Boolean(selectedSkill && selected.path === selectedSkill.skill_file);
    const pathParts = selected.path.split("/");
    const openSkillHome = () => {
      if (!selectedSkill || isSkillHome) return;
      void selectFile({
        name: "SKILL.md",
        path: selectedSkill.skill_file,
        type: "file",
      });
    };
    return (
      <>
        <nav className="skill-breadcrumb" aria-label="Skill file location">
          <button type="button" disabled={isSkillHome || !selectedSkill} onClick={openSkillHome} title="Open Skill home">
            {selectedSkill?.name || pathParts[0]}
          </button>
          {pathParts.slice(1).map((part, index) => (
            <span key={`${part}-${index}`}>
              <b>/</b>
              {part}
            </span>
          ))}
        </nav>
        <div className="content-heading">
          <div><h3>{selected.name || selected.path}{dirty ? " •" : ""}</h3><span>{formatBytes(selected.size)} {formatTimestamp(selected.modified)}</span></div>
          <div className="toolbar-actions">
            {selected.text ? <><Button type="button" variant={viewMode === "preview" ? "primary" : "ghost"} onClick={() => setViewMode("preview")}><Eye size={14} />Preview</Button><Button type="button" variant={viewMode === "edit" ? "primary" : "ghost"} onClick={() => setViewMode("edit")}><Edit3 size={14} />Edit</Button><Button type="button" disabled={!dirty || saving} onClick={() => void saveEditor()}><Save size={14} />{conflict ? "Save merged result" : saving ? "Saving..." : "Save"}</Button><IconButton type="button" disabled={!dirty || saving} onClick={discardEditor} title="Discard changes" aria-label="Discard changes"><RotateCcw size={15} /></IconButton></> : null}
            {isSkillHome && selectedSkill ? renderSkillStateAction(selectedSkill) : null}
            {!isSkillHome && selectedSkill ? <><Button type="button" onClick={() => beginMoveEntry(selected)}><Pencil size={14} />Rename / Move</Button><Button type="button" variant="danger" onClick={() => beginDeleteEntry(selected)}><Trash2 size={14} />Delete File</Button></> : null}
          </div>
        </div>
        {validationIssues.length ? <div className="skill-issues">{validationIssues.map((issue, index) => <div key={`${issue.code}-${index}`}><strong>{issue.code}{issue.line ? ` at ${issue.line}:${issue.column || 1}` : ""}</strong><span>{issue.message}</span></div>)}</div> : null}
        {conflict ? <><div className="warning-strip">The left version is current on disk. Edit the right draft into the intended result, then save it against the latest revision.</div><ConflictEditor current={conflict.content} value={editorValue} path={selected.path} onChange={setEditorValue} onSave={() => void saveEditor()} /></> : viewMode === "edit" && selected.text ? <TextEditor value={editorValue} path={selected.path} onChange={setEditorValue} onSave={() => void saveEditor()} /> : renderPreview(previewFile)}
      </>
    );
  };

  return (
    <PageShell>
      <PageToolbar title="Skills" subtitle={info?.root || "Agent skills"} actions={<><Button type="button" onClick={() => { if (!guardLocalDiscard()) return; setSelected(null); setCreating(true); }}><Plus size={15} />New Skill</Button><SearchField placeholder="Search skills" value={query} onChange={(event) => setQuery(event.target.value)} onSubmit={() => void runSearch()} /><Button type="button" onClick={() => void runSearch()}><Search size={15} />Search</Button><IconButton type="button" onClick={() => { setQuery(""); setResults([]); }} title="Clear search"><X size={16} /></IconButton><IconButton type="button" onClick={() => void refreshPage()} title="Refresh"><RefreshCw size={16} /></IconButton></>} />
      {error ? <div className="error-strip">{error}</div> : null}
      {notice ? <div className="success-strip">{notice}</div> : null}
      <BrowserLayout sidebar={results.length ? <div className="space-y-2">{results.map((item) => <button key={item.path} type="button" className="search-result" onClick={() => void selectFile(item)}><strong>{item.path}</strong>{item.snippet ? <span>{item.snippet}</span> : null}</button>)}</div> : loading ? <EmptyState title="Loading..." /> : <FileTree nodes={tree} selectedPath={selected?.path} onSelect={selectFile} renderActions={renderTreeActions} />}>{creating ? renderCreateForm() : renderSelected()}</BrowserLayout>

      <ConfirmDialog open={Boolean(entryDialog)} title={entryDialog?.mode === "move" ? "Rename or move entry" : `New ${entryDialog?.mode || "entry"}`} description={entryDialog?.mode === "move" ? "The destination must remain inside the same Skill package." : "Create the entry inside an existing Skill package."} confirmLabel={entrySaving ? "Saving..." : entryDialog?.mode === "move" ? "Move" : "Create"} confirmVariant="primary" confirmDisabled={entrySaving || !entryDialog?.name.trim() || !entryDialog?.parentPath.trim()} onCancel={() => { if (!entrySaving) setEntryDialog(null); }} onConfirm={() => void submitEntryDialog()}>{entryDialog ? <><label className="form-field"><span>Name</span><input value={entryDialog.name} autoFocus onChange={(event) => setEntryDialog({ ...entryDialog, name: event.target.value })} /></label><label className="form-field"><span>Parent path</span><input value={entryDialog.parentPath} onChange={(event) => setEntryDialog({ ...entryDialog, parentPath: event.target.value })} /></label></> : null}</ConfirmDialog>

      <ConfirmDialog open={Boolean(deleteEntry)} title={`Delete ${deleteEntry?.name || "entry"}?`} description={<><p>This permanently deletes the selected {deleteEntry?.type === "dir" ? "directory and all of its contents" : "file"}.</p><code className="confirm-path">{deleteEntry?.path}</code></>} confirmLabel={entrySaving ? "Deleting..." : "Delete"} confirmDisabled={entrySaving} onCancel={() => { if (!entrySaving) setDeleteEntry(null); }} onConfirm={() => void submitDeleteEntry()} />
    </PageShell>
  );
}

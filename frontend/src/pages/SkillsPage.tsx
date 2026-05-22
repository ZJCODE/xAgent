import { CheckCircle2, Plus, Power, PowerOff, RefreshCw, Search, Trash2, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { FileTree } from "../components/FileTree";
import { Markdown } from "../components/Markdown";
import {
  createSkill,
  deleteSkillPath,
  getSkillsInfo,
  getSkillsTree,
  readSkillFile,
  searchSkills,
  updateSkillState,
} from "../lib/api";
import { formatBytes, formatTimestamp } from "../lib/format";
import type { FileNode, FileReadResult, SearchResult, SkillMetadata, SkillsInfo } from "../types";

interface NewSkillForm {
  name: string;
  description: string;
  body: string;
}

const emptyForm: NewSkillForm = {
  name: "",
  description: "",
  body: "",
};

function skillForPath(skills: SkillMetadata[], path?: string): SkillMetadata | undefined {
  if (!path) return undefined;
  const root = path.split("/")[0];
  return skills.find((skill) => skill.path === root || skill.name === root);
}

export function SkillsPage() {
  const [info, setInfo] = useState<SkillsInfo | null>(null);
  const [tree, setTree] = useState<FileNode[]>([]);
  const [skills, setSkills] = useState<SkillMetadata[]>([]);
  const [selected, setSelected] = useState<FileReadResult | null>(null);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResult[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [creating, setCreating] = useState(false);
  const [saving, setSaving] = useState(false);
  const [form, setForm] = useState<NewSkillForm>(emptyForm);

  const selectedSkill = useMemo(() => skillForPath(skills, selected?.path), [skills, selected]);

  const load = async () => {
    setLoading(true);
    setError("");
    try {
      const [skillsInfo, skillsTree] = await Promise.all([getSkillsInfo(), getSkillsTree()]);
      setInfo(skillsInfo);
      setTree(skillsTree.tree || []);
      setSkills(skillsTree.skills || skillsInfo.skills || []);
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
    setCreating(false);
    setError("");
    try {
      setSelected(await readSkillFile(node.path));
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
      await load();
      setSelected(await readSkillFile(created.skill.skill_file));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  const toggleSelectedSkill = async (skill: SkillMetadata) => {
    setError("");
    try {
      await updateSkillState(skill.name, !skill.enabled);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const deleteSkill = async (skill: SkillMetadata) => {
    if (!window.confirm(`Delete skill ${skill.name}?`)) return;
    setError("");
    try {
      await deleteSkillPath(skill.path, true);
      setSelected(null);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const renderSkillActions = (skill: SkillMetadata) => (
    <div className="toolbar-actions">
      <button type="button" className="ghost-button icon-text-button" onClick={() => void toggleSelectedSkill(skill)}>
        {skill.enabled ? <PowerOff size={15} /> : <Power size={15} />}
        {skill.enabled ? "Disable" : "Enable"}
      </button>
      <button type="button" className="danger-button" onClick={() => void deleteSkill(skill)}>
        <Trash2 size={15} />
        Delete
      </button>
    </div>
  );

  const renderCreateForm = () => (
    <section className="skill-form-panel">
      <div className="content-heading">
        <h3>New Skill</h3>
        <button type="button" className="ghost-button icon-button" onClick={() => setCreating(false)} title="Close">
          <X size={16} />
        </button>
      </div>
      <div className="skill-form">
        <label className="form-field">
          <span>Name</span>
          <input
            value={form.name}
            placeholder="my-skill"
            onChange={(event) => setForm((value) => ({ ...value, name: event.target.value }))}
          />
        </label>
        <label className="form-field">
          <span>Description</span>
          <input
            value={form.description}
            placeholder="Does one focused job. Use when..."
            onChange={(event) => setForm((value) => ({ ...value, description: event.target.value }))}
          />
        </label>
        <label className="form-field">
          <span>Body</span>
          <textarea
            value={form.body}
            placeholder="# My Skill\n\n## Instructions\n"
            onChange={(event) => setForm((value) => ({ ...value, body: event.target.value }))}
          />
        </label>
        <div className="toolbar-actions">
          <button type="button" className="ghost-button icon-text-button" disabled={saving} onClick={submitNewSkill}>
            <CheckCircle2 size={15} />
            Create
          </button>
        </div>
      </div>
    </section>
  );

  const renderOverview = () => (
    <>
      <div className="content-heading">
        <div>
          <h3>Skills</h3>
          <span>{info?.root || "skills"}</span>
        </div>
        <div className="chip-list">
          <span className="data-chip">{info?.enabled_count || 0} enabled</span>
          <span className="data-chip">{info?.disabled_count || 0} disabled</span>
          <span className="data-chip">{info?.invalid_count || 0} invalid</span>
        </div>
      </div>
      {skills.length ? (
        <div className="skill-list">
          {skills.map((skill) => (
            <section key={skill.path} className="skill-summary">
              <div className="min-w-0">
                <h4>{skill.name}</h4>
                <p>{skill.description || "No description"}</p>
                <div className="chip-list">
                  <span className="data-chip">{skill.enabled ? "enabled" : "disabled"}</span>
                  <span className="data-chip">{skill.valid ? "valid" : "invalid"}</span>
                  <span className="data-chip path-chip">{skill.skill_file}</span>
                </div>
              </div>
              {renderSkillActions(skill)}
            </section>
          ))}
        </div>
      ) : (
        <div className="empty-state h-full">No skills found</div>
      )}
    </>
  );

  const renderSelected = () => {
    if (!selected) return renderOverview();
    return (
      <>
        <div className="content-heading">
          <div>
            <h3>{selected.name || selected.path}</h3>
            <span>
              {formatBytes(selected.size)} {formatTimestamp(selected.modified)}
            </span>
          </div>
          {selectedSkill ? renderSkillActions(selectedSkill) : <span>{selected.mime_type}</span>}
        </div>
        {selectedSkill ? (
          <div className="skill-meta-row">
            <span className="data-chip">{selectedSkill.enabled ? "enabled" : "disabled"}</span>
            <span className="data-chip">{selectedSkill.valid ? "valid" : "invalid"}</span>
            {selectedSkill.compatibility ? <span className="data-chip">{selectedSkill.compatibility}</span> : null}
            {selectedSkill.license ? <span className="data-chip">{selectedSkill.license}</span> : null}
          </div>
        ) : null}
        {selected.text && selected.path.endsWith(".md") ? (
          <Markdown content={selected.content} />
        ) : selected.text ? (
          <pre className="file-pre">{selected.content}</pre>
        ) : (
          <div className="empty-state">Binary file preview is unavailable</div>
        )}
      </>
    );
  };

  return (
    <div className="console-page">
      <section className="console-toolbar">
        <div className="min-w-0">
          <h2>Skills</h2>
          <p>{info?.root || "Agent skills"}</p>
        </div>
        <div className="console-toolbar-actions">
          <button
            type="button"
            className="ghost-button icon-text-button"
            onClick={() => {
              setSelected(null);
              setCreating(true);
            }}
          >
            <Plus size={15} />
            New
          </button>
          <div className="search-control skills-search-control">
            <input
              className="search-input"
              placeholder="Search skills"
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
          {creating ? renderCreateForm() : selected ? renderSelected() : renderOverview()}
        </main>
      </div>
    </div>
  );
}
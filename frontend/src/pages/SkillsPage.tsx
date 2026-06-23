import { CheckCircle2, Plus, Power, PowerOff, RefreshCw, Search, Trash2, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
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

function isSkillMarkdown(path?: string): boolean {
  return path === "SKILL.md" || path?.endsWith("/SKILL.md") === true;
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
    if (field) {
      fields[field[1]] = stripYamlQuotes(field[2]);
    }
  }

  return {
    body: content.slice(match[0].length),
    fields,
  };
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
    <div className="skill-actions">
      <Button type="button" className="skill-toggle-button" onClick={() => void toggleSelectedSkill(skill)}>
        {skill.enabled ? <PowerOff size={15} /> : <Power size={15} />}
        {skill.enabled ? "Disable" : "Enable"}
      </Button>
      <IconButton
        type="button"
        variant="danger"
        onClick={() => void deleteSkill(skill)}
        title={`Delete ${skill.name}`}
        aria-label={`Delete ${skill.name}`}
      >
        <Trash2 size={15} />
      </IconButton>
    </div>
  );

  const renderCreateForm = () => (
    <section className="skill-form-panel">
      <div className="content-heading">
        <h3>New Skill</h3>
        <IconButton type="button" onClick={() => setCreating(false)} title="Close">
          <X size={16} />
        </IconButton>
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
          <Button type="button" disabled={saving} onClick={submitNewSkill}>
            <CheckCircle2 size={15} />
            Create
          </Button>
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
          <StatusBadge tone="good">{info?.enabled_count || 0} enabled</StatusBadge>
          <StatusBadge tone="muted">{info?.disabled_count || 0} disabled</StatusBadge>
          <StatusBadge tone={info?.invalid_count ? "danger" : "muted"}>{info?.invalid_count || 0} invalid</StatusBadge>
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
                  <StatusBadge tone={skill.enabled ? "good" : "muted"}>{skill.enabled ? "enabled" : "disabled"}</StatusBadge>
                  <StatusBadge tone={skill.valid ? "good" : "danger"}>{skill.valid ? "valid" : "invalid"}</StatusBadge>
                  <span className="data-chip path-chip">{skill.skill_file}</span>
                </div>
              </div>
              {renderSkillActions(skill)}
            </section>
          ))}
        </div>
      ) : (
        <EmptyState title="No skills found" className="h-full" />
      )}
    </>
  );

  const renderSkillDocument = () => {
    if (!selected) return null;

    const document = parseSkillDocument(selected.content);
    const description = selectedSkill?.description || document.fields.description || "";
    const title = selectedSkill?.name || document.fields.name || selected.name || selected.path;
    const body = document.body.trim();
    const allowedTools = selectedSkill?.allowed_tools || document.fields["allowed-tools"];

    return (
      <article className="skill-document">
        <header className="skill-hero">
          <div className="skill-hero-main">
            <span className="skill-kicker">Agent Skill</span>
            <h3>{title}</h3>
            {description ? <p>{description}</p> : null}
          </div>
          {selectedSkill ? (
            <div className="skill-hero-status">
              <span className={`status-dot-chip ${selectedSkill.enabled ? "is-enabled" : "is-disabled"}`}>
                {selectedSkill.enabled ? "Enabled" : "Disabled"}
              </span>
              <span className={`status-dot-chip ${selectedSkill.valid ? "is-valid" : "is-invalid"}`}>
                {selectedSkill.valid ? "Valid" : "Invalid"}
              </span>
            </div>
          ) : null}
        </header>

        <dl className="skill-detail-grid">
          <div>
            <dt>File</dt>
            <dd>{selected.path}</dd>
          </div>
          <div>
            <dt>Size</dt>
            <dd>{formatBytes(selected.size)}</dd>
          </div>
          <div>
            <dt>Updated</dt>
            <dd>{formatTimestamp(selected.modified)}</dd>
          </div>
          {selectedSkill?.compatibility ? (
            <div>
              <dt>Compatibility</dt>
              <dd>{selectedSkill.compatibility}</dd>
            </div>
          ) : null}
          {selectedSkill?.license ? (
            <div>
              <dt>License</dt>
              <dd>{selectedSkill.license}</dd>
            </div>
          ) : null}
          {allowedTools ? (
            <div>
              <dt>Allowed Tools</dt>
              <dd>{allowedTools}</dd>
            </div>
          ) : null}
        </dl>

        {selectedSkill?.errors?.length ? (
          <div className="skill-issues">
            {selectedSkill.errors.map((issue) => (
              <div key={`${issue.path}-${issue.code}-${issue.message}`}>
                <strong>{issue.code}</strong>
                <span>{issue.message}</span>
              </div>
            ))}
          </div>
        ) : null}

        {body ? (
          <section className="skill-body-panel">
            <Markdown content={body} className="skill-markdown" />
          </section>
        ) : (
          <EmptyState title="No instructions in this skill" />
        )}
      </article>
    );
  };

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
        {selectedSkill && !isSkillMarkdown(selected.path) ? (
          <div className="skill-meta-row">
            <StatusBadge tone={selectedSkill.enabled ? "good" : "muted"}>{selectedSkill.enabled ? "enabled" : "disabled"}</StatusBadge>
            <StatusBadge tone={selectedSkill.valid ? "good" : "danger"}>{selectedSkill.valid ? "valid" : "invalid"}</StatusBadge>
            {selectedSkill.compatibility ? <span className="data-chip">{selectedSkill.compatibility}</span> : null}
            {selectedSkill.license ? <span className="data-chip">{selectedSkill.license}</span> : null}
          </div>
        ) : null}
        {selected.text && isSkillMarkdown(selected.path) ? (
          renderSkillDocument()
        ) : selected.text && selected.path.endsWith(".md") ? (
          <Markdown content={selected.content} />
        ) : selected.text ? (
          <pre className="file-pre">{selected.content}</pre>
        ) : (
          <EmptyState title="Binary file preview is unavailable" />
        )}
      </>
    );
  };

  return (
    <PageShell>
      <PageToolbar
        title="Skills"
        subtitle={info?.root || "Agent skills"}
        actions={
          <>
            <Button
              type="button"
              onClick={() => {
                setSelected(null);
                setCreating(true);
              }}
            >
              <Plus size={15} />
              New
            </Button>
            <SearchField
              placeholder="Search skills"
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
          {creating ? renderCreateForm() : selected ? renderSelected() : renderOverview()}
      </BrowserLayout>
    </PageShell>
  );
}

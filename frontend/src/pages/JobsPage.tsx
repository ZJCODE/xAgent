import { Eye, Play, Plus, RefreshCw, Search, Square, Trash2, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Button, EmptyState, IconButton, PageShell, PageToolbar, SearchField, StatusBadge } from "../components/ui";
import { cancelJob, createJob, deleteJob, getJob, getJobs } from "../lib/api";
import type { BackgroundJobItem, JobScope, JobsResponse } from "../types";

const PAGE_SIZE = 50;
type PageScope = Exclude<JobScope, "current">;

function formatStamp(value?: string | null): string {
  if (!value) return "";
  const date = new Date(value.replace(" ", "T"));
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function jobTarget(job: BackgroundJobItem): string {
  const target = job.target || {};
  const userId = String(job.user_id || target.user_id || "");
  const chatId = String(target.chat_id || "");
  return [...new Set([String(job.channel || ""), chatId, userId].filter(Boolean))].join(" · ") || "local";
}

function jobStatusTone(status: string): "good" | "muted" | "danger" | "info" {
  if (status === "running" || status === "queued") return "info";
  if (status === "completed" || status === "cancelled") return "muted";
  if (status === "failed") return "danger";
  return "good";
}

function lifecycleTime(job: BackgroundJobItem): string {
  if (job.status === "running") return formatStamp(job.started_at) || "Running now";
  if (job.status === "queued") return formatStamp(job.created_at) || "Queued";
  if (job.status === "completed") return formatStamp(job.completed_at);
  if (job.status === "failed") return formatStamp(job.failed_at);
  if (job.status === "cancelled") return formatStamp(job.cancelled_at);
  return formatStamp(job.updated_at);
}

export function JobsPage() {
  const [scope, setScope] = useState<PageScope>("running");
  const [data, setData] = useState<JobsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [appliedQuery, setAppliedQuery] = useState("");
  const [selectedJob, setSelectedJob] = useState<BackgroundJobItem | null>(null);
  const [creating, setCreating] = useState(false);
  const [draftTitle, setDraftTitle] = useState("");
  const [draftCommand, setDraftCommand] = useState("");
  const [saving, setSaving] = useState(false);

  const jobs = useMemo(() => data?.jobs || [], [data]);

  const load = useCallback(
    async (append = false) => {
      if (!append) setLoading(true);
      setError("");
      try {
        const offset = append ? jobs.length : 0;
        const response = await getJobs(scope, appliedQuery, PAGE_SIZE, offset);
        setData((current) =>
          append && current
            ? { ...response, jobs: [...current.jobs, ...response.jobs], offset: 0 }
            : response,
        );
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        if (!append) setLoading(false);
      }
    },
    [appliedQuery, jobs.length, scope],
  );

  useEffect(() => {
    void load(false);
  }, [scope, appliedQuery]);

  useEffect(() => {
    if (scope === "archive") return;
    const timer = window.setInterval(() => {
      void getJobs(scope, appliedQuery, PAGE_SIZE, 0).then(setData).catch(() => undefined);
    }, 3000);
    return () => window.clearInterval(timer);
  }, [appliedQuery, scope]);

  const openDetails = async (job: BackgroundJobItem) => {
    try {
      const response = await getJob(job.job_id);
      setSelectedJob(response.job);
    } catch {
      setSelectedJob(job);
    }
  };

  const onCreate = async () => {
    if (!draftCommand.trim()) return;
    setSaving(true);
    setError("");
    try {
      await createJob({
        command: draftCommand.trim(),
        title: draftTitle.trim() || undefined,
      });
      setCreating(false);
      setDraftTitle("");
      setDraftCommand("");
      setScope("running");
      setAppliedQuery("");
      setQuery("");
      await load(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  const onCancel = async (job: BackgroundJobItem) => {
    setError("");
    try {
      await cancelJob(job.job_id);
      await load(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const onDelete = async (job: BackgroundJobItem) => {
    if (!window.confirm(`Permanently delete job “${job.title || job.job_id}”?`)) return;
    setError("");
    try {
      await deleteJob(job.job_id);
      if (selectedJob?.job_id === job.job_id) setSelectedJob(null);
      await load(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const renderActions = (job: BackgroundJobItem) => {
    if (job.status === "queued" || job.status === "running") {
      return (
        <div className="task-row-actions">
          <Button className="task-action-button" onClick={() => void openDetails(job)}>
            <Eye size={15} />
            View
          </Button>
          <Button className="task-action-button" onClick={() => void onCancel(job)} disabled={saving}>
            <Square size={15} />
            Cancel
          </Button>
        </div>
      );
    }
    return (
      <div className="task-row-actions">
        <Button className="task-action-button" onClick={() => void openDetails(job)}>
          <Eye size={15} />
          View
        </Button>
        <IconButton variant="danger" onClick={() => void onDelete(job)} title="Delete job">
          <Trash2 size={15} />
        </IconButton>
      </div>
    );
  };

  return (
    <PageShell>
      <PageToolbar
        title="Jobs"
        subtitle={data?.root || "jobs"}
        actions={
          <>
            <Button type="button" onClick={() => setCreating(true)}>
              <Plus size={15} />
              Create
            </Button>
            <SearchField
              placeholder={`Search ${scope}`}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              onSubmit={() => setAppliedQuery(query.trim())}
            />
            <Button type="button" onClick={() => setAppliedQuery(query.trim())}>
              <Search size={15} />
              Search
            </Button>
            <IconButton
              type="button"
              onClick={() => {
                setQuery("");
                setAppliedQuery("");
              }}
              title="Clear search"
            >
              <X size={16} />
            </IconButton>
            <IconButton type="button" onClick={() => void load(false)} title="Refresh">
              <RefreshCw size={16} />
            </IconButton>
          </>
        }
      />

      <div className="tasks-layout">
        <div className="task-tab-bar" role="tablist" aria-label="Job lifecycle">
          {(
            [
              ["running", "Active", data?.counts.running],
              ["attention", "Needs attention", data?.counts.attention],
              ["archive", "Archive", data?.counts.archive],
            ] as Array<[PageScope, string, number | undefined]>
          ).map(([value, label, count]) => (
            <button
              key={value}
              type="button"
              className={`task-tab ${scope === value ? "active" : ""}`}
              onClick={() => setScope(value)}
            >
              {label}
              <span>{count ?? 0}</span>
            </button>
          ))}
        </div>

        <section className="task-list-panel">
          {error ? <div className="error-banner">{error}</div> : null}
          {loading ? (
            <EmptyState title="Loading jobs..." />
          ) : jobs.length ? (
            <div className="task-list">
              {jobs.map((job) => (
                <article key={job.job_id} className="task-row">
                  <div className="task-row-icon">
                    <Play size={18} />
                  </div>
                  <div className="task-row-main">
                    <div className="task-row-title">
                      <h3>{job.title || "Background job"}</h3>
                      <div className="task-row-badges">
                        <StatusBadge tone="info">{job.kind}</StatusBadge>
                        <StatusBadge tone={jobStatusTone(job.status)}>{job.status}</StatusBadge>
                      </div>
                    </div>
                    <p>{job.command || job.job_id}</p>
                    {job.last_error ? <p className="task-error-copy">{job.last_error}</p> : null}
                    <div className="chip-list">
                      <span className="data-chip data-chip-wrap">{lifecycleTime(job) || "Unknown time"}</span>
                      <span className="data-chip data-chip-wrap">{jobTarget(job)}</span>
                    </div>
                  </div>
                  {renderActions(job)}
                </article>
              ))}
              {data?.has_more ? (
                <Button type="button" onClick={() => void load(true)}>
                  Load more
                </Button>
              ) : null}
            </div>
          ) : (
            <EmptyState
              title={
                appliedQuery
                  ? `No ${scope} jobs match “${appliedQuery}”`
                  : scope === "running"
                    ? "No active jobs"
                    : scope === "attention"
                      ? "No jobs need attention"
                      : "No archived jobs"
              }
            />
          )}
        </section>
      </div>

      {creating ? (
        <div className="modal-overlay" role="presentation" onClick={() => setCreating(false)}>
          <div className="modal-card" role="dialog" aria-modal="true" onClick={(event) => event.stopPropagation()}>
            <div className="modal-header">
              <h3>New background job</h3>
              <IconButton onClick={() => setCreating(false)} aria-label="Close">
                <X size={16} />
              </IconButton>
            </div>
            <div className="modal-body">
              <label className="task-editor-field-block">
                <span>Title</span>
                <input value={draftTitle} onChange={(event) => setDraftTitle(event.target.value)} placeholder="Optional label" />
              </label>
              <label className="task-editor-field-block">
                <span>Command</span>
                <textarea
                  value={draftCommand}
                  onChange={(event) => setDraftCommand(event.target.value)}
                  placeholder="python3 scripts/long_job.py"
                  rows={5}
                />
              </label>
            </div>
            <div className="modal-footer">
              <Button variant="secondary" onClick={() => setCreating(false)}>
                Cancel
              </Button>
              <Button onClick={() => void onCreate()} disabled={saving || !draftCommand.trim()}>
                Start job
              </Button>
            </div>
          </div>
        </div>
      ) : null}

      <JobDetailsModal job={selectedJob} onClose={() => setSelectedJob(null)} onDelete={(job) => void onDelete(job)} />
    </PageShell>
  );
}

function JobDetailsModal({
  job,
  onClose,
  onDelete,
}: {
  job: BackgroundJobItem | null;
  onClose: () => void;
  onDelete: (job: BackgroundJobItem) => void;
}) {
  if (!job) return null;
  const rows = [
    ["Status", job.status],
    ["Kind", job.kind],
    ["Delivery", jobTarget(job)],
    ["Created", formatStamp(job.created_at)],
    ["Started", formatStamp(job.started_at)],
    ["Updated", formatStamp(job.updated_at)],
    ["Completed", formatStamp(job.completed_at || job.failed_at || job.cancelled_at)],
    ["Error", job.last_error || ""],
  ].filter(([, value]) => value);
  return (
    <div className="modal-overlay" role="presentation" onClick={onClose}>
      <div className="modal-card task-details-modal" role="dialog" aria-modal="true" onClick={(event) => event.stopPropagation()}>
        <div className="modal-header">
          <h3>{job.title || "Background job"}</h3>
          <IconButton onClick={onClose} aria-label="Close details">
            <X size={16} />
          </IconButton>
        </div>
        <div className="modal-body">
          <p className="task-details-content">{job.command}</p>
          <dl className="task-details-grid">
            {rows.map(([label, value]) => (
              <div key={label}>
                <dt>{label}</dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>
          {job.stdout_tail ? (
            <label className="task-editor-field-block">
              <span>Stdout tail</span>
              <pre className="help-code-block">{job.stdout_tail}</pre>
            </label>
          ) : null}
          {job.stderr_tail ? (
            <label className="task-editor-field-block">
              <span>Stderr tail</span>
              <pre className="help-code-block">{job.stderr_tail}</pre>
            </label>
          ) : null}
        </div>
        <div className="modal-footer">
          {job.status === "queued" || job.status === "running" ? null : (
            <Button variant="danger" onClick={() => onDelete(job)}>
              <Trash2 size={15} />
              Delete permanently
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}

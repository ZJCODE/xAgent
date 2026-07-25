import {
  ChevronDown,
  ChevronUp,
  Eye,
  Play,
  Plus,
  RefreshCw,
  RotateCcw,
  Search,
  Square,
  Trash2,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Button,
  EmptyState,
  IconButton,
  PageShell,
  PageToolbar,
  SearchField,
  StatusBadge,
} from "../components/ui";
import { cancelJob, createJob, deleteJob, getJob, getJobs, retryJob } from "../lib/api";
import type { BackgroundJobItem, JobCreateInput, JobScope, JobsResponse } from "../types";

const PAGE_SIZE = 50;
const ACTIVE_STATES = new Set(["queued", "starting", "running", "cancelling"]);

function formatStamp(value?: string | null): string {
  if (!value) return "";
  const date = new Date(value);
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
  if (ACTIVE_STATES.has(status)) return "info";
  if (status === "succeeded") return "good";
  if (status === "failed" || status === "interrupted") return "danger";
  return "muted";
}

function lifecycleTime(job: BackgroundJobItem): string {
  if (job.status === "running") return formatStamp(job.started_at) || "Running now";
  if (job.status === "starting") return formatStamp(job.updated_at) || "Starting";
  if (job.status === "cancelling") return formatStamp(job.updated_at) || "Stopping process";
  if (job.status === "queued") return formatStamp(job.created_at) || "Queued";
  return formatStamp(job.finished_at || job.updated_at);
}

function waitReason(reason?: string | null): string {
  if (!reason) return "";
  if (reason === "waiting_for_worker") return "Waiting for worker";
  if (reason === "concurrency_limit") return "Waiting for an execution slot";
  if (reason.startsWith("waiting_for_resource:")) {
    return `Waiting for ${reason.slice("waiting_for_resource:".length)}`;
  }
  return reason.replaceAll("_", " ");
}

export function JobsPage() {
  const [scope, setScope] = useState<JobScope>("active");
  const [data, setData] = useState<JobsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [appliedQuery, setAppliedQuery] = useState("");
  const [selectedJob, setSelectedJob] = useState<BackgroundJobItem | null>(null);
  const [creating, setCreating] = useState(false);
  const [draftTitle, setDraftTitle] = useState("");
  const [draftCommand, setDraftCommand] = useState("");
  const [draftMode, setDraftMode] = useState<"shell" | "argv">("shell");
  const [draftCwd, setDraftCwd] = useState("");
  const [draftTimeout, setDraftTimeout] = useState("");
  const [draftResources, setDraftResources] = useState("");
  const [advanced, setAdvanced] = useState(false);
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
    if (scope !== "active") return;
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

  const resetDraft = () => {
    setDraftTitle("");
    setDraftCommand("");
    setDraftMode("shell");
    setDraftCwd("");
    setDraftTimeout("");
    setDraftResources("");
    setAdvanced(false);
  };

  const createInput = (): JobCreateInput => {
    const input: JobCreateInput = {
      title: draftTitle.trim() || undefined,
      cwd: draftCwd.trim() || undefined,
      timeout_seconds: draftTimeout ? Number(draftTimeout) : undefined,
      resources: draftResources
        .split(",")
        .map((item) => item.trim())
        .filter(Boolean),
    };
    if (draftMode === "shell") {
      input.command = draftCommand.trim();
      input.shell = true;
      return input;
    }
    const parsed: unknown = JSON.parse(draftCommand);
    if (!Array.isArray(parsed) || !parsed.length || !parsed.every((item) => typeof item === "string")) {
      throw new Error("Argv mode requires a JSON string array, for example [\"python3\", \"script.py\"].");
    }
    input.argv = parsed;
    return input;
  };

  const onCreate = async () => {
    if (!draftCommand.trim()) return;
    setSaving(true);
    setError("");
    try {
      await createJob(createInput());
      setCreating(false);
      resetDraft();
      setScope("active");
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
      const response = await cancelJob(job.job_id);
      if (selectedJob?.job_id === job.job_id) setSelectedJob(response.job);
      await load(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const onRetry = async (job: BackgroundJobItem) => {
    setError("");
    try {
      await retryJob(job.job_id);
      setSelectedJob(null);
      setScope("active");
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
    if (ACTIVE_STATES.has(job.status)) {
      return (
        <div className="task-row-actions">
          <Button className="task-action-button" onClick={() => void openDetails(job)}>
            <Eye size={15} />
            View
          </Button>
          <Button
            className="task-action-button"
            onClick={() => void onCancel(job)}
            disabled={saving || job.status === "cancelling"}
          >
            <Square size={15} />
            {job.status === "cancelling" ? "Cancelling" : "Cancel"}
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
        {job.status === "failed" || job.status === "interrupted" ? (
          <Button className="task-action-button" onClick={() => void onRetry(job)}>
            <RotateCcw size={15} />
            Retry
          </Button>
        ) : null}
        <IconButton variant="danger" onClick={() => void onDelete(job)} title="Delete job">
          <Trash2 size={15} />
        </IconButton>
      </div>
    );
  };

  const workerUnavailable = data?.worker && !data.worker.available;

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

      {workerUnavailable ? (
        <div className="error-banner">
          Job worker is {data?.worker.state || "unavailable"}.
          {data?.worker.last_error ? ` ${data.worker.last_error}` : " Queued work will resume when it can start."}
        </div>
      ) : data?.worker ? (
        <div className="job-worker-strip">
          Worker: {data.worker.state}
          {data.worker.last_heartbeat_at ? ` · heartbeat ${formatStamp(data.worker.last_heartbeat_at)}` : ""}
        </div>
      ) : null}

      <div className="tasks-layout">
        <div className="task-tab-bar" role="tablist" aria-label="Job lifecycle">
          {(
            [
              ["active", "Active", data?.counts.active],
              ["attention", "Needs attention", data?.counts.attention],
              ["history", "History", data?.counts.history],
            ] as Array<[JobScope, string, number | undefined]>
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
                        <StatusBadge tone="info">{job.shell ? "shell" : job.kind}</StatusBadge>
                        <StatusBadge tone={jobStatusTone(job.status)}>{job.status}</StatusBadge>
                      </div>
                    </div>
                    <p>{job.command || job.job_id}</p>
                    {job.last_error ? <p className="task-error-copy">{job.last_error}</p> : null}
                    {job.delivery_warning ? <p className="task-error-copy">Delivery: {job.delivery_warning}</p> : null}
                    <div className="chip-list">
                      <span className="data-chip data-chip-wrap">{lifecycleTime(job) || "Unknown time"}</span>
                      <span className="data-chip data-chip-wrap">{jobTarget(job)}</span>
                      {job.wait_reason ? (
                        <span className="data-chip data-chip-wrap">{waitReason(job.wait_reason)}</span>
                      ) : null}
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
                  : scope === "active"
                    ? "No active jobs"
                    : scope === "attention"
                      ? "No jobs need attention"
                      : "No job history"
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
                <span>{draftMode === "shell" ? "Command" : "Argv JSON"}</span>
                <textarea
                  value={draftCommand}
                  onChange={(event) => setDraftCommand(event.target.value)}
                  placeholder={draftMode === "shell" ? "python3 scripts/long_job.py" : '["python3", "scripts/long_job.py"]'}
                  rows={5}
                />
              </label>
              {draftMode === "shell" ? (
                <p className="wizard-hint">
                  This runs through /bin/sh on this machine. Treat commands and logs as sensitive local data.
                </p>
              ) : null}
              <Button type="button" variant="ghost" onClick={() => setAdvanced((value) => !value)}>
                {advanced ? <ChevronUp size={15} /> : <ChevronDown size={15} />}
                Advanced
              </Button>
              {advanced ? (
                <div className="wizard-grid">
                  <label className="task-editor-field-block">
                    <span>Execution mode</span>
                    <select value={draftMode} onChange={(event) => setDraftMode(event.target.value as "shell" | "argv")}>
                      <option value="shell">Shell command</option>
                      <option value="argv">Argument list (safer)</option>
                    </select>
                  </label>
                  <label className="task-editor-field-block">
                    <span>Working directory</span>
                    <input value={draftCwd} onChange={(event) => setDraftCwd(event.target.value)} placeholder="Inside workspace" />
                  </label>
                  <label className="task-editor-field-block">
                    <span>Timeout seconds</span>
                    <input type="number" min="1" value={draftTimeout} onChange={(event) => setDraftTimeout(event.target.value)} />
                  </label>
                  <label className="task-editor-field-block">
                    <span>Exclusive resources</span>
                    <input value={draftResources} onChange={(event) => setDraftResources(event.target.value)} placeholder="gpu:0, build" />
                  </label>
                </div>
              ) : null}
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

      <JobDetailsModal
        job={selectedJob}
        onClose={() => setSelectedJob(null)}
        onCancel={(job) => void onCancel(job)}
        onRetry={(job) => void onRetry(job)}
        onDelete={(job) => void onDelete(job)}
      />
    </PageShell>
  );
}

function JobDetailsModal({
  job,
  onClose,
  onCancel,
  onRetry,
  onDelete,
}: {
  job: BackgroundJobItem | null;
  onClose: () => void;
  onCancel: (job: BackgroundJobItem) => void;
  onRetry: (job: BackgroundJobItem) => void;
  onDelete: (job: BackgroundJobItem) => void;
}) {
  if (!job) return null;
  const execution = job.execution || {};
  const rows = [
    ["Status", job.status],
    ["Reason", job.reason || ""],
    ["Kind", job.shell ? "process · shell" : "process · argv"],
    ["Delivery", jobTarget(job)],
    ["Wait", waitReason(job.wait_reason)],
    ["Created", formatStamp(job.created_at)],
    ["Started", formatStamp(job.started_at)],
    ["Finished", formatStamp(job.finished_at)],
    ["Attempt", String(execution.attempt_no || "")],
    ["Exit code", execution.exit_code == null ? "" : String(execution.exit_code)],
    ["Signal", execution.signal == null ? "" : String(execution.signal)],
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
          {job.deliveries?.length ? (
            <label className="task-editor-field-block">
              <span>Delivery</span>
              <pre className="help-code-block">{JSON.stringify(job.deliveries, null, 2)}</pre>
            </label>
          ) : null}
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
          {ACTIVE_STATES.has(job.status) ? (
            <Button disabled={job.status === "cancelling"} onClick={() => onCancel(job)}>
              <Square size={15} />
              {job.status === "cancelling" ? "Cancelling" : "Cancel"}
            </Button>
          ) : (
            <>
              {job.status === "failed" || job.status === "interrupted" ? (
                <Button onClick={() => onRetry(job)}>
                  <RotateCcw size={15} />
                  Retry
                </Button>
              ) : null}
              <Button variant="danger" onClick={() => onDelete(job)}>
                <Trash2 size={15} />
                Delete permanently
              </Button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

import {
  Activity,
  Bot,
  CheckCircle2,
  ChevronRight,
  Database,
  FileIcon,
  Files,
  ListTodo,
  MessageSquareText,
  Moon,
  Package,
  Plus,
  Power,
  RefreshCw,
  Save,
  Search,
  Settings,
  Sun,
  Trash2,
  Upload,
  Wifi,
  WifiOff,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { Markdown } from "./components/Markdown";
import { Button, EmptyState, IconButton, StatusBadge } from "./components/ui";
import { ThemeProvider, useTheme } from "./context/ThemeContext";
import {
  consoleWebSocketUrl,
  consoleWorkspaceBlobUrl,
  createConsoleAgent,
  deleteConsoleAgent,
  deleteConsoleTask,
  getConsoleAgents,
  getConsoleChannelLogs,
  getConsoleChannels,
  getConsoleConfig,
  getConsoleIdentity,
  getConsoleMemoryTree,
  getConsoleMessages,
  getConsoleOverview,
  getConsoleSetupSessionEvents,
  getConsoleSkillsInfo,
  getConsoleSkillsTree,
  getConsoleTasks,
  getConsoleWorkspaceTree,
  getHealth,
  previewConsoleConfig,
  readConsoleMemoryFile,
  readConsoleWorkspaceFile,
  runConsoleChannelAction,
  selectConsoleAgent,
  startConsoleSetupSession,
  updateConsoleConfig,
  updateConsoleIdentity,
  uploadConsoleWorkspaceFile,
} from "./lib/api";
import { classNames, formatBytes, formatTimestamp, makeId } from "./lib/format";
import type {
  AttachmentAsset,
  ChatEvent,
  ChatMessage,
  ConfigPreview,
  ConsoleAgentSummary,
  ConsoleChannelState,
  ConsoleCreateAgentInput,
  ConsoleDataTab,
  ConsoleLogResponse,
  ConsoleOverviewResponse,
  ConsoleRouteTab,
  FileNode,
  FileReadResult,
  MessageItem,
  SetupSessionEvent,
  SkillMetadata,
  SkillsInfo,
  TasksResponse,
} from "./types";

const tabs: Array<{ id: ConsoleRouteTab; label: string; icon: ReactNode }> = [
  { id: "overview", label: "Overview", icon: <Activity size={15} /> },
  { id: "chat", label: "Chat", icon: <MessageSquareText size={15} /> },
  { id: "channels", label: "Channels", icon: <Power size={15} /> },
  { id: "config", label: "Config", icon: <Settings size={15} /> },
  { id: "identity", label: "Identity", icon: <Bot size={15} /> },
  { id: "data", label: "Data", icon: <Database size={15} /> },
  { id: "logs", label: "Logs", icon: <FileIcon size={15} /> },
];

const dataTabs: Array<{ id: ConsoleDataTab; label: string; icon: ReactNode }> = [
  { id: "memory", label: "Memory", icon: <Database size={14} /> },
  { id: "messages", label: "Messages", icon: <MessageSquareText size={14} /> },
  { id: "workspace", label: "Workspace", icon: <Files size={14} /> },
  { id: "skills", label: "Skills", icon: <Package size={14} /> },
  { id: "tasks", label: "Tasks", icon: <ListTodo size={14} /> },
];

const defaultCreateForm = {
  name: "",
  title: "",
  makeActive: true,
  provider: "openai",
  model: "gpt-5.4-mini",
  baseUrl: "https://api.openai.com/v1",
  modelApi: "",
  supportsVision: false,
  apiKey: "",
  searchProvider: "none",
  searchApiKey: "",
  imageProvider: "none",
  imageApiKey: "",
  observabilityEnabled: false,
  langfusePublicKey: "",
  langfuseSecretKey: "",
  langfuseBaseUrl: "https://cloud.langfuse.com",
  voiceEnabled: false,
  voiceProvider: "none",
  voiceApiKey: "",
  voiceSttProvider: "soniox",
  voiceSttApiKey: "",
  voiceTtsProvider: "qwen",
  voiceTtsApiKey: "",
  voiceWakeEnabled: false,
  voiceWakePhrases: "xAgent",
  voiceExitPhrases: "exit, stop, goodbye, that's all, never mind",
  voiceInterruptions: false,
  identity: "# Identity\n\nDescribe this agent's role, tone, and behavior here.\n",
  feishuEnabled: false,
  feishuMode: "manual",
  feishuAppId: "",
  feishuAppSecret: "",
  feishuStream: false,
  weixinSetupAfterCreate: false,
};

function normalizeRoute() {
  const parts = window.location.pathname.split("/").filter(Boolean);
  if (parts[0] !== "agents") return { agent: "", tab: "overview" as ConsoleRouteTab };
  const tab = tabs.some((item) => item.id === parts[2]) ? (parts[2] as ConsoleRouteTab) : "overview";
  return { agent: decodeURIComponent(parts[1] || ""), tab };
}

function channelTone(channel?: ConsoleChannelState): "good" | "danger" | "muted" | "info" {
  if (!channel || channel.status === "disabled") return "muted";
  if (channel.status === "running") return "good";
  if (channel.configured) return "info";
  return "muted";
}

function agentApiChannel(agent?: ConsoleAgentSummary | null): ConsoleChannelState | undefined {
  return agent?.channels.find((channel) => channel.channel === "api");
}

function attachmentUrl(agentName: string, attachment: AttachmentAsset): string {
  if (attachment.blob_url) return attachment.blob_url;
  const path = attachment.path || attachment.workspace_path || "";
  return path ? consoleWorkspaceBlobUrl(agentName, path) : "";
}

function isImageAttachment(attachment: AttachmentAsset): boolean {
  return attachment.kind === "image" || Boolean(attachment.mime_type?.startsWith("image/"));
}

function displayError(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function navigateTo(agentName: string, tab: ConsoleRouteTab, setRoute: (route: { agent: string; tab: ConsoleRouteTab }) => void) {
  const path = agentName ? `/agents/${encodeURIComponent(agentName)}/${tab}` : "/agents";
  window.history.pushState(null, "", path);
  setRoute({ agent: agentName, tab });
}

function RuntimeStatus({ health }: { health: "checking" | "online" | "offline" }) {
  return (
    <StatusBadge tone={health === "online" ? "good" : health === "offline" ? "danger" : "muted"}>
      {health === "online" ? <Wifi size={14} /> : <WifiOff size={14} />}
      {health === "checking" ? "Checking" : health === "online" ? "Console online" : "Console offline"}
    </StatusBadge>
  );
}

function AgentsSidebar({
  agents,
  selected,
  query,
  onQuery,
  onSelect,
  onCreate,
  onRefresh,
}: {
  agents: ConsoleAgentSummary[];
  selected: string;
  query: string;
  onQuery: (value: string) => void;
  onSelect: (name: string) => void;
  onCreate: () => void;
  onRefresh: () => void;
}) {
  const filtered = agents.filter((agent) => {
    const text = `${agent.name} ${agent.title} ${agent.model || ""} ${agent.provider || ""}`.toLowerCase();
    return text.includes(query.toLowerCase());
  });

  return (
    <aside className="console-sidebar">
      <div className="brand-block">
        <div className="brand-mark">x</div>
        <div>
          <h1>xAgent</h1>
          <span>Console</span>
        </div>
      </div>
      <div className="agent-sidebar-actions">
        <div className="console-search">
          <Search size={14} />
          <input value={query} placeholder="Search agents" onChange={(event) => onQuery(event.target.value)} />
        </div>
        <IconButton type="button" title="Refresh agents" onClick={onRefresh}>
          <RefreshCw size={15} />
        </IconButton>
        <IconButton type="button" title="Create agent" onClick={onCreate}>
          <Plus size={15} />
        </IconButton>
      </div>
      <div className="agent-list">
        {filtered.length ? filtered.map((agent) => (
          <button
            type="button"
            key={agent.name}
            className={classNames("agent-list-item", selected === agent.name && "active")}
            onClick={() => onSelect(agent.name)}
          >
            <span className="agent-list-title-row">
              <span className="agent-list-title">{agent.title || agent.name}</span>
              {agent.active ? <span className="active-pill">Active</span> : null}
            </span>
            <span className="agent-list-meta">{agent.name} · {agent.provider || "provider"} / {agent.model || "model"}</span>
            <span className="channel-dots" aria-label="Channel status">
              {["api", "voice", "feishu", "weixin"].map((channelName) => {
                const channel = agent.channels.find((item) => item.channel === channelName);
                return <span key={channelName} className={classNames("channel-dot", `dot-${channelTone(channel)}`)} title={`${channelName}: ${channel?.status || "unknown"}`} />;
              })}
            </span>
          </button>
        )) : (
          <EmptyState title="No agents" />
        )}
      </div>
    </aside>
  );
}

function AgentHeader({
  agent,
  route,
  health,
  onTab,
  onRefresh,
  onSetActive,
  onDelete,
  onToggleTheme,
  dark,
}: {
  agent: ConsoleAgentSummary;
  route: ConsoleRouteTab;
  health: "checking" | "online" | "offline";
  onTab: (tab: ConsoleRouteTab) => void;
  onRefresh: () => void;
  onSetActive: () => void;
  onDelete: () => void;
  onToggleTheme: () => void;
  dark: boolean;
}) {
  const api = agentApiChannel(agent);
  return (
    <header className="console-header">
      <div className="agent-title-block">
        <h2>{agent.title || agent.name}</h2>
        <span>{agent.name} · {agent.provider || "provider"} / {agent.model || "model"} · {agent.path}</span>
      </div>
      <div className="console-header-actions">
        <RuntimeStatus health={health} />
        <StatusBadge tone={channelTone(api)}>API {api?.status || "unknown"}</StatusBadge>
        <Button type="button" onClick={onSetActive} disabled={agent.active}>Set Active</Button>
        <IconButton type="button" title="Refresh" onClick={onRefresh}><RefreshCw size={15} /></IconButton>
        <IconButton type="button" title="Toggle theme" onClick={onToggleTheme}>{dark ? <Sun size={16} /> : <Moon size={16} />}</IconButton>
        <IconButton type="button" title="Delete agent" variant="danger" onClick={onDelete}><Trash2 size={15} /></IconButton>
      </div>
      <nav className="console-tabs">
        {tabs.map((tab) => (
          <button key={tab.id} type="button" className={classNames(route === tab.id && "active")} onClick={() => onTab(tab.id)}>
            {tab.icon}
            {tab.label}
          </button>
        ))}
      </nav>
    </header>
  );
}

function OverviewTab({ agentName }: { agentName: string }) {
  const [overview, setOverview] = useState<ConsoleOverviewResponse | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    getConsoleOverview(agentName).then((data) => {
      if (!cancelled) setOverview(data);
    }).catch((err) => !cancelled && setError(displayError(err)));
    return () => {
      cancelled = true;
    };
  }, [agentName]);

  if (error) return <div className="error-strip">{error}</div>;
  if (!overview) return <EmptyState title="Loading overview..." />;

  return (
    <div className="console-scroll">
      <section className="overview-hero">
        <div>
          <h3>{overview.overview.headline}</h3>
          <p>{overview.overview.config_dir}</p>
        </div>
        <StatusBadge tone={overview.overview.initialized ? "good" : "danger"}>
          {overview.overview.initialized ? "Initialized" : "Setup required"}
        </StatusBadge>
      </section>
      <div className="overview-grid">
        {overview.overview.items.map((item) => (
          <section key={item.name} className="console-card">
            <span className="metric-label">{item.name}</span>
            <strong>{item.value}</strong>
            <span>{item.detail || item.status}</span>
          </section>
        ))}
      </div>
      {overview.missing_secrets.length ? (
        <section className="console-card">
          <h3>Missing Secrets</h3>
          <div className="chip-list">
            {overview.missing_secrets.map((secret) => <span className="data-chip" key={secret}>{secret}</span>)}
          </div>
        </section>
      ) : null}
    </div>
  );
}

function ChannelsTab({ agentName, onChanged }: { agentName: string; onChanged: () => void }) {
  const [channels, setChannels] = useState<ConsoleChannelState[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [sessions, setSessions] = useState<Record<string, SetupSessionEvent[]>>({});

  const load = useCallback(async () => {
    setError("");
    try {
      const data = await getConsoleChannels(agentName);
      setChannels(data.channels);
    } catch (err) {
      setError(displayError(err));
    }
  }, [agentName]);

  useEffect(() => {
    void load();
  }, [load]);

  const runAction = async (channel: string, action: "start" | "stop" | "restart") => {
    setBusy(`${channel}-${action}`);
    setError("");
    try {
      await runConsoleChannelAction(agentName, channel, action);
      await load();
      onChanged();
    } catch (err) {
      setError(displayError(err));
      await load();
    } finally {
      setBusy("");
    }
  };

  const startSetup = async (channel: "feishu" | "weixin") => {
    const payload = channel === "feishu"
      ? { kind: "feishu", mode: "one_click" }
      : { kind: "weixin" };
    setError("");
    try {
      const session = await startConsoleSetupSession(agentName, payload);
      setSessions((value) => ({ ...value, [channel]: session.events }));
      const timer = window.setInterval(async () => {
        const events = await getConsoleSetupSessionEvents(session.session_id);
        setSessions((value) => ({ ...value, [channel]: events.events }));
        const last = events.events[events.events.length - 1];
        if (last?.phase === "done" || last?.phase === "error" || last?.phase === "cancelled") {
          window.clearInterval(timer);
          await load();
          onChanged();
        }
      }, 1500);
    } catch (err) {
      setError(displayError(err));
    }
  };

  return (
    <div className="console-scroll">
      <div className="section-toolbar">
        <h3>Channels</h3>
        <Button type="button" onClick={() => void load()}><RefreshCw size={15} />Refresh</Button>
      </div>
      {error ? <div className="error-strip">{error}</div> : null}
      <div className="channel-grid">
        {channels.map((channel) => (
          <section key={channel.channel} className="channel-card">
            <div className="channel-card-head">
              <div>
                <h3>{channel.channel}</h3>
                <span>{channel.target || channel.log_path || "not configured"}</span>
              </div>
              <StatusBadge tone={channelTone(channel)}>{channel.status}</StatusBadge>
            </div>
            <div className="channel-meta">
              <span>Configured: {channel.configured ? "yes" : "no"}</span>
              <span>Enabled: {channel.enabled ? "yes" : "no"}</span>
              <span>PID: {channel.pid || "none"}</span>
            </div>
            <div className="toolbar-actions">
              <Button type="button" onClick={() => void runAction(channel.channel, "start")} disabled={busy !== "" || channel.status === "running"}>
                <Power size={15} />Start
              </Button>
              <Button type="button" onClick={() => void runAction(channel.channel, "stop")} disabled={busy !== "" || channel.status !== "running"}>
                Stop
              </Button>
              <Button type="button" onClick={() => void runAction(channel.channel, "restart")} disabled={busy !== ""}>
                Restart
              </Button>
              {channel.channel === "feishu" && !channel.configured ? <Button type="button" onClick={() => void startSetup("feishu")}>One-click Setup</Button> : null}
              {channel.channel === "weixin" && !channel.configured ? <Button type="button" onClick={() => void startSetup("weixin")}>QR Setup</Button> : null}
            </div>
            {sessions[channel.channel]?.length ? (
              <div className="setup-events">
                {sessions[channel.channel].map((event, index) => (
                  <p key={`${event.phase}-${index}`}>
                    <strong>{event.phase}</strong> {event.message || event.error || ""}
                    {event.qr_url ? <a href={event.qr_url} target="_blank" rel="noreferrer">Open QR</a> : null}
                    {event.auth_url ? <a href={event.auth_url} target="_blank" rel="noreferrer">Authorize</a> : null}
                  </p>
                ))}
              </div>
            ) : null}
          </section>
        ))}
      </div>
    </div>
  );
}

function ChatTab({ agentName, apiChannel, onStartApi }: { agentName: string; apiChannel?: ConsoleChannelState; onStartApi: () => Promise<void> }) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [userId, setUserId] = useState("web_user");
  const [stream, setStream] = useState(true);
  const [attachments, setAttachments] = useState<AttachmentAsset[]>([]);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const node = scrollRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [messages]);

  if (apiChannel?.status !== "running") {
    return (
      <div className="empty-action-panel">
        <EmptyState icon={<MessageSquareText size={24} />} title="API channel required">
          Start this agent's API channel to open Chat.
        </EmptyState>
        <Button type="button" variant="primary" onClick={() => void onStartApi()}>
          <Power size={15} />Start API
        </Button>
      </div>
    );
  }

  const send = async (event?: FormEvent) => {
    event?.preventDefault();
    const text = input.trim();
    if (!text && !attachments.length) return;
    setInput("");
    setSending(true);
    const userMessage: ChatMessage = {
      id: makeId("user"),
      role: "user",
      content: text,
      attachments,
      images: attachments.filter(isImageAttachment).map((item) => attachmentUrl(agentName, item)),
    };
    const assistantId = makeId("assistant");
    setMessages((value) => [...value, userMessage, { id: assistantId, role: "assistant", content: "", pending: true }]);
    setAttachments([]);

    await new Promise<void>((resolve) => {
      const socket = new WebSocket(consoleWebSocketUrl(`/ws/console/agents/${encodeURIComponent(agentName)}/chat`));
      let textBuffer = "";
      socket.addEventListener("open", () => {
        socket.send(JSON.stringify({ user_id: userId, user_message: text, stream, attachments }));
      });
      socket.addEventListener("message", (socketEvent) => {
        let parsed: ChatEvent;
        try {
          parsed = JSON.parse(socketEvent.data) as ChatEvent;
        } catch {
          return;
        }
        if (parsed.type === "error") {
          setMessages((value) => value.map((message) => message.id === assistantId ? { ...message, content: `Error: ${parsed.error}`, pending: false, error: true } : message));
          socket.close();
          resolve();
          return;
        }
        if (parsed.type === "message_delta" && parsed.delta) {
          textBuffer += parsed.delta;
          setMessages((value) => value.map((message) => message.id === assistantId ? { ...message, content: textBuffer } : message));
          return;
        }
        if (parsed.type === "message_done") {
          const doneText = parsed.content == null ? textBuffer : String(parsed.content);
          setMessages((value) => value.map((message) => message.id === assistantId ? {
            ...message,
            content: doneText,
            pending: false,
            attachments: parsed.attachments,
            images: (parsed.attachments || []).filter(isImageAttachment).map((item) => attachmentUrl(agentName, item)),
          } : message));
          return;
        }
        if (parsed.message != null) {
          const content = typeof parsed.message === "string" ? parsed.message : JSON.stringify(parsed.message, null, 2);
          setMessages((value) => value.map((message) => message.id === assistantId ? { ...message, content, pending: false } : message));
        }
        if (parsed.type === "done") {
          socket.close();
          resolve();
        }
      });
      socket.addEventListener("error", () => {
        setMessages((value) => value.map((message) => message.id === assistantId ? { ...message, content: "Error: WebSocket connection failed", pending: false, error: true } : message));
        resolve();
      });
    });
    setSending(false);
  };

  const upload = async (files: FileList | null) => {
    if (!files) return;
    const uploaded = await Promise.all(Array.from(files).map((file) => uploadConsoleWorkspaceFile(agentName, file)));
    setAttachments((value) => [
      ...value,
      ...uploaded.map((file) => ({
        kind: file.mime_type?.startsWith("image/") ? "image" : "file",
        path: file.path,
        workspace_path: file.path,
        blob_url: file.blob_url,
        mime_type: file.mime_type,
        size_bytes: file.size,
        file_name: file.name,
        original_name: file.name,
        source_channel: "web",
      })),
    ]);
  };

  return (
    <section className="console-chat">
      <div className="chat-toolbar">
        <label className="inline-field"><span className="inline-label">User</span><input className="inline-input" value={userId} onChange={(event) => setUserId(event.target.value)} /></label>
        <label className="setting-toggle"><span>Stream</span><input type="checkbox" checked={stream} onChange={(event) => setStream(event.target.checked)} /><span className="toggle-track" /></label>
      </div>
      <div className="console-chat-scroll" ref={scrollRef}>
        {messages.length ? messages.map((message) => (
          <div key={message.id} className={classNames("chat-message-group", message.role === "user" && "from-user")}>
            <div className={classNames("message-bubble", message.role === "user" ? "user-bubble" : "assistant-bubble", message.error && "error-bubble")}>
              <div className="message-label">{message.role === "user" ? "You" : "xAgent"}</div>
              {message.pending && !message.content ? <span>Thinking...</span> : <Markdown content={message.content} renderImages={false} />}
            </div>
            {(message.images || []).map((src, index) => <img key={`${message.id}-${index}`} className="message-image-preview" src={src} alt="" />)}
          </div>
        )) : <EmptyState title="Start a message stream" />}
      </div>
      {attachments.length ? (
        <div className="pending-attachments">
          {attachments.map((attachment, index) => (
            <span className="file-chip" key={`${attachment.path}-${index}`}>
              {attachment.file_name || attachment.path}
              <button type="button" onClick={() => setAttachments((value) => value.filter((_, i) => i !== index))}><X size={13} /></button>
            </span>
          ))}
        </div>
      ) : null}
      <form className="composer-row" onSubmit={send}>
        <textarea
          value={input}
          disabled={sending}
          placeholder="Message selected agent..."
          onChange={(event) => setInput(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              void send();
            }
          }}
        />
        <div className="composer-actions">
          <IconButton type="button" title="Attach files" onClick={() => fileRef.current?.click()}><Upload size={17} /></IconButton>
          <input ref={fileRef} className="hidden" type="file" multiple onChange={(event) => void upload(event.target.files)} />
          <Button type="submit" variant="primary" disabled={sending || (!input.trim() && !attachments.length)}>Send</Button>
        </div>
      </form>
    </section>
  );
}

function IdentityTab({ agentName }: { agentName: string }) {
  const [value, setValue] = useState("");
  const [saved, setSaved] = useState("");
  const [path, setPath] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setError("");
    getConsoleIdentity(agentName).then((data) => {
      setValue(data.identity);
      setSaved(data.identity);
      setPath(data.path);
    }).catch((err) => setError(displayError(err)));
  }, [agentName]);

  const save = async () => {
    setSaving(true);
    setError("");
    try {
      const data = await updateConsoleIdentity(agentName, value);
      setSaved(data.identity);
      setValue(data.identity);
      setPath(data.path);
    } catch (err) {
      setError(displayError(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="editor-page">
      <div className="section-toolbar">
        <div><h3>identity.md</h3><span>{path}</span></div>
        <Button type="button" onClick={save} disabled={saving || value === saved}><Save size={15} />Save</Button>
      </div>
      {error ? <div className="error-strip">{error}</div> : null}
      <textarea className="console-editor" value={value} onChange={(event) => setValue(event.target.value)} />
    </div>
  );
}

function ConfigTab({ agentName }: { agentName: string }) {
  const [text, setText] = useState("");
  const [path, setPath] = useState("");
  const [preview, setPreview] = useState<ConfigPreview | null>(null);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setPreview(null);
    setError("");
    getConsoleConfig(agentName).then((data) => {
      setText(JSON.stringify(data.config, null, 2));
      setPath(data.path);
    }).catch((err) => setError(displayError(err)));
  }, [agentName]);

  const parse = () => JSON.parse(text) as Record<string, unknown>;
  const runPreview = async () => {
    setError("");
    try {
      setPreview(await previewConsoleConfig(agentName, parse()));
    } catch (err) {
      setError(displayError(err));
    }
  };
  const save = async () => {
    setSaving(true);
    setError("");
    try {
      const updated = await updateConsoleConfig(agentName, parse());
      setText(JSON.stringify(updated.config, null, 2));
      setPreview(updated.preview || null);
    } catch (err) {
      setError(displayError(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="editor-page">
      <div className="section-toolbar">
        <div><h3>config.yaml</h3><span>{path}</span></div>
        <div className="toolbar-actions">
          <Button type="button" onClick={runPreview}>Preview</Button>
          <Button type="button" variant="primary" onClick={save} disabled={saving}><Save size={15} />Save</Button>
        </div>
      </div>
      {error ? <div className="error-strip">{error}</div> : null}
      {preview ? (
        <section className={classNames("config-preview", !preview.valid && "invalid")}>
          <strong>{preview.valid ? "Valid config" : "Invalid config"}</strong>
          {preview.errors.map((item) => <p key={item}>{item}</p>)}
          {preview.restart_required_channels.length ? <p>Restart required: {preview.restart_required_channels.join(", ")}</p> : null}
          {preview.changes.slice(0, 8).map((change) => <p key={change.path}>{change.path}: {change.before} → {change.after}</p>)}
        </section>
      ) : null}
      <textarea className="console-editor" value={text} onChange={(event) => setText(event.target.value)} />
    </div>
  );
}

function LogsTab({ agentName }: { agentName: string }) {
  const [channel, setChannel] = useState("api");
  const [log, setLog] = useState<ConsoleLogResponse | null>(null);
  const [error, setError] = useState("");
  const load = useCallback(async () => {
    setError("");
    try {
      setLog(await getConsoleChannelLogs(agentName, channel));
    } catch (err) {
      setError(displayError(err));
    }
  }, [agentName, channel]);
  useEffect(() => {
    void load();
  }, [load]);
  return (
    <div className="editor-page">
      <div className="section-toolbar">
        <div className="segmented-control">
          {["api", "voice", "feishu", "weixin"].map((item) => <button type="button" key={item} className={item === channel ? "active" : ""} onClick={() => setChannel(item)}>{item}</button>)}
        </div>
        <Button type="button" onClick={() => void load()}><RefreshCw size={15} />Refresh</Button>
      </div>
      {error ? <div className="error-strip">{error}</div> : null}
      <pre className="full-log">{log?.content || "No log output."}</pre>
    </div>
  );
}

function DataTab({ agentName }: { agentName: string }) {
  const [tab, setTab] = useState<ConsoleDataTab>("memory");
  return (
    <div className="data-page">
      <div className="data-tabs">
        {dataTabs.map((item) => <button key={item.id} type="button" className={tab === item.id ? "active" : ""} onClick={() => setTab(item.id)}>{item.icon}{item.label}</button>)}
      </div>
      {tab === "memory" ? <MemoryData agentName={agentName} /> : null}
      {tab === "messages" ? <MessagesData agentName={agentName} /> : null}
      {tab === "workspace" ? <WorkspaceData agentName={agentName} /> : null}
      {tab === "skills" ? <SkillsData agentName={agentName} /> : null}
      {tab === "tasks" ? <TasksData agentName={agentName} /> : null}
    </div>
  );
}

function TreeList({ nodes, onSelect }: { nodes: FileNode[]; onSelect: (node: FileNode) => void }) {
  return (
    <div className="compact-tree">
      {nodes.map((node) => (
        <div key={node.path}>
          <button type="button" onClick={() => node.type === "file" ? onSelect(node) : undefined}>
            <ChevronRight size={13} />{node.name}
          </button>
          {node.children?.length ? <TreeList nodes={node.children} onSelect={onSelect} /> : null}
        </div>
      ))}
    </div>
  );
}

function MemoryData({ agentName }: { agentName: string }) {
  const [tree, setTree] = useState<FileNode[]>([]);
  const [selected, setSelected] = useState<FileReadResult | null>(null);
  useEffect(() => { getConsoleMemoryTree(agentName).then((data) => setTree(data.tree)); }, [agentName]);
  return <BrowserDataLayout tree={<TreeList nodes={tree} onSelect={(node) => readConsoleMemoryFile(agentName, node.path).then(setSelected)} />} content={selected ? <Markdown content={selected.content} /> : <EmptyState title="Select memory file" />} />;
}

function WorkspaceData({ agentName }: { agentName: string }) {
  const [tree, setTree] = useState<FileNode[]>([]);
  const [selected, setSelected] = useState<FileReadResult | null>(null);
  useEffect(() => { getConsoleWorkspaceTree(agentName).then((data) => setTree(data.tree)); }, [agentName]);
  return <BrowserDataLayout tree={<TreeList nodes={tree} onSelect={(node) => readConsoleWorkspaceFile(agentName, node.path).then(setSelected)} />} content={selected ? selected.text ? <pre className="file-content">{selected.content}</pre> : <a href={selected.blob_url} target="_blank" rel="noreferrer">Open binary file</a> : <EmptyState title="Select workspace file" />} />;
}

function MessagesData({ agentName }: { agentName: string }) {
  const [messages, setMessages] = useState<MessageItem[]>([]);
  useEffect(() => { getConsoleMessages(agentName, 100, 0).then((data) => setMessages(data.messages)); }, [agentName]);
  return <div className="console-scroll">{messages.length ? messages.map((message, index) => <section className="console-card" key={`${message.timestamp}-${index}`}><strong>{message.role}</strong><p>{message.content}</p></section>) : <EmptyState title="No messages" />}</div>;
}

function SkillsData({ agentName }: { agentName: string }) {
  const [info, setInfo] = useState<SkillsInfo | null>(null);
  const [skills, setSkills] = useState<SkillMetadata[]>([]);
  useEffect(() => {
    void Promise.all([getConsoleSkillsInfo(agentName), getConsoleSkillsTree(agentName)]).then(([infoData, treeData]) => {
      setInfo(infoData);
      setSkills(treeData.skills || []);
    });
  }, [agentName]);
  return <div className="console-scroll"><div className="chip-list"><StatusBadge tone="good">{info?.enabled_count || 0} enabled</StatusBadge><StatusBadge tone="muted">{info?.disabled_count || 0} disabled</StatusBadge></div>{skills.map((skill) => <section className="console-card" key={skill.path}><strong>{skill.name}</strong><p>{skill.description}</p><div className="chip-list"><StatusBadge tone={skill.enabled ? "good" : "muted"}>{skill.enabled ? "enabled" : "disabled"}</StatusBadge><StatusBadge tone={skill.valid ? "good" : "danger"}>{skill.valid ? "valid" : "invalid"}</StatusBadge></div></section>)}</div>;
}

function TasksData({ agentName }: { agentName: string }) {
  const [data, setData] = useState<TasksResponse | null>(null);
  const load = useCallback(() => getConsoleTasks(agentName).then(setData), [agentName]);
  useEffect(() => { void load(); }, [load]);
  const remove = async (taskId: string) => { await deleteConsoleTask(agentName, taskId); await load(); };
  return <div className="console-scroll">{data?.tasks.length ? data.tasks.map((task) => <section className="console-card task-card" key={task.task_id}><div><strong>{task.title || task.task_id}</strong><p>{task.content}</p><span>{task.next_run_at}</span></div><Button type="button" variant="danger" onClick={() => void remove(task.task_id)}><Trash2 size={14} />Delete</Button></section>) : <EmptyState title="No tasks" />}</div>;
}

function BrowserDataLayout({ tree, content }: { tree: ReactNode; content: ReactNode }) {
  return <div className="data-browser"><aside>{tree}</aside><main>{content}</main></div>;
}

type CreateFormState = typeof defaultCreateForm;
type CreateStepId = "profile" | "model" | "tools" | "identity" | "channels";

const createSteps: Array<{ id: CreateStepId; label: string; icon: ReactNode }> = [
  { id: "profile", label: "Profile", icon: <Bot size={15} /> },
  { id: "model", label: "Model", icon: <Settings size={15} /> },
  { id: "tools", label: "Tools", icon: <Package size={15} /> },
  { id: "identity", label: "Identity", icon: <FileIcon size={15} /> },
  { id: "channels", label: "Channels", icon: <Power size={15} /> },
];

const providerPresets: Array<{
  id: string;
  label: string;
  model: string;
  baseUrl: string;
  modelApi?: string;
  supportsVision?: boolean;
}> = [
  { id: "openai", label: "OpenAI", model: "gpt-5.4-mini", baseUrl: "https://api.openai.com/v1", supportsVision: true },
  { id: "deepseek", label: "DeepSeek", model: "deepseek-v4-pro", baseUrl: "https://api.deepseek.com", supportsVision: false },
  { id: "qwen", label: "Qwen", model: "qwen3.6-flash", baseUrl: "https://dashscope.aliyuncs.com/compatible-mode/v1", supportsVision: true },
  { id: "minimax", label: "MiniMax", model: "MiniMax-M2.7", baseUrl: "https://api.minimax.io/v1", supportsVision: false },
  { id: "anthropic", label: "Anthropic", model: "claude-sonnet-4-20250514", baseUrl: "https://api.anthropic.com", modelApi: "anthropic_messages", supportsVision: true },
  { id: "custom", label: "Custom", model: "custom-model", baseUrl: "https://api.example.com/v1", modelApi: "openai_chat_completions", supportsVision: false },
];

const identityTemplates = [
  {
    id: "general",
    label: "General",
    title: "General Agent",
    identity: "# Identity\n\nYou are a practical xAgent that helps with planning, research, writing, and operations.\n",
  },
  {
    id: "operator",
    label: "Operator",
    title: "Ops Agent",
    identity: "# Identity\n\nYou are an operations-focused xAgent. Be concise, track state carefully, and surface blockers early.\n",
  },
  {
    id: "builder",
    label: "Builder",
    title: "Builder Agent",
    identity: "# Identity\n\nYou are a builder-focused xAgent. Make concrete changes, verify work, and keep implementation notes precise.\n",
  },
];

function createAgentPayload(form: CreateFormState): ConsoleCreateAgentInput {
  return {
    name: form.name.trim(),
    title: form.title.trim(),
    make_active: form.makeActive,
    identity: form.identity,
    model: {
      provider: form.provider,
      model: form.model,
      base_url: form.baseUrl,
      model_api: form.modelApi,
      supports_vision: form.supportsVision,
      api_key: form.apiKey,
    },
    capabilities: {
      search_provider: form.searchProvider,
      search_api_key: form.searchApiKey,
      image_generation_provider: form.imageProvider,
      image_generation_api_key: form.imageApiKey,
      observability_enabled: form.observabilityEnabled,
      langfuse_public_key: form.langfusePublicKey,
      langfuse_secret_key: form.langfuseSecretKey,
      langfuse_base_url: form.langfuseBaseUrl,
    },
    voice: {
      enabled: form.voiceEnabled,
      provider: form.voiceProvider,
      api_key: form.voiceApiKey,
      stt_provider: form.voiceSttProvider,
      stt_api_key: form.voiceSttApiKey,
      tts_provider: form.voiceTtsProvider,
      tts_api_key: form.voiceTtsApiKey,
      wake_enabled: form.voiceWakeEnabled,
      wake_phrases: form.voiceWakePhrases.split(",").map((item) => item.trim()).filter(Boolean),
      exit_phrases: form.voiceExitPhrases.split(",").map((item) => item.trim()).filter(Boolean),
      enable_interruptions: form.voiceInterruptions,
    },
    feishu: {
      enabled: form.feishuEnabled,
      mode: form.feishuMode,
      app_id: form.feishuAppId,
      app_secret: form.feishuAppSecret,
      stream: form.feishuStream,
    },
  };
}

function createStepIssues(form: CreateFormState, step: CreateStepId): string[] {
  const issues: string[] = [];
  if (step === "profile") {
    const name = form.name.trim();
    if (!name) issues.push("Agent name is required.");
    if (name && !/^[a-z][a-z0-9_-]*$/.test(name)) {
      issues.push("Agent name must start with a lowercase letter and only use lowercase letters, numbers, hyphens, or underscores.");
    }
  }
  if (step === "model") {
    if (!form.model.trim()) issues.push("Model is required.");
    if (!form.baseUrl.trim()) issues.push("Base URL is required.");
    if (form.provider === "custom" && !form.modelApi.trim()) issues.push("Custom provider requires a model API.");
  }
  if (step === "identity" && !form.identity.trim()) {
    issues.push("Identity cannot be empty.");
  }
  if (step === "channels" && form.feishuEnabled && form.feishuMode === "manual") {
    if (!form.feishuAppId.trim()) issues.push("Feishu App ID is required for manual setup.");
    if (!form.feishuAppSecret.trim()) issues.push("Feishu App Secret is required for manual setup.");
  }
  if (step === "tools" && form.voiceEnabled && form.voiceProvider === "none") {
    issues.push("Choose a voice provider or turn voice off.");
  }
  return issues;
}

function createBlockingIssues(form: CreateFormState): string[] {
  return createSteps.flatMap((step) => createStepIssues(form, step.id));
}

function createMissingSecrets(form: CreateFormState): string[] {
  const missing: string[] = [];
  if (!form.apiKey.trim()) missing.push("Model API key");
  if (form.searchProvider !== "none" && !form.searchApiKey.trim() && form.searchProvider !== form.provider) {
    missing.push("Search API key");
  }
  if (form.imageProvider !== "none" && !form.imageApiKey.trim() && form.imageProvider !== form.provider) {
    missing.push("Image API key");
  }
  if (form.observabilityEnabled) {
    if (!form.langfusePublicKey.trim()) missing.push("Langfuse public key");
    if (!form.langfuseSecretKey.trim()) missing.push("Langfuse secret key");
  }
  if (form.voiceEnabled && form.voiceProvider !== "none") {
    if (form.voiceProvider === "custom") {
      if (!form.voiceSttApiKey.trim()) missing.push("Voice STT key");
      if (!form.voiceTtsApiKey.trim()) missing.push("Voice TTS key");
    } else if (!form.voiceApiKey.trim()) {
      missing.push("Voice API key");
    }
  }
  return missing;
}

function agentDirectoryPreview(name: string): string {
  return `~/.xagent/agents/${name.trim() || "<name>"}`;
}

function FormGrid({ fields }: { fields: Array<[string, ReactNode]> }) {
  return <div className="form-grid">{fields.map(([label, control]) => <label key={label} className="form-field"><span>{label}</span>{control}</label>)}</div>;
}

function WizardSection({ title, children, className }: { title: string; children: ReactNode; className?: string }) {
  return (
    <section className={classNames("wizard-section", className)}>
      <h3>{title}</h3>
      {children}
    </section>
  );
}

function ToggleSetting({
  label,
  checked,
  onChange,
}: {
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <label className="wizard-toggle-row">
      <span>{label}</span>
      <input type="checkbox" checked={checked} onChange={(event) => onChange(event.target.checked)} />
    </label>
  );
}

function CreateAgentModal({ onClose, onCreated }: { onClose: () => void; onCreated: (agentName: string) => void }) {
  const [form, setForm] = useState<CreateFormState>(defaultCreateForm);
  const [step, setStep] = useState(0);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const currentStep = createSteps[step];
  const blockingIssues = createBlockingIssues(form);
  const currentIssues = createStepIssues(form, currentStep.id);
  const missingSecrets = createMissingSecrets(form);
  const enabledChannels = ["api", form.feishuEnabled ? "feishu" : "", form.weixinSetupAfterCreate ? "weixin" : ""].filter(Boolean);

  const update = (key: keyof CreateFormState, value: string | boolean) => {
    setForm((current) => ({ ...current, [key]: value }));
    setError("");
  };
  const updateMany = (values: Partial<CreateFormState>) => {
    setForm((current) => ({ ...current, ...values }));
    setError("");
  };
  const selectProvider = (provider: string) => {
    const preset = providerPresets.find((item) => item.id === provider);
    updateMany({
      provider,
      model: preset?.model || form.model,
      baseUrl: preset?.baseUrl || form.baseUrl,
      modelApi: preset?.modelApi || "",
      supportsVision: Boolean(preset?.supportsVision),
    });
  };
  const applyIdentityTemplate = (template: typeof identityTemplates[number]) => {
    updateMany({
      title: form.title.trim() ? form.title : template.title,
      identity: template.identity,
    });
  };

  const next = () => {
    if (currentIssues.length) {
      setError(currentIssues[0]);
      return;
    }
    setError("");
    setStep((value) => Math.min(createSteps.length - 1, value + 1));
  };

  const submit = async () => {
    if (blockingIssues.length) {
      setError(blockingIssues[0]);
      const invalidStep = createSteps.findIndex((item) => createStepIssues(form, item.id).length > 0);
      if (invalidStep >= 0) setStep(invalidStep);
      return;
    }
    setSaving(true);
    setError("");
    try {
      await createConsoleAgent(createAgentPayload(form));
      if (form.feishuEnabled && form.feishuMode === "one_click") {
        await startConsoleSetupSession(form.name.trim(), {
          kind: "feishu",
          mode: "one_click",
          stream: form.feishuStream,
        });
      }
      if (form.weixinSetupAfterCreate) {
        await startConsoleSetupSession(form.name.trim(), { kind: "weixin" });
      }
      onCreated(form.name.trim());
    } catch (err) {
      setError(displayError(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="modal-backdrop">
      <section className="create-modal create-modal-redesigned">
        <header className="create-modal-header">
          <div>
            <h2>Create Agent</h2>
            <span>{form.name.trim() || "new agent"} · {currentStep.label}</span>
          </div>
          <IconButton type="button" onClick={onClose} title="Close"><X size={16} /></IconButton>
        </header>

        <div className="create-wizard-layout">
          <aside className="create-step-rail">
            {createSteps.map((item, index) => {
              const issues = createStepIssues(form, item.id);
              return (
                <button
                  type="button"
                  key={item.id}
                  className={classNames("create-step-button", index === step && "active", issues.length > 0 && "has-issues")}
                  onClick={() => setStep(index)}
                >
                  {item.icon}
                  <span>{item.label}</span>
                  {issues.length ? <StatusBadge tone="danger">!</StatusBadge> : index < step ? <CheckCircle2 size={14} /> : null}
                </button>
              );
            })}
          </aside>

          <main className="create-step-panel">
            {error ? <div className="error-strip">{error}</div> : null}
            {currentStep.id === "profile" ? (
              <div className="wizard-stack">
                <WizardSection title="Profile">
                  <FormGrid fields={[
                    ["Name", <input value={form.name} placeholder="work" autoFocus onChange={(event) => update("name", event.target.value)} />],
                    ["Title", <input value={form.title} placeholder="Work Agent" onChange={(event) => update("title", event.target.value)} />],
                  ]} />
                  <ToggleSetting label="Make active" checked={form.makeActive} onChange={(value) => update("makeActive", value)} />
                </WizardSection>
                <WizardSection title="Template">
                  <div className="template-grid">
                    {identityTemplates.map((template) => (
                      <button type="button" key={template.id} onClick={() => applyIdentityTemplate(template)}>
                        <strong>{template.label}</strong>
                        <span>{template.title}</span>
                      </button>
                    ))}
                  </div>
                </WizardSection>
              </div>
            ) : null}

            {currentStep.id === "model" ? (
              <div className="wizard-stack">
                <WizardSection title="Provider">
                  <div className="provider-grid">
                    {providerPresets.map((preset) => (
                      <button
                        type="button"
                        key={preset.id}
                        className={classNames(form.provider === preset.id && "active")}
                        onClick={() => selectProvider(preset.id)}
                      >
                        <strong>{preset.label}</strong>
                        <span>{preset.model}</span>
                      </button>
                    ))}
                  </div>
                </WizardSection>
                <WizardSection title="Model">
                  <FormGrid fields={[
                    ["Model", <input value={form.model} onChange={(event) => update("model", event.target.value)} />],
                    ["Base URL", <input value={form.baseUrl} onChange={(event) => update("baseUrl", event.target.value)} />],
                    ["API key", <input type="password" value={form.apiKey} placeholder="optional" onChange={(event) => update("apiKey", event.target.value)} />],
                    ["Vision", <input type="checkbox" checked={form.supportsVision} onChange={(event) => update("supportsVision", event.target.checked)} />],
                    ...(form.provider === "custom" ? [["Model API", <input value={form.modelApi} onChange={(event) => update("modelApi", event.target.value)} />] as [string, ReactNode]] : []),
                  ]} />
                </WizardSection>
              </div>
            ) : null}

            {currentStep.id === "tools" ? (
              <div className="wizard-stack">
                <WizardSection title="Capabilities">
                  <FormGrid fields={[
                    ["Search", <select value={form.searchProvider} onChange={(event) => update("searchProvider", event.target.value)}><option>none</option><option>openai</option><option>qwen</option><option>minimax</option></select>],
                    ["Image", <select value={form.imageProvider} onChange={(event) => update("imageProvider", event.target.value)}><option>none</option><option>openai</option><option>minimax</option><option>qwen</option></select>],
                    ...(form.searchProvider !== "none" ? [["Search key", <input type="password" value={form.searchApiKey} placeholder="optional" onChange={(event) => update("searchApiKey", event.target.value)} />] as [string, ReactNode]] : []),
                    ...(form.imageProvider !== "none" ? [["Image key", <input type="password" value={form.imageApiKey} placeholder="optional" onChange={(event) => update("imageApiKey", event.target.value)} />] as [string, ReactNode]] : []),
                  ]} />
                  <ToggleSetting label="Observability" checked={form.observabilityEnabled} onChange={(value) => update("observabilityEnabled", value)} />
                  {form.observabilityEnabled ? (
                    <FormGrid fields={[
                      ["Langfuse public", <input value={form.langfusePublicKey} onChange={(event) => update("langfusePublicKey", event.target.value)} />],
                      ["Langfuse secret", <input type="password" value={form.langfuseSecretKey} onChange={(event) => update("langfuseSecretKey", event.target.value)} />],
                      ["Langfuse URL", <input value={form.langfuseBaseUrl} onChange={(event) => update("langfuseBaseUrl", event.target.value)} />],
                    ]} />
                  ) : null}
                </WizardSection>
                <WizardSection title="Voice">
                  <ToggleSetting label="Enable voice" checked={form.voiceEnabled} onChange={(value) => updateMany({ voiceEnabled: value, voiceProvider: value ? "soniox" : "none" })} />
                  {form.voiceEnabled ? (
                    <>
                      <FormGrid fields={[
                        ["Provider", <select value={form.voiceProvider} onChange={(event) => update("voiceProvider", event.target.value)}><option>soniox</option><option>qwen</option><option>custom</option></select>],
                        ...(form.voiceProvider === "custom"
                          ? [
                              ["STT provider", <select value={form.voiceSttProvider} onChange={(event) => update("voiceSttProvider", event.target.value)}><option>soniox</option><option>qwen</option></select>] as [string, ReactNode],
                              ["STT key", <input type="password" value={form.voiceSttApiKey} onChange={(event) => update("voiceSttApiKey", event.target.value)} />] as [string, ReactNode],
                              ["TTS provider", <select value={form.voiceTtsProvider} onChange={(event) => update("voiceTtsProvider", event.target.value)}><option>soniox</option><option>qwen</option></select>] as [string, ReactNode],
                              ["TTS key", <input type="password" value={form.voiceTtsApiKey} onChange={(event) => update("voiceTtsApiKey", event.target.value)} />] as [string, ReactNode],
                            ]
                          : [["Voice key", <input type="password" value={form.voiceApiKey} onChange={(event) => update("voiceApiKey", event.target.value)} />] as [string, ReactNode]]),
                      ]} />
                      <div className="wizard-toggle-grid">
                        <ToggleSetting label="Wake phrases" checked={form.voiceWakeEnabled} onChange={(value) => update("voiceWakeEnabled", value)} />
                        <ToggleSetting label="Interruptions" checked={form.voiceInterruptions} onChange={(value) => update("voiceInterruptions", value)} />
                      </div>
                      {form.voiceWakeEnabled ? (
                        <FormGrid fields={[
                          ["Wake phrases", <input value={form.voiceWakePhrases} onChange={(event) => update("voiceWakePhrases", event.target.value)} />],
                          ["Exit phrases", <input value={form.voiceExitPhrases} onChange={(event) => update("voiceExitPhrases", event.target.value)} />],
                        ]} />
                      ) : null}
                    </>
                  ) : null}
                </WizardSection>
              </div>
            ) : null}

            {currentStep.id === "identity" ? (
              <div className="identity-builder">
                <textarea className="wizard-identity" value={form.identity} onChange={(event) => update("identity", event.target.value)} />
                <div className="identity-preview">
                  <Markdown content={form.identity || "# Identity"} />
                </div>
              </div>
            ) : null}

            {currentStep.id === "channels" ? (
              <div className="wizard-stack">
                <WizardSection title="API">
                  <div className="channel-review-row">
                    <StatusBadge tone="good">enabled</StatusBadge>
                    <span>127.0.0.1:8011+</span>
                  </div>
                </WizardSection>
                <WizardSection title="Feishu">
                  <ToggleSetting label="Enable Feishu" checked={form.feishuEnabled} onChange={(value) => update("feishuEnabled", value)} />
                  {form.feishuEnabled ? (
                    <>
                      <div className="segmented-control wizard-segment">
                        {["manual", "one_click"].map((mode) => (
                          <button type="button" key={mode} className={form.feishuMode === mode ? "active" : ""} onClick={() => update("feishuMode", mode)}>
                            {mode === "manual" ? "Manual" : "One-click"}
                          </button>
                        ))}
                      </div>
                      {form.feishuMode === "manual" ? (
                        <FormGrid fields={[
                          ["App ID", <input value={form.feishuAppId} onChange={(event) => update("feishuAppId", event.target.value)} />],
                          ["App Secret", <input type="password" value={form.feishuAppSecret} onChange={(event) => update("feishuAppSecret", event.target.value)} />],
                        ]} />
                      ) : null}
                      <ToggleSetting label="Streaming cards" checked={form.feishuStream} onChange={(value) => update("feishuStream", value)} />
                    </>
                  ) : null}
                </WizardSection>
                <WizardSection title="Weixin">
                  <ToggleSetting label="QR setup after create" checked={form.weixinSetupAfterCreate} onChange={(value) => update("weixinSetupAfterCreate", value)} />
                </WizardSection>
                <WizardSection title="Review">
                  <div className="review-grid">
                    <span>Name</span><strong>{form.name.trim() || "not set"}</strong>
                    <span>Directory</span><strong>{agentDirectoryPreview(form.name)}</strong>
                    <span>Model</span><strong>{form.provider} / {form.model || "not set"}</strong>
                    <span>Channels</span><strong>{enabledChannels.join(", ")}</strong>
                  </div>
                </WizardSection>
              </div>
            ) : null}
          </main>

          <aside className="create-summary-panel">
            <h3>Review</h3>
            <div className="review-grid">
              <span>Name</span><strong>{form.name.trim() || "not set"}</strong>
              <span>Path</span><strong>{agentDirectoryPreview(form.name)}</strong>
              <span>Model</span><strong>{form.provider} / {form.model || "not set"}</strong>
              <span>Channels</span><strong>{enabledChannels.join(", ")}</strong>
            </div>
            <div className="summary-chip-stack">
              {blockingIssues.length ? <StatusBadge tone="danger">{blockingIssues.length} blocking</StatusBadge> : <StatusBadge tone="good">ready</StatusBadge>}
              {missingSecrets.length ? <StatusBadge tone="info">{missingSecrets.length} secrets later</StatusBadge> : null}
            </div>
            {blockingIssues.length ? (
              <div className="summary-list">
                {blockingIssues.slice(0, 4).map((issue) => <p key={issue}>{issue}</p>)}
              </div>
            ) : null}
            {missingSecrets.length ? (
              <div className="summary-list muted">
                {missingSecrets.slice(0, 5).map((secret) => <p key={secret}>{secret}</p>)}
              </div>
            ) : null}
          </aside>
        </div>

        <footer>
          <Button type="button" onClick={() => setStep((value) => Math.max(0, value - 1))} disabled={step === 0}>Back</Button>
          {step < createSteps.length - 1 ? (
            <Button type="button" variant="primary" onClick={next}>Next</Button>
          ) : (
            <Button type="button" variant="primary" disabled={saving || blockingIssues.length > 0} onClick={submit}>
              <CheckCircle2 size={15} />{saving ? "Creating..." : "Create"}
            </Button>
          )}
        </footer>
      </section>
    </div>
  );
}

function ConsoleApp() {
  const { dark, toggleTheme } = useTheme();
  const [route, setRoute] = useState(() => normalizeRoute());
  const [agents, setAgents] = useState<ConsoleAgentSummary[]>([]);
  const [query, setQuery] = useState("");
  const [health, setHealth] = useState<"checking" | "online" | "offline">("checking");
  const [error, setError] = useState("");
  const [createOpen, setCreateOpen] = useState(false);

  const selectedAgent = useMemo(() => agents.find((agent) => agent.name === route.agent) || agents[0] || null, [agents, route.agent]);

  const loadAgents = useCallback(async () => {
    setError("");
    try {
      const data = await getConsoleAgents();
      setAgents(data.agents);
      const target = route.agent && data.agents.some((agent) => agent.name === route.agent)
        ? route.agent
        : data.active_agent || data.agents[0]?.name || "";
      if (target !== route.agent) navigateTo(target, route.tab, setRoute);
    } catch (err) {
      setError(displayError(err));
    }
  }, [route.agent, route.tab]);

  useEffect(() => {
    void loadAgents();
  }, [loadAgents]);

  useEffect(() => {
    getHealth().then(() => setHealth("online")).catch(() => setHealth("offline"));
    const onPop = () => setRoute(normalizeRoute());
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const startApi = async () => {
    if (!selectedAgent) return;
    await runConsoleChannelAction(selectedAgent.name, "api", "start");
    await loadAgents();
  };

  const deleteAgent = async () => {
    if (!selectedAgent) return;
    const answer = window.prompt(`Type ${selectedAgent.name} to delete this agent and all local data.`);
    if (answer !== selectedAgent.name) return;
    try {
      await deleteConsoleAgent(selectedAgent.name, false);
    } catch (err) {
      if (!window.confirm(`${displayError(err)}\nStop running channels and delete?`)) return;
      await deleteConsoleAgent(selectedAgent.name, true);
    }
    await loadAgents();
  };

  return (
    <div className="console-shell">
      <AgentsSidebar
        agents={agents}
        selected={selectedAgent?.name || ""}
        query={query}
        onQuery={setQuery}
        onSelect={(name) => navigateTo(name, route.tab, setRoute)}
        onCreate={() => setCreateOpen(true)}
        onRefresh={() => void loadAgents()}
      />
      <main className="console-main">
        {selectedAgent ? (
          <>
            <AgentHeader
              agent={selectedAgent}
              route={route.tab}
              health={health}
              dark={dark}
              onToggleTheme={toggleTheme}
              onRefresh={() => void loadAgents()}
              onSetActive={async () => { await selectConsoleAgent(selectedAgent.name); await loadAgents(); }}
              onDelete={() => void deleteAgent()}
              onTab={(tab) => navigateTo(selectedAgent.name, tab, setRoute)}
            />
            {error ? <div className="error-strip">{error}</div> : null}
            <section className="console-workspace">
              {route.tab === "overview" ? <OverviewTab agentName={selectedAgent.name} /> : null}
              {route.tab === "channels" ? <ChannelsTab agentName={selectedAgent.name} onChanged={() => void loadAgents()} /> : null}
              {route.tab === "chat" ? <ChatTab agentName={selectedAgent.name} apiChannel={agentApiChannel(selectedAgent)} onStartApi={startApi} /> : null}
              {route.tab === "identity" ? <IdentityTab agentName={selectedAgent.name} /> : null}
              {route.tab === "config" ? <ConfigTab agentName={selectedAgent.name} /> : null}
              {route.tab === "data" ? <DataTab agentName={selectedAgent.name} /> : null}
              {route.tab === "logs" ? <LogsTab agentName={selectedAgent.name} /> : null}
            </section>
          </>
        ) : (
          <div className="empty-action-panel">
            <EmptyState icon={<Bot size={24} />} title="Create your first agent">
              The Console manages every local xAgent from one place.
            </EmptyState>
            <Button type="button" variant="primary" onClick={() => setCreateOpen(true)}><Plus size={15} />Create Agent</Button>
          </div>
        )}
      </main>
      {createOpen ? <CreateAgentModal onClose={() => setCreateOpen(false)} onCreated={(name) => { setCreateOpen(false); void loadAgents().then(() => navigateTo(name, "overview", setRoute)); }} /> : null}
    </div>
  );
}

export default function App() {
  return (
    <ThemeProvider>
      <ConsoleApp />
    </ThemeProvider>
  );
}

const { spawn } = require("child_process");
const fs = require("fs");
const http = require("http");
const https = require("https");
const os = require("os");
const path = require("path");

const DEFAULT_POLL_MS = 500;
const DEFAULT_TIMEOUT_MS = 6000;
const DEFAULT_INITIAL_PROBE_MS = 2500;
const INSTALL_SCRIPT_URL = "https://raw.githubusercontent.com/ZJCODE/xagent/main/install.sh";
const INSTALL_COMMAND = `curl -fsSL ${INSTALL_SCRIPT_URL} | bash`;
const LOOPBACK_NO_PROXY_HOSTS = ["localhost", "127.0.0.1", "::1", "0.0.0.0"];

const BOOTSTRAP_STATUS = {
  CHECK_ENV: { title: "Checking environment", detail: "Looking for the xAgent backend" },
  CHECK_WEB: { title: "Connecting to local service", detail: "Checking the web UI at 127.0.0.1:1415" },
  FIND_XAGENT: { title: "Locating xAgent", detail: "Reading the recorded CLI command" },
  START_WEB: { title: "Starting web service", detail: "First launch may take a few seconds" },
  WAIT_WEB: { title: "Waiting for web service", detail: "Loading the management UI" },
  ENTERING: { title: "Opening xAgent", detail: "Loading the main window" },
  INSTALLING: { title: "Installing backend", detail: "Downloading and configuring xAgent. Keep this window open." },
};

function normalizeStatus(status) {
  if (typeof status === "string") {
    return { title: status, detail: "" };
  }
  return {
    title: status?.title || "Starting xAgent...",
    detail: status?.detail || "",
  };
}

function mergeNoProxy(value) {
  const seen = new Set();
  const entries = String(value || "")
    .split(",")
    .map((entry) => entry.trim())
    .filter(Boolean);
  return [...entries, ...LOOPBACK_NO_PROXY_HOSTS]
    .filter((entry) => {
      const key = entry.toLowerCase();
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .join(",");
}

function applyLoopbackNoProxyEnv(env = process.env) {
  const noProxy = mergeNoProxy(env.NO_PROXY || env.no_proxy || "");
  env.NO_PROXY = noProxy;
  env.no_proxy = noProxy;
  return env;
}

applyLoopbackNoProxyEnv();

function childProcessEnv(env = process.env) {
  const noProxy = mergeNoProxy(env.NO_PROXY || env.no_proxy || "");
  return { ...env, NO_PROXY: noProxy, no_proxy: noProxy };
}

function waitDetail(baseDetail, startedAt) {
  const seconds = Math.max(1, Math.floor((Date.now() - startedAt) / 1000));
  if (!baseDetail) {
    return `Elapsed ${seconds}s`;
  }
  return `${baseDetail} (elapsed ${seconds}s)`;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function fileExists(candidate) {
  try {
    return fs.existsSync(candidate);
  } catch {
    return false;
  }
}

function manifestPath() {
  return path.join(os.homedir(), ".xagent", "cli.json");
}

function validCommand(value) {
  return Array.isArray(value) && value.length > 0 && value.every((item) => typeof item === "string" && item.length > 0);
}

// Desktop never searches for xagent. It accepts an explicit override or the
// launch command written by the CLI/install script.
function configuredXagentCommand() {
  const override = process.env.XAGENT_BINARY;
  if (override && fileExists(override)) {
    return [override];
  }

  const manifest = manifestPath();
  if (!fileExists(manifest)) {
    return null;
  }
  try {
    const data = JSON.parse(fs.readFileSync(manifest, "utf8"));
    if (data && validCommand(data.command)) {
      return data.command.slice();
    }
  } catch {
    // Corrupt or unreadable manifest: treat as missing.
  }
  return null;
}

function resolveXagentCommand() {
  const command = configuredXagentCommand();
  return command ? [...command, "client", "web", "start"] : null;
}

async function fetchWebShell(webUrl) {
  const url = new URL(webUrl);
  const transport = url.protocol === "https:" ? https : http;
  const hostname = url.hostname.replace(/^\[(.*)\]$/, "$1");

  return new Promise((resolve) => {
    const request = transport.request(
      {
        protocol: url.protocol,
        hostname,
        port: url.port || (url.protocol === "https:" ? 443 : 80),
        path: url.pathname || "/",
        method: "GET",
        timeout: 3000,
      },
      (response) => {
        const contentType = String(response.headers["content-type"] || "");
        response.resume();
        resolve(
          response.statusCode >= 200
            && response.statusCode < 300
            && contentType.includes("text/html"),
        );
      },
    );

    request.on("timeout", () => {
      request.destroy();
      resolve(false);
    });

    request.on("error", () => {
      resolve(false);
    });

    request.end();
  });
}

async function pollWebReady(
  webUrl,
  deadline,
  onStatus,
  pollMs = DEFAULT_POLL_MS,
  shellFetcher = fetchWebShell,
) {
  const startedAt = Date.now();
  let pass = 0;
  while (Date.now() < deadline) {
    pass += 1;
    if (pass === 1) {
      onStatus?.(BOOTSTRAP_STATUS.CHECK_WEB);
    } else if (pass % 4 === 0) {
      onStatus?.({
        title: BOOTSTRAP_STATUS.WAIT_WEB.title,
        detail: waitDetail(BOOTSTRAP_STATUS.WAIT_WEB.detail, startedAt),
      });
    }
    if (await shellFetcher(webUrl)) {
      return true;
    }
    const remainingMs = deadline - Date.now();
    if (remainingMs > 0) {
      await sleep(Math.min(pollMs, remainingMs));
    }
  }
  return null;
}

function spawnDetached(command) {
  const [binary, ...args] = command;
  const child = spawn(binary, args, {
    detached: true,
    stdio: "ignore",
    env: childProcessEnv(),
  });
  child.unref();
}

async function startWebClient(command = resolveXagentCommand()) {
  if (!command) {
    return false;
  }
  try {
    spawnDetached(command);
    return true;
  } catch {
    return false;
  }
}

async function runInstallScript(options = {}) {
  const onLine = options.onLine;
  const onStatus = options.onStatus;
  onStatus?.(BOOTSTRAP_STATUS.INSTALLING);

  if (process.platform === "win32") {
    return { ok: false, error: "In-app install is not supported on Windows yet. Use pip install myxagent." };
  }

  return new Promise((resolve) => {
    const shell = "/bin/bash";
    const script = `curl -fsSL ${INSTALL_SCRIPT_URL} | bash`;
    const child = spawn(shell, ["-lc", script], {
      env: childProcessEnv(),
      stdio: ["ignore", "pipe", "pipe"],
    });

    let stderr = "";
    const handleChunk = (chunk, stream) => {
      const text = chunk.toString();
      if (stream === "stderr") {
        stderr += text;
      }
      for (const line of text.split(/\r?\n/)) {
        const trimmed = line.trim();
        if (trimmed) {
          onLine?.(trimmed);
        }
      }
    };

    child.stdout.on("data", (chunk) => handleChunk(chunk, "stdout"));
    child.stderr.on("data", (chunk) => handleChunk(chunk, "stderr"));

    child.on("error", (error) => {
      resolve({ ok: false, error: error.message || String(error) });
    });

    child.on("close", (code) => {
      if (code === 0) {
        resolve({ ok: true });
        return;
      }
      resolve({
        ok: false,
        error: stderr.trim() || `Install script exited with code ${code}`,
      });
    });
  });
}

async function runBootstrap(webUrl, options = {}) {
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const initialProbeMs = options.initialProbeMs ?? DEFAULT_INITIAL_PROBE_MS;
  const pollMs = options.pollMs ?? DEFAULT_POLL_MS;
  const onStatus = options.onStatus;
  const resolveCommand = options.resolveXagentCommand || resolveXagentCommand;
  const startWeb = options.startWebClient || startWebClient;
  const shellFetcher = options.fetchWebShell || fetchWebShell;

  onStatus?.(BOOTSTRAP_STATUS.CHECK_ENV);
  let webReady = await pollWebReady(webUrl, Date.now() + initialProbeMs, onStatus, pollMs, shellFetcher);

  if (!webReady) {
    onStatus?.(BOOTSTRAP_STATUS.FIND_XAGENT);
    const command = resolveCommand();
    if (!command) {
      return { ok: false, reason: "missing-xagent" };
    }

    onStatus?.(BOOTSTRAP_STATUS.START_WEB);
    const started = await startWeb(command);
    if (!started) {
      return { ok: false, reason: "spawn-failed" };
    }

    webReady = await pollWebReady(webUrl, Date.now() + timeoutMs, onStatus, pollMs, shellFetcher);
    if (!webReady) {
      return { ok: false, reason: "web-timeout" };
    }
  }

  onStatus?.(BOOTSTRAP_STATUS.ENTERING);
  return { ok: true };
}

module.exports = {
  BOOTSTRAP_STATUS,
  DEFAULT_INITIAL_PROBE_MS,
  DEFAULT_POLL_MS,
  DEFAULT_TIMEOUT_MS,
  INSTALL_COMMAND,
  INSTALL_SCRIPT_URL,
  LOOPBACK_NO_PROXY_HOSTS,
  applyLoopbackNoProxyEnv,
  childProcessEnv,
  configuredXagentCommand,
  fetchWebShell,
  manifestPath,
  normalizeStatus,
  pollWebReady,
  resolveXagentCommand,
  runBootstrap,
  runInstallScript,
  spawnDetached,
  startWebClient,
};

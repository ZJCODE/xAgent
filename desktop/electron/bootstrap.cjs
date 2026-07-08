const { spawn, execFileSync } = require("child_process");
const fs = require("fs");
const http = require("http");
const https = require("https");
const os = require("os");
const path = require("path");

const DEFAULT_POLL_MS = 500;
const DEFAULT_TIMEOUT_MS = 45000;
const DEFAULT_INITIAL_PROBE_MS = 2500;
const DEFAULT_API_WAIT_MS = 5000;
const INSTALL_SCRIPT_URL = "https://raw.githubusercontent.com/ZJCODE/xagent/main/install.sh";
const INSTALL_COMMAND = `curl -fsSL ${INSTALL_SCRIPT_URL} | bash`;
const LOOPBACK_NO_PROXY_HOSTS = ["localhost", "127.0.0.1", "::1", "0.0.0.0"];

const BOOTSTRAP_STATUS = {
  CHECK_ENV: { title: "Checking environment", detail: "Looking for the xAgent backend on this Mac" },
  CHECK_WEB: { title: "Connecting to local service", detail: "Checking the web UI at 127.0.0.1:1415" },
  FIND_XAGENT: { title: "Locating xAgent", detail: "Searching common install locations and shell PATH" },
  START_WEB: { title: "Starting web service", detail: "First launch may take a few seconds" },
  WAIT_WEB: { title: "Waiting for web service", detail: "Loading the management UI" },
  START_API: { title: "Starting agent service", detail: "Preparing chat and task capabilities" },
  WAIT_API: { title: "Waiting for agent service", detail: "First launch usually takes a few seconds" },
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

function enrichedPathEnv() {
  const home = os.homedir();
  const extra = [
    path.join(home, ".local", "bin"),
    path.join(home, "anaconda3", "bin"),
    path.join(home, "miniconda3", "bin"),
    path.join(home, "mambaforge", "bin"),
    path.join(home, "miniforge3", "bin"),
    path.join(home, "opt", "anaconda3", "bin"),
    path.join(home, "opt", "miniconda3", "bin"),
    process.env.CONDA_PREFIX ? path.join(process.env.CONDA_PREFIX, "bin") : "",
    "/usr/local/bin",
    "/opt/homebrew/bin",
    "/usr/bin",
    "/bin",
  ];
  const current = process.env.PATH || "";
  const parts = [...extra, ...current.split(path.delimiter)];
  const seen = new Set();
  const merged = parts.filter((entry) => {
    if (!entry || seen.has(entry)) return false;
    seen.add(entry);
    return true;
  });
  const noProxy = mergeNoProxy(process.env.NO_PROXY || process.env.no_proxy || "");
  return { ...process.env, PATH: merged.join(path.delimiter), NO_PROXY: noProxy, no_proxy: noProxy };
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

function findOnPath(binaryName, pathEnv = enrichedPathEnv().PATH) {
  for (const dir of pathEnv.split(path.delimiter)) {
    if (!dir) continue;
    const candidate = path.join(dir, binaryName);
    if (fileExists(candidate)) {
      return candidate;
    }
  }
  return null;
}

function shellQuote(value) {
  return `'${String(value).replace(/'/g, "'\\''")}'`;
}

function shellCandidates() {
  if (process.platform === "win32") {
    return [];
  }
  const candidates = [process.env.SHELL, "/bin/zsh", "/bin/bash", "/bin/sh"];
  const seen = new Set();
  return candidates.filter((candidate) => {
    if (!candidate || seen.has(candidate) || !fileExists(candidate)) return false;
    seen.add(candidate);
    return true;
  });
}

function findViaLoginShell(binaryName) {
  if (process.platform === "win32") {
    return null;
  }
  const command = `command -v ${shellQuote(binaryName)} || which ${shellQuote(binaryName)}`;
  for (const shell of shellCandidates()) {
    try {
      const resolved = execFileSync(shell, ["-lc", command], {
        encoding: "utf8",
        env: enrichedPathEnv(),
      })
        .trim()
        .split(/\r?\n/)[0];
      if (resolved && fileExists(resolved)) {
        return resolved;
      }
    } catch {
      // try next shell
    }
  }
  return null;
}

function condaInstallCandidates(binaryName) {
  const home = os.homedir();
  return [
    path.join(home, "anaconda3", "bin", binaryName),
    path.join(home, "miniconda3", "bin", binaryName),
    path.join(home, "mambaforge", "bin", binaryName),
    path.join(home, "miniforge3", "bin", binaryName),
    path.join(home, "opt", "anaconda3", "bin", binaryName),
    path.join(home, "opt", "miniconda3", "bin", binaryName),
    process.env.CONDA_PREFIX ? path.join(process.env.CONDA_PREFIX, "bin", binaryName) : "",
  ].filter(Boolean);
}

function findXagentBinary() {
  const binaryName = process.platform === "win32" ? "xagent.exe" : "xagent";

  for (const candidate of condaInstallCandidates(binaryName)) {
    if (fileExists(candidate)) {
      return candidate;
    }
  }

  const enrichedBinary = findOnPath(binaryName);
  if (enrichedBinary) {
    return enrichedBinary;
  }

  const loginShellBinary = findViaLoginShell(binaryName);
  if (loginShellBinary) {
    return loginShellBinary;
  }

  const localBin = path.join(os.homedir(), ".local", "bin", binaryName);
  if (fileExists(localBin)) {
    return localBin;
  }

  if (process.platform === "darwin") {
    const libraryPython = path.join(os.homedir(), "Library", "Python");
    if (fileExists(libraryPython)) {
      for (const versionDir of fs.readdirSync(libraryPython)) {
        const candidate = path.join(libraryPython, versionDir, "bin", binaryName);
        if (fileExists(candidate)) {
          return candidate;
        }
      }
    }
  }

  if (process.platform === "win32") {
    const localAppData = process.env.LOCALAPPDATA || "";
    if (localAppData) {
      const scriptsDir = path.join(localAppData, "Programs", "Python");
      if (fileExists(scriptsDir)) {
        for (const entry of fs.readdirSync(scriptsDir)) {
          const candidate = path.join(scriptsDir, entry, "Scripts", binaryName);
          if (fileExists(candidate)) {
            return candidate;
          }
        }
      }
    }
  }

  return null;
}

function findPython() {
  const loginShellPython = findViaLoginShell("python3") || findViaLoginShell("python");
  if (loginShellPython) {
    return loginShellPython;
  }

  for (const name of ["python3", "python"]) {
    const enriched = findOnPath(name);
    if (enriched) {
      return enriched;
    }
    try {
      const resolved = execFileSync("which", [name], {
        encoding: "utf8",
        env: enrichedPathEnv(),
      }).trim();
      if (resolved && fileExists(resolved)) {
        return resolved;
      }
    } catch {
      // try next candidate
    }
  }
  return null;
}

function resolveXagentCommands() {
  const commands = [];
  const seen = new Set();

  const addCommand = (command) => {
    const key = command.join("\0");
    if (seen.has(key)) return;
    seen.add(key);
    commands.push(command);
  };

  const xagentBinary = findXagentBinary();
  if (xagentBinary) {
    addCommand([xagentBinary, "client", "web", "start"]);
  }

  const python = findPython();
  if (python) {
    addCommand([python, "-m", "xagent.interfaces.cli", "client", "web", "start"]);
  }

  return commands;
}

function directJsonRequest(targetUrl, options = {}) {
  const method = options.method || "GET";
  const timeoutMs = options.timeoutMs || 3000;
  const url = new URL(targetUrl);
  const transport = url.protocol === "https:" ? https : http;
  const hostname = url.hostname.replace(/^\[(.*)\]$/, "$1");

  return new Promise((resolve) => {
    const request = transport.request(
      {
        protocol: url.protocol,
        hostname,
        port: url.port || (url.protocol === "https:" ? 443 : 80),
        path: `${url.pathname}${url.search}`,
        method,
        headers: {
          Accept: "application/json",
        },
        timeout: timeoutMs,
      },
      (response) => {
        let body = "";
        response.setEncoding("utf8");
        response.on("data", (chunk) => {
          body += chunk;
        });
        response.on("end", () => {
          let data = null;
          try {
            data = body ? JSON.parse(body) : null;
          } catch {
            data = null;
          }
          resolve({
            ok: response.statusCode >= 200 && response.statusCode < 300,
            statusCode: response.statusCode,
            data,
          });
        });
      },
    );

    request.on("timeout", () => {
      request.destroy();
      resolve({ ok: false, statusCode: 0, data: null });
    });

    request.on("error", () => {
      resolve({ ok: false, statusCode: 0, data: null });
    });

    request.end();
  });
}

async function fetchWebHealth(webUrl) {
  try {
    const response = await directJsonRequest(`${webUrl}/api/health`, { timeoutMs: 3000 });
    if (!response.ok) {
      return null;
    }
    return response.data;
  } catch {
    return null;
  }
}

async function fetchWebShell(webUrl) {
  try {
    const response = await directJsonRequest(webUrl, { timeoutMs: 3000 });
    return response.ok;
  } catch {
    return false;
  }
}

async function pollWebReady(
  webUrl,
  deadline,
  onStatus,
  pollMs = DEFAULT_POLL_MS,
  healthFetcher = fetchWebHealth,
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
    const health = await healthFetcher(webUrl);
    if (health && health.status === "ok" && health.web) {
      return health;
    }
    if (await shellFetcher(webUrl)) {
      return { status: "ok", web: true, api_reachable: false, health_unavailable: true };
    }
    const remainingMs = deadline - Date.now();
    if (remainingMs > 0) {
      await sleep(Math.min(pollMs, remainingMs));
    }
  }
  return null;
}

async function pollApiReady(webUrl, deadline, onStatus, pollMs = DEFAULT_POLL_MS, healthFetcher = fetchWebHealth) {
  const startedAt = Date.now();
  let pass = 0;
  while (Date.now() < deadline) {
    pass += 1;
    if (pass === 1) {
      onStatus?.(BOOTSTRAP_STATUS.WAIT_API);
    } else if (pass % 4 === 0) {
      onStatus?.({
        title: BOOTSTRAP_STATUS.WAIT_API.title,
        detail: waitDetail(BOOTSTRAP_STATUS.WAIT_API.detail, startedAt),
      });
    }
    const health = await healthFetcher(webUrl);
    if (health?.api_reachable) {
      return health;
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
    env: enrichedPathEnv(),
  });
  child.unref();
}

async function startWebClient(commands = resolveXagentCommands()) {
  for (const command of commands) {
    try {
      spawnDetached(command);
      return true;
    } catch {
      // try next command shape
    }
  }
  return false;
}

async function startApiChannel(webUrl) {
  try {
    const response = await directJsonRequest(`${webUrl}/api/channels/api/start`, { method: "POST", timeoutMs: 30000 });
    return response.ok;
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
      env: enrichedPathEnv(),
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
  const apiWaitMs = options.apiWaitMs ?? DEFAULT_API_WAIT_MS;
  const pollMs = options.pollMs ?? DEFAULT_POLL_MS;
  const onStatus = options.onStatus;
  const resolveCommands = options.resolveXagentCommands || resolveXagentCommands;
  const startWeb = options.startWebClient || startWebClient;
  const startApi = options.startApiChannel || startApiChannel;
  const healthFetcher = options.fetchWebHealth || fetchWebHealth;
  const shellFetcher = options.fetchWebShell || fetchWebShell;

  onStatus?.(BOOTSTRAP_STATUS.CHECK_ENV);
  let health = await pollWebReady(webUrl, Date.now() + initialProbeMs, onStatus, pollMs, healthFetcher, shellFetcher);

  if (!health) {
    onStatus?.(BOOTSTRAP_STATUS.FIND_XAGENT);
    const commands = resolveCommands();
    if (commands.length === 0) {
      return { ok: false, reason: "missing-xagent" };
    }

    onStatus?.(BOOTSTRAP_STATUS.START_WEB);
    const started = await startWeb(commands);
    if (!started) {
      return { ok: false, reason: "spawn-failed" };
    }

    health = await pollWebReady(webUrl, Date.now() + timeoutMs, onStatus, pollMs, healthFetcher, shellFetcher);
    if (!health) {
      return { ok: false, reason: "web-timeout" };
    }
  }

  if (!health.api_reachable) {
    onStatus?.(BOOTSTRAP_STATUS.START_API);
    await startApi(webUrl);
    await pollApiReady(webUrl, Date.now() + apiWaitMs, onStatus, pollMs, healthFetcher);
  }

  onStatus?.(BOOTSTRAP_STATUS.ENTERING);
  return { ok: true };
}

module.exports = {
  BOOTSTRAP_STATUS,
  DEFAULT_API_WAIT_MS,
  DEFAULT_INITIAL_PROBE_MS,
  DEFAULT_POLL_MS,
  DEFAULT_TIMEOUT_MS,
  INSTALL_COMMAND,
  INSTALL_SCRIPT_URL,
  LOOPBACK_NO_PROXY_HOSTS,
  applyLoopbackNoProxyEnv,
  condaInstallCandidates,
  directJsonRequest,
  enrichedPathEnv,
  fetchWebHealth,
  fetchWebShell,
  findOnPath,
  findPython,
  findViaLoginShell,
  findXagentBinary,
  normalizeStatus,
  pollApiReady,
  pollWebReady,
  resolveXagentCommands,
  runBootstrap,
  runInstallScript,
  spawnDetached,
  startApiChannel,
  startWebClient,
};

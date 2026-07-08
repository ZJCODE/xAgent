const { app, BrowserWindow, clipboard, ipcMain, session, shell } = require("electron");
const path = require("path");
const { INSTALL_COMMAND, normalizeStatus, runBootstrap, runInstallScript } = require("./bootstrap.cjs");

const WEB_URL = (process.env.XAGENT_WEB_URL || "http://127.0.0.1:1415").replace(/\/$/, "");
const WINDOW_TITLE = process.env.XAGENT_APP_TITLE || "xAgent";
const SKIP_BOOTSTRAP = process.env.XAGENT_SKIP_BOOTSTRAP === "1";
const LOOPBACK_PROXY_BYPASS_RULES = ["<-loopback>", "localhost", "127.0.0.1", "::1", "[::1]"].join(";");

const SPLASH_URL = `file://${path.join(__dirname, "splash.html")}`;
const SETUP_URL = `file://${path.join(__dirname, "setup.html")}`;

app.commandLine.appendSwitch("proxy-bypass-list", LOOPBACK_PROXY_BYPASS_RULES);
app.commandLine.appendSwitch("no-proxy-server");

/** @type {import("electron").BrowserWindow | null} */
let mainWindow = null;
let bootstrapPromise = null;
let installPromise = null;
let bootstrapComplete = false;

function resolveWebUrl() {
  return WEB_URL;
}

function setSplashStatus(window, status) {
  if (!window || window.isDestroyed()) return;
  const normalized = normalizeStatus(status);
  const script = `(() => {
    const title = document.getElementById("status");
    const detail = document.getElementById("detail");
    if (title) title.textContent = ${JSON.stringify(normalized.title)};
    if (detail) detail.textContent = ${JSON.stringify(normalized.detail)};
  })();`;
  void window.webContents.executeJavaScript(script).catch(() => {});
}

function sendInstallProgress(window, payload) {
  if (!window || window.isDestroyed()) return;
  window.webContents.send("bootstrap:install-progress", payload);
}

function loadLocalPage(window, fileUrl) {
  return window.loadURL(fileUrl);
}

async function configureLocalProxyBypass() {
  try {
    await session.defaultSession.setProxy({
      proxyRules: "direct://",
      proxyBypassRules: LOOPBACK_PROXY_BYPASS_RULES,
    });
  } catch {
    // Command-line proxy switches above still apply before the browser process starts.
  }
}

async function beginBootstrap(window) {
  if (!window || window.isDestroyed()) {
    return;
  }
  if (installPromise) {
    return;
  }

  bootstrapComplete = false;
  await loadLocalPage(window, SPLASH_URL);
  setSplashStatus(window, { title: "Starting xAgent", detail: "Preparing the application environment" });

  const result = await runBootstrap(resolveWebUrl(), {
    onStatus: (message) => setSplashStatus(window, message),
  });

  if (!window || window.isDestroyed()) {
    return;
  }

  if (!result.ok) {
    if (result.reason === "missing-xagent") {
      await loadLocalPage(window, SETUP_URL);
      return;
    }
    setSplashStatus(window, "Could not connect to xAgent. Retrying...");
    await loadLocalPage(window, SETUP_URL);
    return;
  }

  bootstrapComplete = true;
  await window.loadURL(resolveWebUrl());
}

function ensureBootstrap(window) {
  if (installPromise) {
    return installPromise;
  }
  if (SKIP_BOOTSTRAP) {
    bootstrapComplete = true;
    return window.loadURL(resolveWebUrl());
  }

  if (bootstrapPromise) {
    return bootstrapPromise;
  }

  bootstrapPromise = beginBootstrap(window).finally(() => {
    bootstrapPromise = null;
  });
  return bootstrapPromise;
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1220,
    height: 820,
    minWidth: 920,
    minHeight: 640,
    title: WINDOW_TITLE,
    backgroundColor: "#f7f8fa",
    show: false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      preload: path.join(__dirname, "preload.cjs"),
    },
  });

  mainWindow.once("ready-to-show", () => {
    mainWindow?.show();
  });

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    void shell.openExternal(url);
    return { action: "deny" };
  });

  mainWindow.webContents.on("did-fail-load", (_event, _errorCode, _errorDescription, validatedURL) => {
    if (SKIP_BOOTSTRAP || !validatedURL.startsWith("http")) {
      return;
    }
    if (!bootstrapComplete) {
      return;
    }
    bootstrapComplete = false;
    void ensureBootstrap(mainWindow);
  });

  if (SKIP_BOOTSTRAP) {
    void mainWindow.loadURL(resolveWebUrl());
  } else {
    void ensureBootstrap(mainWindow);
  }

  mainWindow.on("closed", () => {
    mainWindow = null;
    bootstrapComplete = false;
  });
}

ipcMain.handle("bootstrap:retry", async () => {
  if (!mainWindow || mainWindow.isDestroyed()) {
    return;
  }
  if (installPromise) {
    return;
  }
  await ensureBootstrap(mainWindow);
});

ipcMain.handle("bootstrap:copy-install", async () => {
  clipboard.writeText(INSTALL_COMMAND);
});

ipcMain.handle("bootstrap:install-backend", async () => {
  if (!mainWindow || mainWindow.isDestroyed()) {
    return { ok: false, error: "Desktop window is not available." };
  }
  if (installPromise) {
    return installPromise;
  }

  const window = mainWindow;
  installPromise = (async () => {
    sendInstallProgress(window, {
      type: "status",
      status: { title: "Installing backend", detail: "Downloading and configuring xAgent. Keep this window open." },
    });
    const result = await runInstallScript({
      onLine: (line) => sendInstallProgress(window, { type: "log", line }),
      onStatus: (status) => sendInstallProgress(window, { type: "status", status: normalizeStatus(status) }),
    });

    if (!result.ok) {
      sendInstallProgress(window, { type: "done", ok: false, error: result.error || "Install failed." });
      return result;
    }

    sendInstallProgress(window, { type: "done", ok: true });
    return { ok: true };
  })().finally(() => {
    installPromise = null;
  });

  return installPromise;
});

const singleInstance = app.requestSingleInstanceLock();
if (!singleInstance) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (!mainWindow) {
      createWindow();
      return;
    }
    if (mainWindow.isMinimized()) {
      mainWindow.restore();
    }
    mainWindow.focus();
  });

  app.whenReady().then(async () => {
    await configureLocalProxyBypass();
    createWindow();

    app.on("activate", () => {
      if (BrowserWindow.getAllWindows().length === 0) {
        createWindow();
      }
    });
  });

  app.on("window-all-closed", () => {
    if (process.platform !== "darwin") {
      app.quit();
    }
  });
}

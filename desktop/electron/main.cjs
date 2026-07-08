const { app, BrowserWindow, shell } = require("electron");
const path = require("path");

const WEB_URL = (process.env.XAGENT_WEB_URL || "http://127.0.0.1:1415").replace(/\/$/, "");
const WINDOW_TITLE = process.env.XAGENT_APP_TITLE || "xAgent";

/** @type {import("electron").BrowserWindow | null} */
let mainWindow = null;

function resolveWebUrl() {
  return WEB_URL;
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

  void mainWindow.loadURL(resolveWebUrl());

  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

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

  app.whenReady().then(() => {
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

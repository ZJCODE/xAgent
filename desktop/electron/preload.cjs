const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("xagentDesktop", {
  retryBootstrap: () => ipcRenderer.invoke("bootstrap:retry"),
  copyInstallCommand: () => ipcRenderer.invoke("bootstrap:copy-install"),
  installBackend: () => ipcRenderer.invoke("bootstrap:install-backend"),
  onInstallProgress: (callback) => {
    const listener = (_event, payload) => callback(payload);
    ipcRenderer.on("bootstrap:install-progress", listener);
    return () => ipcRenderer.removeListener("bootstrap:install-progress", listener);
  },
});

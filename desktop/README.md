# xAgent Desktop

Electron shell for the xAgent web UI. The desktop app loads the configured web UI URL (`http://127.0.0.1:1415` by default) inside a native window.

`pip install myxagent` does **not** bundle Electron. End users install the desktop app from GitHub Releases; the CLI opens it with `xagent client desktop open`.

## Prerequisites

- Node.js 20+ and npm — **build only**
- At runtime, a web UI server must be reachable at `XAGENT_WEB_URL` (default `http://127.0.0.1:1415`)

After the window opens, use **Channels** in the app to start the API channel (or other integrations) for the selected agent. You do not need to start those channels from the terminal first.

## Development

```bash
cd desktop
npm install
npm start
```

Environment variables:

```bash
export XAGENT_WEB_URL=http://127.0.0.1:1415
export XAGENT_APP_TITLE=xAgent
npm start
```

## Build installers

```bash
cd desktop
npm install

npm run build        # current platform
npm run build:mac           # .dmg (signed if a cert is available)
npm run build:mac:unsigned  # .dmg without code signing (local dev)
npm run build:win    # NSIS .exe (run on Windows or CI)
npm run build:linux  # .AppImage
npm run build:all    # mac + win + linux (usually CI only)
```

Artifacts are written to `desktop/dist/`:

| Platform | Example artifact |
|----------|------------------|
| macOS | `xAgent-0.1.0-mac-arm64.dmg` |
| Windows | `xAgent-0.1.0-win-x64.exe` |
| Linux | `xAgent-0.1.0-linux-x64.AppImage` |

### Icons (optional)

Place custom icons in `desktop/build/` before building:

- `icon.icns` — macOS
- `icon.ico` — Windows
- `icon.png` — Linux (512×512 recommended)

Without these files, electron-builder falls back to the default Electron icon.

### macOS signing stuck?

If the build hangs at `signing file=dist/mac-arm64/xAgent.app`:

1. **Check Keychain** — macOS may be waiting for your password. Open **Keychain Access**, unlock the login keychain, and look for a hidden prompt.
2. **Cancel and rebuild unsigned** (fine for local testing):
   ```bash
   npm run build:mac:unsigned
   ```
3. **Certificate type** — `Apple Development` is for local dev only. Public releases need **Developer ID Application** plus notarization.

Unsigned `.app` / `.dmg` still runs locally; Gatekeeper may warn on first open (right-click → Open).

## Publish to GitHub Releases

### 1. Bump version

Update `version` in `desktop/package.json` (keep it aligned with the release tag you plan to ship).

### 2. Build artifacts

Build on each target platform, or use CI (recommended):

```bash
cd desktop
npm ci
npm run build:mac     # on macOS
npm run build:win     # on Windows
npm run build:linux   # on Linux
```

Collect files from `desktop/dist/`.

### 3. Create and push a tag

```bash
git tag desktop-v0.1.0
git push origin desktop-v0.1.0
```

Use a dedicated tag prefix such as `desktop-v*` so desktop releases can ship independently from the Python package.

### 4. Upload to GitHub Releases

Using [GitHub CLI](https://cli.github.com/):

```bash
gh release create desktop-v0.1.0 \
  desktop/dist/xAgent-0.1.0-mac-arm64.dmg \
  desktop/dist/xAgent-0.1.0-win-x64.exe \
  desktop/dist/xAgent-0.1.0-linux-x64.AppImage \
  --title "xAgent Desktop 0.1.0" \
  --notes "$(cat <<'EOF'
## xAgent Desktop 0.1.0

Native window for the xAgent web UI.

### Install

- **macOS**: open the `.dmg` and drag xAgent to Applications
- **Windows**: run the installer
- **Linux**: `chmod +x` the AppImage and move it to `~/.local/bin/xagent-desktop`

### Use with pip

```bash
pip install myxagent
xagent client desktop open
```

In the app, open **Channels** and start the API channel for your agent when needed.

Override the installed app path with:

```bash
export XAGENT_DESKTOP_APP=/path/to/xAgent.app
```
EOF
)"
```

Without `gh`, create the release in the GitHub UI and upload the same artifacts manually.

### 5. Verify after publish

1. Install the artifact on a clean machine (or VM)
2. Run `xagent client desktop open`
3. In the app, open **Channels** and click **Start** on the API channel for your agent
4. Confirm Chat and other agent features work

## CLI integration

| Command | Description |
|---------|-------------|
| `xagent client desktop start` | Launch the desktop client in the background |
| `xagent client desktop open` | Open or focus the desktop window |
| `xagent client desktop stop` | Stop the background desktop process |
| `xagent client desktop status` | Show desktop client status |

Installed app lookup order:

1. `XAGENT_DESKTOP_APP` environment variable
2. macOS: `/Applications/xAgent.app`
3. Windows: `Program Files\xAgent\xAgent.exe`
4. Linux: `xagent-desktop` on `PATH`, or `xAgent-*-linux-*.AppImage` in common folders
5. Development fallback: `desktop/` in a cloned repository with `npm install`

## Directory layout

```text
desktop/
  electron/          # main process + preload
  build/             # icons and other build resources
  dist/              # build output (gitignored)
  node_modules/      # dependencies (gitignored)
  package.json
  package-lock.json
```

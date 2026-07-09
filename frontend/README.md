# xAgent Web UI

This directory contains the Vite + React + TypeScript source for the built-in xAgent web UI.

## Development

Install dependencies once:

```bash
cd frontend
npm install
```

Run the frontend dev server:

```bash
npm run dev
```

The dev server proxies API and WebSocket requests to the built-in web client at `http://127.0.0.1:1415`, so start it before running the frontend:

```bash
xagent web start
```

Then run the frontend dev server in another terminal. The web client proxies chat traffic to whichever agent's API channel is currently selected.

## Build Static Assets

Generate the static files served by FastAPI:

```bash
cd frontend
npm run build
```

The build writes directly to:

```text
../xagent/interfaces/static/
```

Commit the generated `xagent/interfaces/static/index.html` and `xagent/interfaces/static/assets/*` files. End users who install the Python package do not need Node.js; they only need the prebuilt static assets included in the package.

## Packaging Check

Before publishing, verify the Python wheel includes the latest static assets:

```bash
rm -rf build dist
uv build --wheel
```

The wheel output should list `xagent/interfaces/static/index.html` and the current files under `xagent/interfaces/static/assets/`.

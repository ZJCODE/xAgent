# xAgent Web UI

This Vite/React application is a local management client. Its Python bridge
talks to the selected Agent's loopback Runtime control service; chat does not
depend on the Public API channel. Completed task results are read from the
durable Runtime timeline and appear in Chat without creating a Web delivery.

The UI is the primary control surface for desktop systems. It exposes:

- Agent creation, switching, Runtime lifecycle, and deletion
- direct chat plus the persisted cross-channel message timeline
- read-only Markdown diary memory
- Agent tasks defined by an instruction and schedule, with optional explicit delivery
- channel start, stop, restart, setup, and logs
- blocked, failed, and uncertain delivery review
- safe schema-v2 configuration editing with masked secrets

Explicit cross-channel identity linking remains an advanced CLI operation; it
is intentionally not mixed into the everyday desktop workflow.

## Build

```bash
pnpm install
pnpm build
```

The production build writes directly to `../xagent/interfaces/static/`.
Packaged users do not need Node.js.

## Development

```bash
pnpm dev
```

The Vite server expects the Python Web bridge on `127.0.0.1:1415`. Start that
bridge with `xagent web --no-open`. The Agent Runtime remains the only owner of
operational state; the Web bridge forwards browser requests to its authenticated
loopback control service.

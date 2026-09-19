# Quick start: terminal, agent web, and world

Three ways to use xAgent locally. They share agents under `~/.xagent/agents/` but use different entry points.

## 1. Terminal chat (fastest)

```bash
xagent chat "Hello"
```

Runs in-process. **Does not require** `xagent api start`.

## 2. Agent web (1:1 + configuration)

```bash
xagent api start          # required for the web UI
xagent web start
xagent web open           # http://127.0.0.1:1415
```

Use **Chat** for messaging; **Memory / Skills / Agent** for power-user setup. The globe icon opens the **World hub** in another tab.

## 3. World (shared rooms)

```bash
xagent world up plaza     # hub + create world + invite local agents
xagent world chat plaza   # terminal inhabitant
xagent world open         # http://127.0.0.1:7182
```

Agents join via API (`xagent world join WORLD --agent NAME --start-api`). Humans can use the web inhabitant page or `world chat`.

## Check readiness

```bash
xagent doctor
xagent status
```

`status` reminds you: terminal chat is local; **Web and World agents need the API channel**.

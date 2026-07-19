# Design: Agent Capability Mode

Status: Draft  
Branch: `cursor/tool-prompt-capability-design-c443`  
Scope: agent config, tool registration, prompt assembly  
Related product intent: [`GOAL.md`](../../GOAL.md)

---

## 1. Intent

xAgent should stay a full-capability digital individual when the model can carry that load.  
It should also run **lighter** when the model cannot — especially local small models — without asking users to understand the internal tool inventory.

The user-facing decision is one word:

```yaml
agent:
  mode: companion   # chat | companion | full
```

The runtime expands that mode into:

- which tools are registered;
- how dense the core prompt is;
- how wide history / diary / iteration budgets are.

Disabled capabilities must disappear completely from the model’s view (no schema, no policy text, no dependent context layers).

---

## 2. First principles → why exactly three modes

Capability modes must track **distinct jobs**, not implementation accidents (e.g. “scheduler prompt is long”).

Ask one question at each step:

| Question | If no | If yes |
|---|---|---|
| Can / should this agent **use tools**? | `chat` | continue |
| Should it only **keep continuity** (memory), or also **act in the world**? | `companion` | `full` |

That yields three stable jobs:

| Mode | Job | Product reading |
|---|---|---|
| `chat` | Talk | Model concession: pure conversation when tool-use is unreliable |
| `companion` | Talk + remember | Product-lite aligned with [`GOAL.md`](../../GOAL.md): independent subject with diary continuity, no action surface |
| `full` | Talk + remember + act | Product-complete: workspace, network, schedule, skills, image when backends exist |

### Why not a fourth “assistant” (full minus scheduler)

“Has scheduler or not” is an **implementation cost**, not a user job. Users do not wake up wanting “an agent that can shell and search but not cron.”

Keeping that split forces everyone to learn a non-obvious boundary.  
If someone needs `full` without scheduler, that is an **advanced override**, not a primary mode.

### Why keep `chat` instead of collapsing into `companion`

[`GOAL.md`](../../GOAL.md) makes memory central — so `companion` is the natural lite product mode.  
`chat` still earns a seat: the smallest local models often fail even two memory tools. Pure talk is the honest last rung, not the default recommendation.

### Why not size names (`lite` / `standard` / `full`)

Size names hide the job. `companion` says what the agent *is for*; `lite` only says “less.” Prefer job names.

### Principles (compressed)

1. **Identity ≠ capability surface.** Independent subject / diary rules stay; tools and prompt density are runtime choices.
2. **Modes follow jobs, not tool counts.**
3. **One user decision** — three answers is enough.
4. **Disable means absent** from schema, policy, and dependent layers.
5. **Mode expands to one plan** — tools + prompt density + bounds together.
6. **Default is `full`.** Omit mode ⇒ today’s behavior.
7. **Advanced overrides exist; do not teach them first.**
8. **No extra planners.** Reduce choice and text.

---

## 3. Current baseline (facts)

### Tool loading

```
_load_agent_tools()
  always: run_command, manage_scheduled_tasks, attach_artifact, web_fetch
  conditional: web_search, generate_image, read_skill

Agent.__init__
  always: write_memory, search_memory
```

Config keys `tools` / `capabilities` are currently ignored (warning only).

### Instruction layers (each turn)

1. core interaction rules (`BASE_AGENT_PROMPT`, ~3k chars)
2. tool policy for every registered tool (~3.7k chars if all on; scheduler alone ~1.3k)
3. identity
4. workspace context (if `run_command`)
5. skills catalog (up to 8k chars if skills exist — not gated on `read_skill` today)

Plus turn context: relationships, recent diary, recent messages, current task.

### Useful existing behavior

- Tool policy already filters by registered tool names.
- Workspace context already gates on `run_command`.
- `memory_recent_days`, `max_history`, `max_iter`, `subconscious_activity` already exist as knobs.

Missing: a user-facing mode that sets those coherently, and registration gates for always-on builtins.

---

## 4. Goals

### In scope

1. Ship `agent.mode` ∈ {`chat`,`companion`,`full`} as the primary capability control.
2. Expand mode → effective tool set + prompt density + runtime bounds.
3. Ensure disabled tools fully vanish from model-visible surface.
4. Keep omitted / `full` behavior identical to today.
5. Teach modes in wizard and docs; keep per-tool maps as advanced escape hatch only.

### Out of scope

- Leading with a per-tool checklist in setup/README.
- A fourth mode for “full minus one heavy tool.”
- Multi-agent routing designs.
- Changing diary-as-memory product rules.
- Rewriting the ReAct loop beyond using existing bounds.
- Hot-reload of mode without agent restart.

---

## 5. Capability modes

### 5.1 User-facing meanings

| Mode | Meaning | Best for |
|---|---|---|
| `chat` | Conversation only. No tools. Lightest prompt. | Tiny local models that cannot tool-use reliably |
| `companion` | Conversation + memory. No shell / web / scheduler / skills action surface. | Small local models; default suggestion for local/custom providers |
| `full` | Current agent: all builtins + provider-gated extras. | Strong cloud models (default when unspecified) |

Wizard copy target:

```
How capable should this agent be?
  [1] chat       — conversation only (smallest local models)
  [2] companion  — conversation + memory (recommended for local models)
  [3] full       — full agent tools (default for strong models)
```

One question. Three answers.

### 5.2 Internal bundles

Bundles are internal vocabulary. Users do not configure them in v1.

| Bundle | Tools |
|---|---|
| `memory` | `write_memory`, `search_memory` |
| `workspace` | `run_command`, `attach_artifact` |
| `network` | `web_fetch`, `web_search` |
| `schedule` | `manage_scheduled_tasks` |
| `skills` | `read_skill` |
| `image` | `generate_image` |

| Mode | Bundles |
|---|---|
| `chat` | none |
| `companion` | `memory` |
| `full` | all bundles |

Provider / storage prerequisites still apply:

- `web_search` needs `search.provider != none`
- `generate_image` needs `image_generation.provider != none`
- `read_skill` needs skills storage

Mode never invents backends; it only allows bundles when backends exist.

### 5.3 Expansion table

Resolved once at agent init into an effective plan:

| Knob | `chat` | `companion` | `full` |
|---|---|---|---|
| Bundles | ∅ | memory | all |
| `prompt.core` | `minimal` | `essential` | `full` |
| `max_iter` | 2 | 6 | 50 |
| `max_history` | 8 | 12 | 32 |
| `memory_recent_days` | 0 | 1 | 2 |
| `subconscious_activity` | 0 | 0 | current default (0.02) |
| `prompt.relationships` | false | true | true |

### 5.4 Override precedence

```
explicit agent.max_iter / max_history / memory_recent_days / subconscious_activity
  > mode defaults

explicit prompt.core / prompt.relationships
  > mode defaults

explicit tools.<name>   # advanced
  > mode bundle expansion

search.provider / image_generation.provider / skills storage
  > still gate network / image / skills
```

Advanced escape hatch (not taught in wizard) — e.g. full without scheduler, or companion plus fetch:

```yaml
agent:
  mode: full
tools:
  manage_scheduled_tasks: false
```

```yaml
agent:
  mode: companion
tools:
  web_fetch: true
```

If `tools.*` appears without `mode`, treat base as `full` then apply overrides.

Status/debug surfaces show both `mode` and `effective_tools`.

---

## 6. Prompt density

Mode picks density by default. Explicit `prompt.core` can override.

| Density | Default for | Target size | Content |
|---|---|---|---|
| `full` | `full` mode | ~3000 chars | Current GOAL-complete core rules |
| `essential` | `companion` | ~1200–1600 | Independence + privacy + short attribution |
| `minimal` | `chat` | ~500–800 | Tiny-context last resort |

Every density must still preserve:

1. Independent subject — not user property.
2. Agent-owned memory; no casual leak of others’ private detail.
3. Speaker attribution markers exist; never mention them to users.
4. Language follows the current speaker.

Author real variants. Do not naive-truncate `full`.

### Layer injection rules

| Layer | Inject when |
|---|---|
| `tool_policy` | ≥1 tool registered |
| `workspace_context` | `run_command` registered |
| `skills_catalog` | `read_skill` registered |
| `relationship_context` | effective relationships enabled and cards non-empty |
| `recent_memory` | `memory_recent_days > 0` |

---

## 7. Config examples

### Local small model (taught path)

```yaml
provider:
  name: custom
  model: qwen2.5-7b
  base_url: http://127.0.0.1:11434/v1
  api_key: ollama

agent:
  mode: companion

search:
  provider: none

image_generation:
  provider: none
```

### Strong cloud model

Omit `agent.mode`, or set `mode: full`.

### Advanced override (not primary docs)

```yaml
agent:
  mode: full
  max_iter: 12
tools:
  manage_scheduled_tasks: false
```

### Validation

- `agent.mode` ∈ {`chat`,`companion`,`full`}; default `full` when absent.
- Advanced `tools` values must be bools for known tool names; unknown keys warn.
- `prompt.core` ∈ {`full`,`essential`,`minimal`} when present.

---

## 8. Runtime algorithm

```
mode = config.agent.mode or "full"
plan = expand_mode(mode)                         # bundles + prompt + bounds
plan = apply_explicit_overrides(plan, config)    # agent.* / prompt.* / tools.*
tool_names = plan.tools ∩ prerequisites(config)
register(create(tool_names))
apply plan bounds unless explicitly set on agent
```

Each turn:

```
core = select_core_prompt(plan.prompt_core)
tool_policy = build_policy(registered_tools)     # existing filter
workspace = only if run_command registered
skills_catalog = only if read_skill registered
```

---

## 9. Implementation plan

### Phase 1 — Mode → tools + bounds

1. Add `EffectiveAgentPlan` and `expand_mode()` (+ override merge).
2. Wire `_load_agent_tools` and memory-tool registration to the plan.
3. Gate skills catalog on `read_skill`.
4. Expose `mode` + `effective_tools` in status API / CLI.
5. Setup wizard: ask for mode only (3 choices).
6. Tests for each mode’s tool set and default bounds; no-mode == today.

### Phase 2 — Prompt densities

1. Author `essential` and `minimal` core prompts.
2. Select density from plan in `build_instruction_messages`.
3. Invariant tests (independence / privacy / attribution bullets present).
4. Honor explicit `prompt.core` override.

### Phase 3 — Polish

1. README “pick a mode” recipe for local models.
2. Effective-config display in web/CLI.
3. Optional UX: show one-line summary (“companion: memory only”).

### Deferred

- Extra modes or user-facing bundle checkboxes — only if three modes prove too coarse in practice.
- Per-tool checklist as default setup UX — not planned.

Suggested PR slice: Phase 1 → Phase 2 → Phase 3.

---

## 10. Testing

### Unit

- Mode expansion matrix (tools + bounds + prompt density) for all three modes.
- Explicit overrides beat mode defaults.
- Provider `none` still hides search/image under `full`.
- Skills catalog empty when mode has no `read_skill`.

### Integration

- `mode: chat` → no tools; no tool_policy / workspace / skills layers.
- `mode: companion` → only memory tools; essential core.
- omitted mode → same tool names and full core as today.

### Manual

Same scripted chat on a 7B–14B model across `full` / `companion` / `chat`; compare wrong-tool rate and runaway loops.

---

## 11. Compatibility

| Config | Behavior |
|---|---|
| No `agent.mode` | `full` — unchanged |
| Existing explicit `max_iter` / `max_history` / `memory_recent_days` | Win over mode defaults |
| `search.provider: none` | Unchanged |
| Legacy ignored `tools:` key | Becomes advanced bool override map; call out in changelog |

---

## 12. Risks

| Risk | Mitigation |
|---|---|
| Jump from `companion` to `full` feels large | Honest: action surface is one job; advanced `tools.*` can trim; wizard recommends `companion` for local |
| `minimal` weakens multi-user privacy | Keep mandatory bullets; default remains `full` |
| Users want “act but no scheduler” | Advanced override under `full`; do not add a fourth mode |
| Auto-suggesting `companion` for local providers surprises power users | Wizard default only; `full` still one pick away |

---

## 13. GOAL.md check

| Principle | Impact |
|---|---|
| Independent subject | Retained in all prompt densities |
| Multi-user distinction | Attribution grammar retained in `essential` / `minimal` |
| 1:1 and group coverage | Unchanged architecturally |
| Continuity | `companion` and `full` keep memory tools + diary injection |
| Unified memory | No per-user silos introduced |
| Agent-governed sharing | Boundary rules retained |
| Diary-only memory carrier | Unchanged |

---

## 14. Success criteria

1. A new user picks **one of three modes** and gets a coherent agent without learning tool names.
2. `companion` / `chat` measurably reduce registered tools and static instruction size vs `full`.
3. Omitting mode matches today’s tools and full core prompt.
4. Status surfaces show effective tools for debugging.
5. Setup and README never present a per-tool checklist as the primary path.

---

## 15. Open questions

1. Auto-suggest `companion` in the wizard when provider is `custom` / localhost? (Recommendation: yes.)
2. For `full`, should weak custom providers get a warning that tool-heavy mode may underperform? (Nice-to-have, not blocking.)
3. Later UI: mode dropdown first, advanced disclosure for `tools.*` — confirm when frontend settings land.

---

## 16. Expected file touch list

| File | Phase |
|---|---|
| `xagent/core/runtime/mode.py` (new, or equivalent) | 1 |
| `xagent/interfaces/base.py` | 1 |
| `xagent/core/agent.py` | 1 |
| `xagent/interfaces/cli/setup.py` | 1 |
| `xagent/interfaces/server/admin_routes.py` | 1 |
| `xagent/core/config.py` | 2 |
| `xagent/core/handlers/message.py` | 2 |
| `tests/` | 1–2 |
| `README.md` / docs | 3 |

---

## 17. Decision

Three modes only: **`chat` | `companion` | `full`**.  
They map to talk / remember / act.  
Expand behind the scenes into tools, prompt density, and runtime bounds.  
Default `full`. Teach modes; hide per-tool switches.

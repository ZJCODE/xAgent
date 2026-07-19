# Design: Agent Capability Mode

Status: Draft  
Branch: `cursor/tool-prompt-capability-design-c443`  
Scope: agent config, tool registration, prompt assembly  
Related product intent: [`GOAL.md`](../../GOAL.md)

---

## 1. Intent

xAgent should stay a full-capability digital individual when the model can carry that load.  
It should also be able to run **lighter** when the model cannot — especially local small models — without asking users to understand the internal tool inventory.

The user-facing decision is one word:

```yaml
agent:
  mode: companion   # chat | companion | assistant | full
```

The runtime expands that mode into:

- which tools are registered;
- how dense the core prompt is;
- how wide history / diary / iteration budgets are.

Disabled capabilities must disappear completely from the model’s view (no schema, no policy text, no dependent context layers).

---

## 2. Why this shape

### 2.1 The real failure mode

Today the agent always loads most builtins and injects a large instruction stack every turn. Strong cloud models tolerate that. Small local models often do not:

- too many tools → wrong calls, loops, empty turns;
- too much instruction text → identity / privacy / attribution rules get diluted;
- no first-class way to say “run lighter.”

### 2.2 Why not expose per-tool switches as the main UX

Atomic toggles (`run_command`, `manage_scheduled_tasks`, `web_fetch`, …) force users to learn:

- nine tool names;
- hidden couplings (skills catalog ↔ `read_skill`, workspace context ↔ `run_command`);
- provider gates for search / image.

Users think in jobs (“just chat”, “remember me”, “help me do things”), not in tool registries.  
So the product contract is **modes**. Tool sets are an expansion detail.

### 2.3 First principles

1. **Identity ≠ capability surface.** Independent subject / diary memory stay product invariants. Tool and prompt density are runtime choices.
2. **One user decision.** Prefer 3–4 named modes over many booleans.
3. **Disable means absent.** Off tools leave no schema, no policy, no orphan layers.
4. **Mode expands to a plan.** Tools + prompt density + runtime bounds resolve together.
5. **Default stays full.** Omitting `mode` preserves current behavior.
6. **Advanced overrides exist, but are not taught first.**
7. **No extra planners.** Reduce choice and text; do not add routing agents.

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

1. Ship `agent.mode` as the primary capability control.
2. Expand mode → effective tool set + prompt density + runtime bounds.
3. Ensure disabled tools fully vanish from model-visible surface.
4. Keep omitted/`full` behavior identical to today.
5. Teach modes in wizard and docs; keep per-tool maps as advanced escape hatch only.

### Out of scope

- Leading with a per-tool checklist in setup/README.
- Multi-agent “small model routes to big model” designs.
- Changing diary-as-memory product rules.
- Rewriting the ReAct loop beyond using existing bounds.
- Hot-reload of mode without agent restart.

---

## 5. Capability modes

### 5.1 User-facing meanings

| Mode | Meaning | Best for |
|---|---|---|
| `chat` | Conversation only. No tools. Lightest prompt. | Tiny local models |
| `companion` | Conversation + memory tools. No shell / web / scheduler. | Small local models that should keep continuity |
| `assistant` | Companion + workspace / files / network / skills / image when backends exist. **No scheduler.** | Mid-strength models |
| `full` | Current agent: all builtins + provider-gated extras. | Strong cloud models (default) |

Wizard copy target:

```
How capable should this agent be?
  [1] chat       — conversation only (best for small local models)
  [2] companion  — conversation + memory
  [3] assistant  — memory + workspace/files + web when configured
  [4] full       — all tools including scheduler (default for strong models)
```

One question. Four answers.

### 5.2 Internal bundles

Bundles are an internal vocabulary so expansion stays readable. Users do not configure bundles in v1.

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
| `assistant` | `memory` + `workspace` + `network` + `skills` + `image` |
| `full` | all bundles |

Provider / storage prerequisites still apply:

- `web_search` needs `search.provider != none`
- `generate_image` needs `image_generation.provider != none`
- `read_skill` needs skills storage

Mode never invents backends; it only allows bundles when backends exist.

Scheduler stays out of `assistant` on purpose: heaviest tool policy, common weak-model failure point. Users who need it choose `full` (or an advanced override).

### 5.3 Full expansion table

Resolved once at agent init into an effective plan:

| Knob | `chat` | `companion` | `assistant` | `full` |
|---|---|---|---|---|
| Bundles | ∅ | memory | memory + workspace + network + skills + image | all |
| `prompt.core` | `minimal` | `essential` | `essential` | `full` |
| `max_iter` | 2 | 6 | 12 | 50 |
| `max_history` | 8 | 12 | 16 | 32 |
| `memory_recent_days` | 0 | 1 | 2 | 2 |
| `subconscious_activity` | 0 | 0 | 0 | current default (0.02) |
| `prompt.relationships` | false | true | true | true |

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

Advanced escape hatch (not taught in wizard):

```yaml
agent:
  mode: companion
tools:
  web_fetch: true   # power-user override
```

If `tools.*` appears without `mode`, treat base as `full` then apply overrides.

Status/debug surfaces should show both `mode` and `effective_tools` so operators need not read expansion code.

---

## 6. Prompt density

Mode picks density by default. Explicit `prompt.core` can override.

| Density | Used by default for | Target size | Content |
|---|---|---|---|
| `full` | `full` mode | ~3000 chars | Current GOAL-complete core rules |
| `essential` | `companion`, `assistant` | ~1200–1600 | Independence + privacy + short attribution |
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
  mode: assistant
  max_iter: 8
prompt:
  core: minimal
tools:
  manage_scheduled_tasks: true
```

### Validation

- `agent.mode` ∈ {`chat`,`companion`,`assistant`,`full`}; default `full` when absent.
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
5. Setup wizard: ask for mode only.
6. Tests for each mode’s tool set and default bounds; no-mode == today.

### Phase 2 — Prompt densities

1. Author `essential` and `minimal` core prompts.
2. Select density from plan in `build_instruction_messages`.
3. Invariant tests (independence / privacy / attribution bullets present).
4. Honor explicit `prompt.core` override.

### Phase 3 — Polish

1. README “pick a mode” recipe for local models.
2. Effective-config display in web/CLI.
3. Optional UX: show bundle summary (“companion includes memory”).

### Deferred

- First-class user-facing bundle checkboxes — only if four modes prove too coarse.
- Per-tool checklist as default setup UX — not planned.

Suggested PR slice: Phase 1 → Phase 2 → Phase 3.

---

## 10. Testing

### Unit

- Mode expansion matrix (tools + bounds + prompt density).
- Explicit overrides beat mode defaults.
- Provider `none` still hides search/image under `assistant` / `full`.
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
| Four modes too coarse | Advanced `tools.*`; consider bundles later |
| `assistant` vs `full` unclear | Wizard blurbs + docs table; scheduler is the clearest divider |
| `minimal` weakens multi-user privacy | Keep mandatory bullets; default remains `full` |
| Auto-suggesting `companion` for local providers surprises power users | Suggest as wizard default, still easy to pick `full` |

---

## 13. GOAL.md check

| Principle | Impact |
|---|---|
| Independent subject | Retained in all prompt densities |
| Multi-user distinction | Attribution grammar retained in `essential` / `minimal` |
| 1:1 and group coverage | Unchanged architecturally |
| Continuity | `companion+` keep memory tools and diary injection |
| Unified memory | No per-user silos introduced |
| Agent-governed sharing | Boundary rules retained |
| Diary-only memory carrier | Unchanged |

---

## 14. Success criteria

1. A new user picks **one mode** and gets a coherent lighter/heavier agent without learning tool names.
2. `companion` / `chat` measurably reduce registered tools and static instruction size vs `full`.
3. Omitting mode matches today’s tools and full core prompt.
4. Status surfaces show effective tools for debugging.
5. Setup and README never present a per-tool checklist as the primary path.

---

## 15. Open questions

1. Auto-suggest `companion` in the wizard when provider is `custom` / localhost?
2. Should `assistant` include network automatically whenever a search provider is configured? (Recommendation: yes — mode allows; provider gates.)
3. Exact naming: keep job names (`chat` / `companion` / `assistant` / `full`) rather than size names (`lite` / `standard`)?
4. Later UI: mode dropdown first, advanced disclosure for overrides — confirm when frontend settings land.

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

Ship **capability mode** as the product control for lighter/heavier agents.  
Expand mode into tools, prompt density, and runtime bounds behind the scenes.  
Keep defaults at `full`. Teach modes; hide per-tool switches.

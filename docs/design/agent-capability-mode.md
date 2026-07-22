# Design: Agent Capability Mode

Status: Draft  
Branch: `cursor/tool-prompt-capability-design-c443`  
Scope: agent config, tool registration, prompt assembly  
Related product intent: [`GOAL.md`](../../GOAL.md)

---

## 1. Intent

Users should pick a mode that matches **how complex their usage scenario is**, not which internal tools they have memorized.

```yaml
agent:
  mode: companion   # chat | companion | full
```

The runtime expands that mode into tool registration, prompt density, and history / diary / iteration bounds.  
Capabilities that the scenario does not need must be **absent** from the model’s view (no schema, no policy, no orphan context layers).

Model strength is a constraint on which scenarios you can honestly serve — it is not the definition of the modes.

---

## 2. First principles: scenario complexity

### 2.1 What actually varies across real use

Ignore tool names. Ask what the *situation* demands of the agent.

| Dimension | Low | Mid | High |
|---|---|---|---|
| **Time horizon** | This message only | Across days / sessions | Across the calendar (later / on a clock) |
| **Continuity** | No durable self required | Same subject with memory & relationships | Same, plus commitments that outlive the turn |
| **Effect surface** | Words in the chat | Words + durable memory writes | Effects outside the transcript (files, web, shell, deliveries, jobs) |
| **Initiative** | Only replies when spoken to | Replies when spoken to; may recall | May act when due, not only when spoken to |

Social width (1:1 vs group) and channel count matter, but they are **orthogonal**: the same mode can cover 1:1 or group if attribution rules exist. They do not define a separate capability rung.

### 2.2 Natural scenario ladder

Stack the dimensions. Distinct rungs appear only where the user’s expectations jump.

**Scene A — 即时对话 (ephemeral dialogue)**  
User wants an answer or a short exchange *now*.  
No expectation of “remember last Tuesday,” no expectation of touching the world outside chat.  
Complexity: lowest.

**Scene B — 持续主体 (continuous subject)**  
User (or several users) treat the agent as the *same someone* over time.  
Diary, relationships, attribution, and cross-session continuity matter. Interaction is still mostly conversational presence.  
This is the lite form of [`GOAL.md`](../../GOAL.md)’s independent individual.  
Complexity: medium.

**Scene C — 世间行动 (world action)**  
The agent must produce effects outside the chat transcript: workspace files, shell, fetch/search, artifacts, skills, images — and, as part of real-world “getting things done,” time-bound work (reminders, scheduled turns).  
Initiative beyond the current turn belongs here: acting *when due* is still world action, not a separate human job called “cron mode.”  
Complexity: highest.

### 2.3 Why this collapses to exactly three modes

Map scenes → modes:

| Scene | Mode | One-line contract |
|---|---|---|
| A 即时对话 | `chat` | Talk now. Nothing else. |
| B 持续主体 | `companion` | Talk, and remain someone who remembers. |
| C 世间行动 | `full` | Talk, remember, and act in the world (including later). |

Two binary questions, from the scenario — not from the model card:

1. Does this usage need **continuity beyond the present turn**?  
   - No → `chat`  
   - Yes → continue
2. Does this usage need **effects outside dialogue** (including future/due work)?  
   - No → `companion`  
   - Yes → `full`

No other rung is load-bearing:

| Rejected split | Why it is not a primary scenario |
|---|---|
| “Full but no scheduler” | Users want *办事* or not; clock-driven work is part of 办事, not a separate lifestyle. Trim via advanced override if needed. |
| “Group mode” vs “1:1 mode” | Social width crosses scenes; it is policy/attribution, not a capability ladder. |
| “Multi-channel mode” | Channel is transport; the scene (talk / remember / act) stays the same. |
| Size names (`lite` / `standard`) | Describe quantity, not the usage contract. |

### 2.4 Where model capacity fits (secondary)

Modes describe **scenes**. Models constrain **which scenes you can serve well**:

- Weak local model in Scene C → poor tool routing; user should pick a simpler scene (`companion` / `chat`), not a fourth mode.
- Strong cloud model in Scene B → still `companion` if the user does not need world action; more model does not oblige more surface.

Wizard may *suggest* `companion` for local/custom providers because those deployments often intend Scene B — still a scenario default, not a model taxonomy.

### 2.5 Operating principles

1. **Modes track scenario complexity**, not tool inventory or prompt length accidents.
2. **Identity ≠ capability surface.** GOAL invariants stay; surface follows the scene.
3. **Disable means absent** from schema, policy, and dependent layers.
4. **One plan per mode** — tools + prompt density + bounds expand together.
5. **Default `full`** = today’s Scene C behavior when mode is omitted.
6. **Advanced `tools.*` overrides** exist; do not teach them as the primary path.
7. **No extra planners** to paper over scene/model mismatch.

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

Missing: a scenario-facing mode that sets those coherently, and registration gates for always-on builtins.

---

## 4. Goals

### In scope

1. Ship `agent.mode` ∈ {`chat`,`companion`,`full`} as the primary capability control, defined by scenario complexity.
2. Expand mode → effective tool set + prompt density + runtime bounds.
3. Ensure scene-unnecessary tools fully vanish from the model-visible surface.
4. Keep omitted / `full` behavior identical to today.
5. Teach modes as scenes in wizard/docs; keep per-tool maps as advanced escape hatch only.

### Out of scope

- Leading with a per-tool checklist.
- Extra modes for orthogonal axes (group, channel, “no scheduler”).
- Multi-agent routing designs.
- Changing diary-as-memory product rules.
- Rewriting the ReAct loop beyond using existing bounds.
- Hot-reload of mode without agent restart.

---

## 5. Capability modes

### 5.1 Scenario contracts

| Mode | Scene | User expectation | Best when |
|---|---|---|---|
| `chat` | 即时对话 | Reply in-thread; no durable agent life required | Disposable Q&A; or continuity not wanted |
| `companion` | 持续主体 | Same someone over time; memory/relationship matter; still conversation-first | Daily presence, diary continuity, multi-user talk without world side-effects |
| `full` | 世间行动 | May change or fetch things outside chat, including due-time work | Workspace, web, skills, reminders, scheduled agent turns |

Wizard copy (scene language first):

```
What kind of use is this agent for?
  [1] chat       — talk now only (no memory tools, no world actions)
  [2] companion  — ongoing someone who remembers (recommended for local everyday use)
  [3] full       — remember and act in the world, including scheduled work
```

### 5.2 Internal bundles

Bundles are internal. Users do not configure them in v1.

| Bundle | Tools | Scene role |
|---|---|---|
| `memory` | `write_memory`, `search_memory` | Continuity (Scene B+) |
| `workspace` | `run_command`, `attach_artifact` | World action |
| `network` | `web_fetch`, `web_search` | World action |
| `schedule` | `manage_scheduled_tasks` | World action over time |
| `skills` | `read_skill` | World action (procedural) |
| `image` | `generate_image` | World action |

| Mode | Bundles |
|---|---|
| `chat` | none |
| `companion` | `memory` |
| `full` | all bundles |

Prerequisites still apply:

- `web_search` → `search.provider != none`
- `generate_image` → `image_generation.provider != none`
- `read_skill` → skills storage present

Mode allows bundles; backends still gate availability.

### 5.3 Expansion table

| Knob | `chat` (Scene A) | `companion` (Scene B) | `full` (Scene C) |
|---|---|---|---|
| Bundles | ∅ | memory | all |
| `prompt.core` | `minimal` | `essential` | `full` |
| `max_iter` | 2 | 6 | 50 |
| `max_history` | 8 | 12 | 32 |
| `memory_recent_days` | 0 | 1 | 2 |
| `subconscious_activity` | 0 | 0 | current default (0.02) |
| `prompt.relationships` | false | true | true |

Bounds follow scene needs: ephemeral talk does not need long tool loops or diary injection; continuous subject needs some memory window; world action needs deeper iteration.

### 5.4 Override precedence

```
explicit agent.max_iter / max_history / memory_recent_days / subconscious_activity
  > mode defaults

explicit prompt.core / prompt.relationships
  > mode defaults

explicit tools.<name>   # advanced scene trim/extend
  > mode bundle expansion

search.provider / image_generation.provider / skills storage
  > still gate network / image / skills
```

Examples of advanced (not taught) trims:

```yaml
# Scene C without clock-driven tasks
agent:
  mode: full
tools:
  manage_scheduled_tasks: false
```

```yaml
# Scene B plus occasional URL read
agent:
  mode: companion
tools:
  web_fetch: true
```

If `tools.*` appears without `mode`, base as `full` then apply overrides.  
Status surfaces expose `mode` + `effective_tools`.

---

## 6. Prompt density

Density follows scene complexity by default.

| Density | Default mode | Target size | Why |
|---|---|---|---|
| `minimal` | `chat` | ~500–800 | Scene A does not need full life-policy text |
| `essential` | `companion` | ~1200–1600 | Scene B needs independence, privacy, attribution — not the full tool-era rhetoric |
| `full` | `full` | ~3000 | Scene C keeps today’s GOAL-complete core |

Invariants in every density:

1. Independent subject — not user property.
2. Agent-owned memory; no casual leak of others’ private detail.
3. Speaker attribution markers exist; never mention them to users.
4. Language follows the current speaker.

Author real variants; do not naive-truncate.

### Layer injection

| Layer | Inject when |
|---|---|
| `tool_policy` | ≥1 tool registered |
| `workspace_context` | `run_command` registered |
| `skills_catalog` | `read_skill` registered |
| `relationship_context` | relationships enabled and cards non-empty |
| `recent_memory` | `memory_recent_days > 0` |

---

## 7. Config examples

### Everyday local companion (Scene B — taught path)

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

### Full world-action agent (Scene C)

Omit `agent.mode`, or set `mode: full`.

### Ephemeral talk (Scene A)

```yaml
agent:
  mode: chat
```

### Validation

- `agent.mode` ∈ {`chat`,`companion`,`full`}; default `full` when absent.
- Advanced `tools` values: known names → bool; unknown keys warn.
- `prompt.core` ∈ {`full`,`essential`,`minimal`} when present.

---

## 8. Runtime algorithm

```
mode = config.agent.mode or "full"
plan = expand_mode(mode)                         # scene → bundles + prompt + bounds
plan = apply_explicit_overrides(plan, config)
tool_names = plan.tools ∩ prerequisites(config)
register(create(tool_names))
apply plan bounds unless explicitly set on agent
```

Each turn:

```
core = select_core_prompt(plan.prompt_core)
tool_policy = build_policy(registered_tools)
workspace = only if run_command registered
skills_catalog = only if read_skill registered
```

---

## 9. Implementation plan

### Phase 1 — Mode → tools + bounds

1. `EffectiveAgentPlan` + `expand_mode()` + override merge.
2. Wire `_load_agent_tools` and memory-tool registration.
3. Gate skills catalog on `read_skill`.
4. Status: `mode` + `effective_tools`.
5. Wizard: three scene choices.
6. Tests: per-mode tool set / bounds; no-mode == today.

### Phase 2 — Prompt densities

1. Author `essential` / `minimal` cores.
2. Select from plan in `build_instruction_messages`.
3. Invariant tests.
4. Explicit `prompt.core` override.

### Phase 3 — Polish

1. README framed as “pick your scene.”
2. Effective-config in web/CLI.
3. Optional one-line scene summary in UI.

### Deferred

- More modes for orthogonal axes — not planned.
- Per-tool checklist as default UX — not planned.

---

## 10. Testing

### Unit

- Expansion matrix for three scenes.
- Overrides beat mode defaults.
- Provider `none` hides search/image under `full`.
- Skills catalog empty without `read_skill`.

### Integration

- `chat` → no tools; no tool_policy / workspace / skills.
- `companion` → memory tools only; essential core.
- omitted mode → today’s tools + full core.

### Manual

Scripted Scene A/B/C chats; confirm surface matches scenario (no world tools in B, no memory tools in A).

---

## 11. Compatibility

| Config | Behavior |
|---|---|
| No `agent.mode` | `full` (Scene C) — unchanged |
| Explicit bounds already set | Win over mode defaults |
| `search.provider: none` | Unchanged |
| Legacy ignored `tools:` | Advanced bool overrides; changelog note |

---

## 12. Risks

| Risk | Mitigation |
|---|---|
| Scene B → C feels like a big jump | Correct: world action *is* a jump; advanced trim under `full` if needed |
| Users confuse “companion” with “weak model” | Docs/wizard use scene language first; model only as suggestion |
| `minimal` under-specifies privacy/attribution | Mandatory bullets in all densities |
| Subconscious/heartbeat only in `full` surprises | Documented as Scene C initiative; Scene B stays reactive |

---

## 13. GOAL.md check

| Principle | Impact |
|---|---|
| Independent subject | All densities; Scene B is the lite product expression |
| Multi-user distinction | Attribution retained; orthogonal to mode |
| 1:1 and group coverage | Not a mode axis |
| Continuity | Scene B/C keep memory + diary injection |
| Unified memory | Unchanged |
| Agent-governed sharing | Boundary rules retained |
| Diary-only carrier | Unchanged |

---

## 14. Success criteria

1. Users can choose a mode by answering “talk / remember / act,” without learning tool names.
2. Scene A/B measurably shrink model-visible surface vs Scene C.
3. Omitting mode matches today’s Scene C behavior.
4. Status shows effective tools for debugging.
5. Primary docs stay scene-centric.

---

## 15. Open questions

1. Wizard: when provider is `custom` / localhost, default selection `companion` (Scene B)? Recommendation: yes.
2. Warn when `full` is chosen with a clearly local/small setup? Nice-to-have.
3. Frontend: scene dropdown first; advanced `tools.*` disclosure later.

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

Define three modes from **usage scenario complexity**:

1. `chat` — 即时对话  
2. `companion` — 持续主体  
3. `full` — 世间行动（含到期主动）

Expand each scene into tools, prompt density, and bounds.  
Default `full`. Teach scenes; hide per-tool switches.  
Model capacity only constrains which scene you should pick — it does not define the ladder.

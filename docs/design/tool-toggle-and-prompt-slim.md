# Design: Capability Mode & Prompt Slimming

Status: Draft (revised)  
Branch: `cursor/tool-prompt-capability-design-c443`  
Scope: configuration + prompt assembly; no runtime rewrite of the agent loop  
Related product intent: [`GOAL.md`](../../GOAL.md)

---

## 0. Revision Note

Earlier draft led with **per-tool boolean toggles** as the primary config surface.  
That puts the wrong cognitive load on users: they must understand nine tool names, dependencies (`read_skill` ↔ skills catalog, `run_command` ↔ workspace context, search/image provider gates), and how those interact with prompt layers.

**Revised principle:** users choose a **capability mode** (one word).  
The runtime expands that mode into a tool set + prompt density + runtime bounds.  
Per-tool overrides exist only as an advanced escape hatch, not as the default mental model.

---

## 1. Problem

xAgent defaults to a **capable-model** runtime:

- Most built-in tools are always registered.
- Core rules, per-tool policies, identity, skills catalog, diary, relationship cards, and recent history are all injected every turn.
- Side loops (participation JSON, subconscious JSON, relationship updates) add more instruction-following load.

This works for strong cloud models. For local small models (and even mid-tier models under tight context), the failure mode is predictable:

1. Too many tools → wrong tool choice, loops, empty turns.
2. Too much instruction text → core rules get diluted; attribution / privacy / tool policy compete for attention.
3. Config cannot express “run lighter” today, except indirectly for `web_search` / `generate_image` via `provider: none`.

Two concrete gaps:

| Gap | Today |
|---|---|
| Capability closeability | `_load_agent_tools` always loads shell / scheduler / artifact / web_fetch; memory tools are hard-added in `Agent.__init__`; `tools` / `capabilities` config keys are ignored with a warning |
| Prompt slimness | One `BASE_AGENT_PROMPT` + all active tool policies; no density mode; skills catalog injects whenever skills storage exists |

A third, product gap: **even if we add knobs, exposing raw tool switches is a bad UX.** Users think in jobs (“just chat”, “remember me”, “full agent”), not in `manage_scheduled_tasks`.

---

## 2. First Principles

### P1. Capability must be selectable, not assumed

An agent’s *identity* and *memory ownership* are product invariants ([`GOAL.md`](../../GOAL.md)).  
Tool surface and prompt density are **runtime capabilities**, not identity. They must be configurable per agent without forking code.

### P2. What is disabled must disappear completely

Disabling a capability is not “return `{status: disabled}` when called.”  
If a tool is off after mode expansion:

- it must not appear in tool schemas sent to the model;
- its `TOOL_SYSTEM_PROMPTS` section must not be injected;
- dependent context layers must not be injected (e.g. workspace context requires `run_command`; skills catalog requires `read_skill`).

Anything left in the prompt after disable is still cognitive load and hallucinated affordance.

### P3. Prompt cost is a budget, not a bag of strings

Every injected layer competes for limited attention and context.  
Layers should be justified by a job. If a layer’s job is inactive, omit it. If the model is weak, prefer a **smaller correct subset** over a complete but ignored corpus.

### P4. One source of truth for “what is on”

After mode resolution, the registered tool set decides:

1. registration;
2. tool-policy text;
3. dependent context layers;
4. admin/status surfaces (`/api` capabilities, CLI help).

Do not maintain parallel allowlists in prompt builder vs tool loader.

### P5. Backward compatible by default

Missing mode config ⇒ today’s full behavior.  
Opt-in lighter modes only.

### P6. Modes are the user contract; tool sets are the expansion

**User-facing choice = one mode** (plus optional prompt density if needed).  
Internally, mode → `{tools, prompt.core, max_iter, max_history, memory_recent_days, subconscious_activity, …}`.

Per-tool maps are:

- an **implementation detail** of expansion;
- an **advanced override** for power users / debugging;
- **not** what setup wizards or docs lead with.

### P7. Prefer fewer, named capability bundles over many booleans

Humans reason about 3–4 named modes.  
They do not want a checklist of nine tools and their couplings.  
If a need cannot be expressed by modes, add a **bundle** (e.g. “web”, “workspace”) before exposing atomic tools.

### P8. Do not solve model weakness by adding more planners

No second agent, no auto-router network, no “smart” tool recommender in v1.  
Reduce choice and text first.

---

## 3. Current Architecture (relevant facts)

### 3.1 Tool loading

```
BaseAgentConfig._load_agent_tools()
  always: run_command, manage_scheduled_tasks, attach_artifact, web_fetch
  conditional: web_search (search.provider != none)
               generate_image (image_generation.provider != none)
               read_skill (skills_storage present)

Agent.__init__
  always append: write_memory, search_memory  (is_enabled=True hardcoded)
```

Key file: `xagent/interfaces/base.py` (`_load_agent_tools`).  
Key file: `xagent/core/agent.py` (memory tool binding).

If config contains `tools` or `capabilities`, they are **ignored** (warning only).

### 3.2 Prompt assembly (static instruction layers)

`MessageHandler.build_instruction_messages()` builds, in order:

1. `core_interaction_rules` ← `BASE_AGENT_PROMPT` (+ optional no-vision / subconscious notices)
2. `tool_policy` ← union of `TOOL_SYSTEM_PROMPTS` for **registered** tool names
3. `identity_context` ← `identity.md`
4. `workspace_context` ← only if `run_command` in tool names
5. `skills_catalog` ← up to `MAX_SKILLS_CATALOG_CHARS` (8000) if catalog non-empty

Soft cap: `MAX_SYSTEM_PROMPT_LENGTH = 16000` chars (warn only, not enforced).

### 3.3 Measured static text (approx. character counts)

| Block | Chars |
|---|---|
| `BASE_AGENT_PROMPT` | ~3043 |
| — Core identity | ~191 |
| — Self / memory rules | ~850 |
| — Boundary rules | ~568 |
| — Context / attribution rules | ~1302 |
| All tool policies combined | ~3703 |
| — `manage_scheduled_tasks` alone | ~1352 |
| — `run_command` | ~434 |
| Turn reply template | ~744 |
| Participation decision prompt | ~796 |
| Subconscious current-task template | ~2239 |

A full-capability agent can already spend **~7k+ static instruction chars** before identity, skills catalog, diary (≤8k), relationships, and recent messages.

### 3.4 What already respects tool names

Tool policy assembly **already filters** by active tool names.  
Workspace context already gates on `run_command`.

Missing:

- no user-facing mode that expands to a lighter tool set;
- skills catalog does **not** gate on `read_skill`;
- core prompt has no slim variant.

---

## 4. Goals & Non-Goals

### Goals (v1)

1. **One primary knob**: `agent.mode` with a small fixed set of modes users can understand.
2. Mode expands to tool registration + prompt density + runtime bounds.
3. Disabled tools (after expansion) vanish from model-visible surface.
4. Prompt density tied to mode by default; optional explicit override.
5. Preserve current default (`mode: full` / omit mode).
6. Advanced per-tool overrides allowed but **hidden** from default docs/wizard.

### Non-goals (v1)

- Leading UX with a per-tool checklist in setup or README.
- Multi-agent routing or “small model planner + big model executor.”
- Changing diary-as-memory carrier.
- Rewriting the ReAct loop itself.
- Hot-reload without agent restart.
- Per-turn dynamic tool menus.

---

## 5. Proposal A — Capability Modes (primary UX)

### 5.1 User-facing config

```yaml
agent:
  mode: full          # chat | companion | assistant | full
```

That is the sentence users need to learn.

| Mode | Plain-language meaning | Typical user |
|---|---|---|
| `chat` | Talk only. No tools. Lightest prompt. | Local tiny models; pure conversation |
| `companion` | Talk + remember (diary tools). No shell/web/scheduler. | Local small models that should keep continuity |
| `assistant` | Companion + workspace/files + optional web/fetch/skills when backends exist. No scheduler. | Mid models; “help me do things” without cron complexity |
| `full` | Everything today’s agent does (default). | Strong cloud models |

Setup wizard / README lead with these four words — **not** tool names.

### 5.2 Mode expansion table (normative)

Mode resolves once at agent init into an **effective plan**:

| Knob | `chat` | `companion` | `assistant` | `full` |
|---|---|---|---|---|
| Tool bundle | none | memory | memory + workspace + network\* + skills\* + artifacts | all builtins + provider-gated extras |
| `prompt.core` | `minimal` | `essential` | `essential` | `full` |
| `max_iter` | 2 | 6 | 12 | 50 (today) |
| `max_history` | 8 | 12 | 16 | 32 (today) |
| `memory_recent_days` | 0 | 1 | 2 | 2 (today) |
| `subconscious_activity` | 0 | 0 | 0 | today default (0.02) |
| `prompt.relationships` | false | true | true | true |

\*Network/skills/image in `assistant` / `full` still require existing provider / storage prerequisites (`search.provider`, `image_generation.provider`, skills storage). Mode never invents backends.

### 5.3 Tool bundles (internal vocabulary)

Users do not configure bundles directly in v1.  
Bundles exist so the expansion table stays readable and so a future UI can show “what this mode includes” without listing atomic tools first.

| Bundle | Tools |
|---|---|
| `memory` | `write_memory`, `search_memory` |
| `workspace` | `run_command`, `attach_artifact` |
| `network` | `web_fetch`, `web_search` (search still needs provider) |
| `schedule` | `manage_scheduled_tasks` |
| `skills` | `read_skill` |
| `image` | `generate_image` (needs provider) |

Mode → bundles:

| Mode | Bundles |
|---|---|
| `chat` | ∅ |
| `companion` | `memory` |
| `assistant` | `memory` + `workspace` + `network` + `skills` + `image` |
| `full` | all bundles |

Rationale for keeping **scheduler out of `assistant`**: it is the heaviest tool policy (~1352 chars) and a common failure point for weaker models. `full` keeps it.

### 5.4 Override precedence (keep advanced, bury it)

```
explicit agent.max_iter / max_history / memory_recent_days / subconscious_activity
  > mode defaults

explicit prompt.core / prompt.relationships
  > mode defaults

explicit tools.<name> (advanced)
  > mode bundle expansion

search.provider / image_generation.provider / skills storage
  > still gate network/image/skills regardless of mode
```

Advanced escape hatch (power users / tests only):

```yaml
agent:
  mode: companion

# Advanced — not shown in wizard. Overrides mode expansion for one tool.
tools:
  web_fetch: true
```

Rules:

- Docs and wizard **do not** teach `tools.*` first.
- Status API should expose both `mode` and `effective_tools` so debugging does not require reading expansion code.
- If only `tools.*` is set without `mode`, treat as `mode: full` then apply overrides (backward-friendly for experimenters).

### 5.5 Code ownership

| Step | Location | Change |
|---|---|---|
| Mode parse | `interfaces/base.py` or `core/runtime/mode.py` | Validate `agent.mode`; build `EffectiveAgentPlan` |
| Expand | same | Map mode → bundles → tool name set + prompt/runtime knobs |
| Apply overrides | same | Merge explicit `tools` / `agent.*` / `prompt.*` |
| Builtin construction | `_load_agent_tools` | Create only planned tools; stop ignoring `tools` as a dead key |
| Memory tools | `Agent.__init__` | Register only if in effective tool set |
| Skills catalog | `Agent._skills_catalog_context` | Empty unless `read_skill` registered |
| Status API | `admin_routes` | Return `mode` + `effective_tools` (+ optional `prompt.core`) |
| Setup / README | wizard + docs | Ask for mode, not tool checklist |

### 5.6 Deprecate the old warning path

Today `tools` / `capabilities` in config are ignored with a warning.  
After this design:

- `agent.mode` is the supported input;
- `tools` becomes an advanced override map of bools (not a free-form tool loader);
- `capabilities` remains **read-only derived status** in API responses.

### 5.7 Wizard copy (target UX)

```
How capable should this agent be?
  [1] chat       — conversation only (best for small local models)
  [2] companion  — conversation + memory
  [3] assistant  — memory + workspace/files + web when configured
  [4] full       — all tools including scheduler (default for strong models)
```

One question. Four answers. No tool taxonomy exam.

---

## 6. Proposal B — Prompt Slimming

Prompt slimming is **mostly driven by mode**. Explicit `prompt.*` remains an override, not the primary story.

### 6.1 Lever B1 — Structural omission (follows effective tools)

| Layer | Inject only when |
|---|---|
| `tool_policy` | ≥1 tool registered |
| `workspace_context` | `run_command` registered |
| `skills_catalog` | `read_skill` registered |
| `relationship_context` | effective `prompt.relationships` and cards non-empty |
| `recent_memory` | `memory_recent_days > 0` |

### 6.2 Lever B2 — Core prompt density

Core modules already exist as separate strings. Mode picks a density; override optional:

```yaml
prompt:
  core: full          # full | essential | minimal  (overrides mode default)
```

| Mode default | `prompt.core` |
|---|---|
| `chat` | `minimal` |
| `companion` / `assistant` | `essential` |
| `full` | `full` |

| Density | Intent | Target chars |
|---|---|---|
| `full` | Current GOAL-complete wording | ~3000 |
| `essential` | Independence + privacy + short attribution | ~1200–1600 |
| `minimal` | Tiny-context last resort | ~500–800 |

**Invariant across all densities:**

1. Independent subject (not user property).
2. Memory is agent-owned; do not dump private third-party detail.
3. Speaker attribution markers exist and must not be mentioned to users.
4. Language follows current speaker.

Author real variants; do not naive-truncate `full`.

### 6.3 Lever B3 — Runtime bounds travel with mode

Users who pick `companion` should not also need to know to lower `max_iter`.  
Mode sets sensible bounds (§5.2). Explicit `agent.*` still wins if set.

### 6.4 Optional later: tool policy verbosity

Only if people run many tools on weak models despite modes. Not v1.

### 6.5 What we will not slim without care

- Diary first-person / unified memory carrier.
- Attribution markers in stored history.
- Channel-specific instructions when the channel needs them.

---

## 7. Unified Config Surface

### 7.1 Happy path (what we teach)

Local small model:

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

Strong cloud model — omit `mode` or set `full`.

### 7.2 Advanced path (what we do not lead with)

```yaml
agent:
  mode: assistant
  max_iter: 8                 # override mode default
prompt:
  core: minimal               # override mode default
tools:
  manage_scheduled_tasks: true  # escape hatch: add scheduler onto assistant
```

### 7.3 Validation

- `agent.mode` ∈ {`chat`,`companion`,`assistant`,`full`}; default `full` when absent.
- Advanced `tools` map: known names → bool; unknown keys warn.
- `prompt.core` ∈ {`full`,`essential`,`minimal`} when present.
- Provider gates unchanged.

---

## 8. Assembly Algorithm (normative)

At agent init:

```
mode = config.agent.mode or "full"
plan = expand_mode(mode)                      # bundles + prompt + bounds
plan = apply_explicit_overrides(plan, config) # agent.*/prompt.*/tools.*
tools = create_all(plan.tool_names ∩ prereqs(config))
register(tools)
agent.max_iter / max_history / ... = plan.* unless explicitly set
```

Each chat turn:

```
tool_names = registered tools
core = select_core_prompt(plan.prompt_core)
tool_policy = build_tool_policy(tool_names)
workspace = ... if run_command in tool_names
skills = catalog if read_skill in tool_names else ""
```

---

## 9. Implementation Plan

### Phase 1 — Mode expansion + tool registration (highest leverage)

1. Add `EffectiveAgentPlan` + `expand_mode()` + override merge.
2. Wire `_load_agent_tools` / memory registration to plan.
3. Gate skills catalog on `read_skill`.
4. Status API: `mode` + `effective_tools`.
5. Tests for each mode’s tool set and default bounds.
6. README + wizard: mode question only.

### Phase 2 — Prompt densities wired to mode

1. Author `essential` / `minimal` core prompts.
2. `build_instruction_messages` selects by plan.
3. Snapshot / invariant tests.
4. Explicit `prompt.core` override.

### Phase 3 — Polish

1. Effective-config display in CLI/web.
2. Docs: “pick a mode” recipe for local models.
3. (Optional) show bundle summary in UI: “companion includes memory”.

### Explicitly deferred

- First-class user-facing bundle toggles (`capabilities: [memory, web]`) — only if four modes prove insufficient.
- Per-tool checklist in setup — **not planned** as default UX.

---

## 10. Testing Strategy

### Unit

- Each mode expands to expected tool set and bounds.
- Explicit overrides beat mode defaults.
- Provider `none` still hides search/image under `assistant` / `full`.
- Skills catalog empty when mode has no `read_skill`.

### Integration

- Temp agent config with `mode: chat` → `agent.tools` empty; instructions lack tool_policy / workspace / skills.
- `mode: companion` → only memory tools; essential core.
- No mode → identical to today’s tool names + full core.

### Manual

- Same scripted chat on a 7B–14B model: `full` vs `companion` vs `chat`; compare wrong-tool rate and loops.

---

## 11. Migration & Compatibility

| Existing config | Behavior |
|---|---|
| No `agent.mode` | `full` (unchanged) |
| Old ignored `tools:` | After Phase 1, bool maps become advanced overrides; changelog note |
| `search.provider: none` | Unchanged |
| `memory_recent_days` etc. already set | Explicit values win over mode defaults |

---

## 12. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Four modes not enough for some users | Advanced `tools.*` escape hatch; consider bundles later |
| Mode names confuse (assistant vs full) | Wizard plain-language blurbs; docs table |
| `minimal` weakens multi-user privacy | Mandatory bullets; default `full` for unspecified |
| Users still paste old per-tool checklists from earlier draft | This revision makes modes canonical; treat tool maps as advanced |
| Scheduler users stuck on weak models | They must choose `full` (honest about cost) or advanced override |

---

## 13. GOAL.md Check

| Principle | Impact |
|---|---|
| Independent subject | All core densities keep independence wording |
| Multi-user distinction | Attribution retained in `essential` / `minimal` |
| 1:1 and group coverage | Unaffected; participation prompt separate |
| Continuity | `companion+` keep memory tools + diary injection |
| Unified memory | No per-user silos |
| Agent-governed sharing | Boundary rules retained |
| Diary-only carrier | Unchanged |

---

## 14. Success Criteria

1. A new user can pick **one mode** and get a coherent lighter/heavier agent — without learning tool names.
2. `mode: companion` (or `chat`) measurably reduces registered tools and static instruction size vs `full`.
3. Default / omitted mode matches today’s tool set and full core prompt.
4. Status surfaces show effective tools for debugging without forcing users to configure them.
5. Setup/README do not present a per-tool checklist as the primary path.

---

## 15. Open Questions

1. **Exact mode names:** `chat` / `companion` / `assistant` / `full` vs `lite` / `standard` / `full`?  
   Recommendation: job-oriented names (`chat`/`companion`/…) over vague size names (`lite`).

2. **Does `assistant` include web by default when search provider is configured?**  
   Recommendation: yes — mode allows the bundle; provider still gates availability.

3. **Should wizard auto-suggest `companion` when provider is `custom` / localhost?**  
   Recommendation: yes, as default selection (user can still pick `full`).

4. **Expose bundles in UI later?**  
   Only if support load shows modes are too coarse; still prefer bundles over atomic tools.

5. **Frontend settings:** mode dropdown first; advanced disclosure for overrides.

---

## 16. Appendix — File Touch List (expected)

| File | Phase |
|---|---|
| `xagent/core/runtime/mode.py` (new) or equivalent | 1 — expand/override |
| `xagent/interfaces/base.py` | 1 — wire plan into tool load + agent knobs |
| `xagent/core/agent.py` | 1 — memory tools + skills catalog gate |
| `xagent/core/config.py` | 2 — essential/minimal prompts |
| `xagent/core/handlers/message.py` | 2 — core density selection |
| `xagent/interfaces/cli/setup.py` | 1 — mode question |
| `xagent/interfaces/server/admin_routes.py` | 1 — mode + effective_tools |
| `tests/` | 1–2 |
| `README.md` / this docs tree | 1–3 |

---

## 17. Decision Summary

1. **Primary UX is `agent.mode`** (`chat` | `companion` | `assistant` | `full`), not a per-tool switchboard.
2. Mode expands to tool bundles + prompt density + runtime bounds; disabled tools fully disappear from the model surface.
3. **Per-tool `tools.*` is an advanced override**, not the taught interface.
4. Defaults remain `full` / current behavior.
5. Ship mode→registration first; then prompt densities; keep user docs mode-centric end to end.

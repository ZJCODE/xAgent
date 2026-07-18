# Design: Tool Toggle & Prompt Slimming

Status: Draft  
Branch: `cursor/tool-prompt-capability-design-c443`  
Scope: configuration + prompt assembly; no runtime rewrite of the agent loop  
Related product intent: [`GOAL.md`](../../GOAL.md)

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
| Tool closeability | `_load_agent_tools` always loads shell / scheduler / artifact / web_fetch; memory tools are hard-added in `Agent.__init__`; `tools` / `capabilities` config keys are ignored with a warning |
| Prompt slimness | One `BASE_AGENT_PROMPT` + all active tool policies; no budget mode; skills catalog injects whenever skills storage exists, independent of whether `read_skill` is useful |

This document proposes a **first-principles** design for both, as one coherent capability surface.

---

## 2. First Principles

### P1. Capability must be selectable, not assumed

An agent’s *identity* and *memory ownership* are product invariants ([`GOAL.md`](../../GOAL.md)).  
Tool surface and prompt density are **runtime capabilities**, not identity. They must be configurable per agent without forking code.

### P2. What is disabled must disappear completely

Disabling a tool is not “return `{status: disabled}` when called.”  
If a tool is off:

- it must not appear in tool schemas sent to the model;
- its `TOOL_SYSTEM_PROMPTS` section must not be injected;
- dependent context layers must not be injected (e.g. workspace context requires `run_command`; skills catalog requires `read_skill`).

Anything left in the prompt after disable is still cognitive load and hallucinated affordance.

### P3. Prompt cost is a budget, not a bag of strings

Every injected layer competes for limited attention and context.  
Layers should be justified by a job:

| Layer | Job |
|---|---|
| Core rules | Who the agent is; privacy; attribution grammar |
| Tool policy | How to use **active** tools |
| Identity | Voice / persona continuity |
| Workspace | Only if shell tool is active |
| Skills catalog | Only if skill loading is active |
| Relationships / diary / recent experience | Continuity for *this* turn |
| Current task | What to do now |

If a layer’s job is inactive, omit it. If the model is weak, prefer a **smaller correct subset** over a complete but ignored corpus.

### P4. One source of truth for “what is on”

Tool enablement decides:

1. registration;
2. tool-policy text;
3. dependent context layers;
4. admin/status surfaces (`/api` capabilities, CLI help).

Do not maintain parallel allowlists in prompt builder vs tool loader.

### P5. Backward compatible by default

Existing agents with no new config must behave as today (full tool set, full core prompt).  
Opt-in slim / disable paths only.

### P6. Profiles are sugar; knobs are truth

A `lite` profile is allowed as convenience, but must expand to explicit knobs (`tools.*`, `prompt.*`, `agent.max_iter`, …).  
Debugging and overrides always operate on knobs.

### P7. Do not solve model weakness by adding more planners

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

Good news: tool policy assembly **already filters** by active tool names (`_build_tool_policy` / `TOOL_POLICY_ORDER`).  
Workspace context already gates on `run_command`.  

Missing pieces:

- no config → tool registration gate;
- skills catalog does **not** gate on `read_skill`;
- core prompt has no slim variant;
- no explicit prompt-budget controls for diary / relationships / history beyond existing `memory_recent_days` / `max_history`.

---

## 4. Goals & Non-Goals

### Goals (v1)

1. **Per-tool enable/disable** via agent `config.yaml`, including memory tools and always-on builtins.
2. **Prompt density controls**: slim core rules + omit inactive layers + optional tighter budgets.
3. Disabled tools vanish from model-visible surface (schema + policy + dependent layers).
4. Preserve current default behavior with zero config changes.
5. Document GOAL-check for identity / memory / multi-user invariants.

### Non-goals (v1)

- Multi-agent routing or “small model planner + big model executor.”
- Changing diary-as-memory carrier ([`GOAL.md`](../../GOAL.md) diary-only principle).
- Rewriting the ReAct loop itself (beyond using existing `max_iter` / `max_history` knobs).
- Hot-reload of tools mid-process without agent restart (restart-on-config remains OK).
- Per-turn dynamic tool menus (too much complexity for v1).

---

## 5. Proposal A — Tool Toggle

### 5.1 Config shape

Add a top-level `tools` map. Keys are **canonical tool names** as registered today:

```yaml
tools:
  run_command: true
  manage_scheduled_tasks: true
  attach_artifact: true
  web_fetch: true
  read_skill: true
  write_memory: true
  search_memory: true
  # Optional explicit overrides; if omitted, follow provider gates as today:
  web_search: true          # effective = this AND search.provider != none
  generate_image: true      # effective = this AND image_generation.provider != none
```

Semantics:

- Missing `tools` section → all builtins default **enabled** (today’s behavior), subject to existing provider gates for search/image.
- `tools.<name>: false` → tool is not created / not registered.
- `tools.<name>: true` → allow registration (still subject to provider/skills prerequisites).
- Unknown keys → warning + ignore (or hard error in strict validation; prefer warning in v1 to match existing config style).

### 5.2 Effective enablement matrix

| Tool | Prerequisites (all must pass) |
|---|---|
| `run_command` | `tools.run_command` ≠ false |
| `manage_scheduled_tasks` | `tools.manage_scheduled_tasks` ≠ false |
| `attach_artifact` | `tools.attach_artifact` ≠ false |
| `web_fetch` | `tools.web_fetch` ≠ false |
| `read_skill` | `tools.read_skill` ≠ false **and** skills storage available |
| `write_memory` | `tools.write_memory` ≠ false |
| `search_memory` | `tools.search_memory` ≠ false |
| `web_search` | `tools.web_search` ≠ false **and** `search.provider` ≠ `none` |
| `generate_image` | `tools.generate_image` ≠ false **and** `image_generation.provider` ≠ `none` |

Provider `none` remains the way to say “no search/image backend.”  
`tools.*.false` is the way to say “do not expose even if backend exists” or “strip builtins.”

### 5.3 Code ownership

| Step | Location | Change |
|---|---|---|
| Parse / normalize | `interfaces/base.py` (or small `core/tooling/config.py`) | Read `tools` map → `enabled_tools: set[str]` |
| Builtin construction | `_load_agent_tools` | Skip create_* when disabled; **stop ignoring** `tools` config (remove/repurpose the warning) |
| Memory tools | `Agent.__init__` | Pass enable flags; do not register when false (prefer not registering over `is_enabled` stub returns) |
| Policy / workspace | already OK | Driven by registered names |
| Skills catalog | `Agent._skills_catalog_context` | Return `""` unless `read_skill` registered |
| Status API / CLI help | `admin_routes`, chat help | Continue listing `agent.tools.keys()` (automatically correct) |

### 5.4 Deprecate the old warning path

Today:

```python
if "capabilities" in agent_cfg or "tools" in agent_cfg:
    self.logger.warning("Configured tools are ignored; ...")
```

Replace with real `tools` handling.  
`capabilities` remains a **read-only derived status** for API responses, not an input config for tool loading (avoid two write paths).

### 5.5 Interaction with `memory_tool.is_enabled`

`create_write_memory_tool(..., is_enabled=)` currently returns a disabled payload if called.  
For config toggles, **do not register** the tool at all when false. Keep `is_enabled` only for rare per-turn overrides if needed later; v1 config path = registration gate.

### 5.6 Recommended presets (documentation only in v1; optional profile sugar in v1.1)

**Chat-only (local small model):**

```yaml
tools:
  run_command: false
  manage_scheduled_tasks: false
  attach_artifact: false
  web_fetch: false
  read_skill: false
  write_memory: false
  search_memory: false
search:
  provider: none
image_generation:
  provider: none
```

**Memory companion (no shell / no scheduler):**

```yaml
tools:
  run_command: false
  manage_scheduled_tasks: false
  attach_artifact: false
  web_fetch: false
  read_skill: false
  write_memory: true
  search_memory: true
```

**Full** — omit `tools` section.

---

## 6. Proposal B — Prompt Slimming

Prompt slimming has three independent levers. Implement all three; they compose.

### 6.1 Lever B1 — Structural omission (free with Proposal A)

Once tools can be disabled, policy text and dependent layers shrink automatically.

Additional hard gates to add:

| Layer | Inject only when |
|---|---|
| `tool_policy` | ≥1 tool registered (already) |
| `workspace_context` | `run_command` registered (already) |
| `skills_catalog` | `read_skill` registered (**new**) |
| `relationship_context` | `prompt.relationships` ≠ false (new) **and** cards non-empty |
| `recent_memory` | `memory_recent_days > 0` (already) |

### 6.2 Lever B2 — Core prompt density mode

Split `BASE_AGENT_PROMPT` assembly by density. Core modules already exist as separate strings:

- `BASE_AGENT_CORE_IDENTITY`
- `BASE_AGENT_SELF_RULES`
- `BASE_AGENT_BOUNDARY_RULES`
- `BASE_AGENT_CONTEXT_RULES`

Config:

```yaml
prompt:
  core: full          # full | essential | minimal
```

| Mode | Includes | Intent |
|---|---|---|
| `full` (default) | All four modules + headers/footers | Current behavior; GOAL-complete wording |
| `essential` | Identity + compressed Self + compressed Boundary + **short** attribution cheat-sheet | Keep independent-subject + privacy + marker grammar; cut rhetoric |
| `minimal` | Identity one-liner + 5–8 bullet attribution/privacy lines | Last resort for tiny context models; document tradeoffs |

**Invariant:** Even `minimal` must preserve:

1. Independent subject (not user property).
2. Memory is agent-owned; do not dump private third-party detail.
3. Speaker attribution markers exist and must not be mentioned to users.
4. Language follows current speaker.

`essential` / `minimal` copy should be **authored variants**, not naive truncation of `full` (truncation destroys meaning).

Suggested size targets (static core only):

| Mode | Target chars |
|---|---|
| `full` | ~3000 (current) |
| `essential` | ~1200–1600 |
| `minimal` | ~500–800 |

### 6.3 Lever B3 — Turn-context budgets

Expose / tighten budgets that already exist as constants:

```yaml
prompt:
  core: full
  relationships: true           # inject relationship cards
  skills_catalog: auto          # auto | true | false  (auto = only if read_skill on)
  max_skills_catalog_chars: 8000
  # Optional v1.1 — wire constants that are today internal-only:
  # memory_recent_max_chars: 8000
  # relationship_max_cards: 4
```

Also document existing knobs users should pair with slim prompts:

```yaml
agent:
  max_history: 32          # lower to 8–12 for small models
  max_iter: 50             # lower to 6–8 when few tools
  memory_recent_days: 2    # 0 disables diary injection
  subconscious_activity: 0.02  # 0 disables subconscious
```

v1 does **not** require a new `max_iter` default change; documentation + optional profile is enough.

### 6.4 Lever B4 — Tool policy verbosity (optional, v1.1)

Scheduler policy alone is ~1352 chars. Optional:

```yaml
prompt:
  tool_policy: full       # full | short
```

`short` maps each tool to a 1–3 line policy. Only needed if users keep many tools on small models; Proposal A usually removes the need.

### 6.5 What we will not slim in v1 without explicit flags

- Per-channel instructions (Feishu/Weixin overlays) — keep; they are situationally necessary.
- Diary *content* quality / first-person perspective — product invariant.
- Removing attribution markers from stored history — would break multi-user principle.

---

## 7. Unified Config Surface

### 7.1 Full example

```yaml
provider:
  name: custom
  model: qwen2.5-7b
  base_url: http://127.0.0.1:11434/v1
  api_key: ollama

agent:
  max_history: 10
  max_iter: 6
  memory_recent_days: 1
  subconscious_activity: 0

tools:
  run_command: false
  manage_scheduled_tasks: false
  attach_artifact: false
  web_fetch: false
  read_skill: false
  write_memory: true
  search_memory: true

prompt:
  core: essential
  relationships: true
  skills_catalog: auto

search:
  provider: none

image_generation:
  provider: none
```

### 7.2 Optional profile sugar (v1.1)

```yaml
agent:
  profile: lite   # full | lite | chat
```

Expansion table:

| Profile | tools | prompt.core | max_iter | max_history | memory_recent_days | subconscious |
|---|---|---|---|---|---|---|
| `full` | all on (defaults) | `full` | 50 | 32 | 2 | default |
| `lite` | memory only | `essential` | 8 | 12 | 1 | 0 |
| `chat` | all off | `minimal` | 2 | 8 | 0 | 0 |

Rules:

- Explicit `tools.*` / `prompt.*` / `agent.*` overrides win over profile defaults.
- Profile is expanded once at agent init; runtime sees only concrete knobs.
- Setup wizard may offer profile selection when provider is `custom` / local URL.

v1 can ship **without** profiles if knobs + docs land first; profiles are UX.

### 7.3 Validation

In `BaseAgentConfig` validation (same style as `runtime.*` / `web.*`):

- `tools` must be a mapping of known names → bool.
- `prompt.core` ∈ {`full`,`essential`,`minimal`}.
- `prompt.skills_catalog` ∈ {`auto`,`true`,`false`} or bool.
- Conflicts: `prompt.skills_catalog: true` while `tools.read_skill: false` → warn and treat as false (P2: no orphan layers).

---

## 8. Assembly Algorithm (normative)

At agent init:

```
enabled = resolve_tool_enablement(config)   # Proposal A matrix
tools = [create(name) for name in builtin_order if name in enabled and prereqs(name)]
register memory tools iff enabled
```

Each chat turn when building instructions:

```
tool_names = registered tools
core = select_core_prompt(prompt.core)
tool_policy = build_tool_policy(tool_names)          # existing
identity = identity.md                               # unchanged
workspace = build_workspace if "run_command" in tool_names else ""
skills = catalog if skills_allowed(prompt, tool_names) else ""
return [core, tool_policy?, identity?, workspace?, skills?]
```

Turn context (unchanged structure, new gates):

```
relationships if prompt.relationships and cards
recent_memory if memory_recent_days > 0
recent_experience (max_history)
current_task
```

---

## 9. Implementation Plan

### Phase 0 — Spec & fixtures (this document)

- Land design doc.
- Agree config names and defaults.

### Phase 1 — Tool toggle (highest leverage)

1. Add `resolve_enabled_tools(config) -> set[str]` (+ unit tests for matrix / defaults / provider gates).
2. Wire `_load_agent_tools` and memory registration.
3. Gate skills catalog on `read_skill`.
4. Update setup comments / README snippet for local models.
5. Tests: disabled tool absent from `agent.tools` and from instruction tool_policy text; search/image still respect provider none.

### Phase 2 — Prompt core modes

1. Author `BASE_AGENT_PROMPT_ESSENTIAL` and `BASE_AGENT_PROMPT_MINIMAL` (or compose from slim module strings).
2. `prompt.core` selection in `build_instruction_messages`.
3. Snapshot tests for each mode’s included invariants (keyword / structural asserts, not brittle full-string equals only).
4. Document size targets and GOAL tradeoffs for `minimal`.

### Phase 3 — Prompt layer flags & budgets

1. `prompt.relationships`, `prompt.skills_catalog`.
2. Optional expose `max_skills_catalog_chars`.
3. Wizard / docs: recommended local-model bundle.

### Phase 4 — Profile sugar (optional)

1. `agent.profile` expansion.
2. Setup wizard choice when detecting custom/local provider.

### Suggested PR slicing

| PR | Content |
|---|---|
| PR1 | Phase 1 tool toggle + skills catalog gate + tests |
| PR2 | Phase 2 core prompt modes + tests |
| PR3 | Phase 3 flags + docs/wizard |
| PR4 | Phase 4 profiles |

---

## 10. Testing Strategy

### Unit

- Enablement matrix: each tool alone off; all off; defaults; search provider none vs tools.web_search true.
- Prompt mode selection returns expected modules.
- Skills catalog empty when `read_skill` disabled even if skills exist on disk.
- Tool policy omits scheduler text when scheduler disabled.

### Integration

- Init agent from temp config dir; assert `list(agent.tools)` and a built instruction blob.
- Chat turn with tools all false still replies (text-only path); `max_iter` respected.

### Regression

- No `tools` / no `prompt` section → bit-identical tool set to current defaults (names); core prompt equals `full`.

### Manual (local small model)

- Same 10-turn scripted chat on 7B–14B: full vs lite bundle; compare wrong-tool rate and runaway loops.

---

## 11. Migration & Compatibility

| Existing config | Behavior |
|---|---|
| No `tools` / `prompt` | Unchanged |
| Old ignored `tools:` list/map | After Phase 1, maps of bools become meaningful; document breaking change if anyone relied on the key being ignored |
| `search.provider: none` | Unchanged; still hides `web_search` |
| `memory_recent_days: 0` | Unchanged |

Changelog note required when `tools` stops being ignored.

---

## 12. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| `minimal` core weakens multi-user privacy | Keep mandatory bullets; GOAL-check in review; default remains `full` |
| Users disable `write_memory` and expect diary continuity from tools | Diary maintenance paths that do not use the tool remain; document that tool-off only removes *model-invoked* memory writes |
| Scheduler-less agents break user habits | Presets are opt-in; full default preserved |
| Two sources of truth (profile vs knobs) | Expand profile at init; persist expanded knobs in logs/status |
| Small models still fail JSON side loops | Out of v1 scope; document turning `subconscious_activity: 0` and participation policy separately |

---

## 13. GOAL.md Check

| Principle | Impact |
|---|---|
| Independent subject | Core modes must retain independence wording; tool toggle does not make the agent a “user-owned tool shell” |
| Multi-user distinction | Attribution cheat-sheet retained in `essential` / `minimal`; do not remove marker grammar |
| 1:1 and group coverage | Unaffected architecturally; group participation prompt is separate (future slim flag) |
| Environment-aware | Unaffected |
| Continuity | Slimming may reduce injected diary/history volume via existing knobs; continuity carrier remains diary |
| Unified memory | No per-user memory silos introduced |
| Agent-governed sharing | Boundary rules retained in all core modes |
| Diary-only memory carrier | No structured LTM schema introduced |

---

## 14. Success Criteria

1. An agent config can disable any builtin tool and the model never sees its schema or policy.
2. `prompt.core: essential|minimal` measurably reduces static instruction size toward targets in §6.2.
3. Default config path shows no behavior change in tool names and `full` core prompt.
4. Local-small-model recipe documented: tools subset + essential/minimal + lower `max_iter` / `max_history` / subconscious off.
5. Admin/CLI tool lists match registration (single source of truth).

---

## 15. Open Questions

1. **Should `minimal` be allowed in production wizards, or only documented for power users?**  
   Recommendation: document + profile `chat`; wizard default stays `full` / `lite`.

2. **Hard-error vs warn on unknown `tools.*` keys?**  
   Recommendation: warn in v1 (consistent with soft validation elsewhere); consider strict mode later.

3. **Persist profile expansion back into `config.yaml`?**  
   Recommendation: no; keep profile as input sugar, show effective config in status API.

4. **Disable subconscious / participation via `prompt.*` or keep `agent.subconscious_activity` only?**  
   Recommendation: keep existing agent knobs; do not duplicate under `prompt` in v1.

5. **Frontend settings UI for toggles?**  
   Follow backend knobs; UI can be a later PR once config schema is stable.

---

## 16. Appendix — File Touch List (expected)

| File | Phase |
|---|---|
| `xagent/interfaces/base.py` | 1 — load/validate tools + prompt config |
| `xagent/core/agent.py` | 1 — memory tool registration; skills catalog gate |
| `xagent/core/config.py` | 2 — essential/minimal prompt strings; maybe prompt defaults |
| `xagent/core/handlers/message.py` | 2–3 — core mode + layer flags |
| `xagent/interfaces/cli/setup.py` | 3–4 — comments / wizard |
| `tests/` | 1–3 — matrix + prompt assembly |
| `README.md` or `docs/` | 3 — local model recipe |

---

## 17. Decision Summary

1. **Tools become a first-class enablement map**; disable ⇒ unregister ⇒ no schema, no policy, no dependent layers.
2. **Prompts become a budgeted assembly**; density mode for core rules + omit inactive layers; reuse existing history/memory knobs.
3. **Defaults stay full**; lite paths are opt-in.
4. **Profiles are optional sugar** over knobs, not a second system.
5. **Ship tool toggle first**, then core prompt modes — that ordering maximizes benefit for local small models per unit of code.

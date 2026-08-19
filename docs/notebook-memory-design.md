# Notebook Memory Design (`notes`)

Status: accepted design, not yet implemented. No code has been written for this yet.

This document specifies a third memory section for xAgent, alongside the existing
time axis (diary) and person axis (relationship cards). It covers the four load-bearing
concerns: **write, organize, retrieve, inject**.

## 1. Intent

Give the agent a notebook, the way a person keeps one: a place for things it worked out
once and wants to reuse, rather than re-deriving them from a year of diary every time.

Three axes over one memory:

| Axis | Store | Question it answers |
| --- | --- | --- |
| Time | `MarkdownMemory` (daily/weekly/monthly/yearly) | What happened, and when? |
| Person | `RelationshipStore` (one card per person) | Who is this person to me? |
| Topic | `NoteStore` (this design) | What do I know, believe, or have concluded? |

The diary stays authoritative. The notebook is a **regenerable projection anchored to the
diary**, exactly as relationship cards already are (see the module docstring of
`xagent/components/memory/relationship_memory.py`). `GOAL.md` Principle 8 is amended in the
same change to name this pattern explicitly.

## 2. Boundaries

Overlap is the main design risk, so each boundary is stated as a rule, not a preference.

- **vs diary** — the diary is narrative ("what happened", append-only, immutable). A note is a
  conclusion ("what I take from it", revisable, topic-addressed). One event produces a diary
  entry always, and a note only when it yields something reusable.
- **vs relationship cards** — relational standing (closeness, trust, tone, open threads) stays
  in the card. A durable *fact or preference* about a person becomes a note only when it has
  cross-context reuse value, and then it carries `source.person` so attribution survives.
- **vs workspace** — `workspace/` holds working files and artifacts and is disposable. Notes are
  cognitive assets that participate in prompt injection.
- **vs skills** — a skill is an executable procedure with a `name`/`description` contract loaded
  via `read_skill`. A note is a small piece of knowledge; higher count, shorter life, no contract.

## 3. Zettelkasten: what we take, what we drop

Taken:

1. **Atomicity** — one note, one idea. Target 60–600 characters, hard cap 2000. This is the
   foundation: it makes retrieval precise and injection affordable.
2. **Immutable IDs** — timestamp IDs that never change. Titles may be rewritten freely without
   breaking links.
3. **Links over taxonomy** — no directory tree, no category hierarchy. A note reaching
   `permanent` must link to at least one existing note (the "no orphans" rule). Link traversal is
   a first-class retrieval action, not a convenience.
4. **Emergent structure** — when a cluster passes a threshold, gardening creates a `hub` note as
   its entry point. Structure is discovered, never predeclared.
5. **The agent's own words** — first-person, the agent's own phrasing, never a transcript excerpt.
   This is the only constraint that reliably stops notes from degrading into a copy of the chat log,
   and it matches the first-person principle in `GOAL.md`.

Dropped: manual fleeting/literature/permanent curation (gardening automates promotion), ID
genealogy (`1a1b`), unbounded growth (archival and decay are mandatory), and tags as a taxonomy
(tags are retrieval entry points under a controlled vocabulary, nothing more).

## 4. Data model

### 4.1 Storage layout

```
~/.xagent/memory/
  daily/ weekly/ monthly/ yearly/        # unchanged
  relationships/<channel>/<user_id>.md   # unchanged
  notes/
    202608190930-jun-espresso-ratio.md
    202608201400-hub-coffee.md
    .notes_index.json                    # derived: titles, tags, keys, links, backlinks
    .notes_usage.json                    # derived: hit counts, last used
    .notes_gardened                      # date stamp, at most one gardening pass per day
```

Notes are flat; IDs sort chronologically on their own. Year sharding is deferred until volume
demands it.

Both `.json` files are **derived and disposable** — rebuildable from the note files at any time.
Nothing that changes on read (usage counts) is written into a note file; that would destroy file
stability and make hand-editing and diffing painful.

There is deliberately **no note cursor**. Automatic distillation runs inside the existing diary
maintenance batch and reuses `_last_processed_message_id`, so a note can only ever be produced
from records that just became a diary entry. Diary anchoring is therefore structural, not a
convention someone has to remember.

### 4.2 File format

YAML frontmatter plus first-person body, following the `SKILL.md` precedent
(`_parse_frontmatter` in `xagent/components/skills/local.py`; `yaml` is already a dependency).
Frontmatter is chosen over the single `<!-- rel ... -->` metadata line used by relationship
cards because notes carry more fields and are meant to be read and edited by a human in the
Web UI Memory tab.

```markdown
---
id: 202608190930
title: Jun takes espresso at 1:2.5
kind: note
status: permanent
tags: [coffee, preference]
keys: [espresso, Jun, ratio]
links: [202607021145]
pinned: false
sensitivity: person-scoped
source:
  diary: [2026-08-19]
  person: feishu:ou_abc
  cursor: 18422
created: 2026-08-19
updated: 2026-08-19
---

Jun always wants espresso at 1:2.5 and 92C; anything thinner has "no spine" to him.
When I brew for him I just use that and stop asking.
```

| Field | Type | Notes |
| --- | --- | --- |
| `id` | 12-digit `YYYYMMDDHHMM` | immutable; collision resolves by incrementing the minute |
| `title` | string, <= 80 chars | freely rewritable |
| `kind` | `note` \| `hub` \| `ref` | atomic idea / cluster entry point / digest of an external source |
| `status` | `fleeting` \| `permanent` \| `archived` | lifecycle, see 6.1 |
| `tags` | list, <= 5 | controlled vocabulary, converged by gardening; also act as retrieval keys |
| `keys` | list, <= 5 | short trigger surfaces for auto-recall, see 7.1 |
| `links` | list of ids | at least one required to reach `permanent` |
| `pinned` | bool | always-injected; at most 3 effective, newest `updated` wins |
| `sensitivity` | `shareable` \| `person-scoped` \| `private` | see 4.3 |
| `source` | mapping | `diary` dates, `person` key, `cursor`, `url`, `tool` |
| `created` / `updated` | date | `updated` is the gardening and ranking input |

Filenames are `<id>-<slug>.md` where the slug is sanitized like `RelationshipStore._safe_segment`
does; a CJK-only title yields an empty slug and the file is simply `<id>.md`. The ID in the
frontmatter is authoritative, never the filename.

Inline `[[202607021145]]` links in the body are also scanned into the index, so hand-written
notes behave correctly, but frontmatter `links` is what the store writes.

The parser must tolerate human damage: missing fields fall back to defaults, and broken YAML
degrades to a body-only `fleeting` note rather than raising.

### 4.3 Sensitivity

This is the hook for the agent-governed sharing principle. The value is injected together with
the note so the model applies its own boundary rules; nothing is hard-filtered in code.

- `shareable` — general knowledge, fine to raise with anyone.
- `person-scoped` — belongs to one person's context via `source.person`; must not leak to others.
- `private` — the agent's own reflection; not volunteered to anyone.

Defaults: tool-written notes are `shareable` unless they carry `source.person`; distilled notes
are `person-scoped` when the batch had exactly one human participant, otherwise `shareable`.

## 5. Write

Three channels, one store.

### 5.1 Channel A — the agent writes deliberately (tools)

`write_note(title, body, tags, keys, links, sensitivity)` and `update_note(id, ...)`.
Tool descriptions carry the atomicity contract in the same register as the existing
`write_memory` tool ("Skip trivial or temporary notes"): one idea per note, own words, only
things worth reusing. Bodies over 2000 characters are rejected with an instruction to split,
which enforces atomicity mechanically instead of by persuasion.

Notes written this way start at `status: permanent` — a deliberate write is already curated.

**Duplicate guard, no LLM required.** Before creating, the tool runs a local neighbour search
(`score_text` over titles, tags, keys) using the incoming title and tags as terms. If the top
score clears a threshold, the tool does *not* create; it returns
`{"status": "similar_exists", "candidates": [...]}` and lets the model choose `update_note` or
confirm a genuinely new note. This costs nothing and prevents note explosion far better than
after-the-fact merging.

### 5.2 Channel B — background distillation

Runs inside `_run_maintenance_locked` in `xagent/core/handlers/memory.py`, immediately after
`_update_relationship_cards` and before the cursor commit, reusing the same `new_records`. One
additional LLM call (`JournalLLMService.distill_notes`) yields 0..N candidate notes at
`status: fleeting`.

Ordering inside the batch: diary write, then relationship cards, then note distillation, then
`_commit_processed_message_id`. Both projections are wrapped defensively exactly like the current
relationship code — a projection failure must never break the diary write.

Distillation is asked for restraint: most batches should produce zero notes. The prompt targets
durable, reusable conclusions and explicitly rejects transcript summary, which is the diary's job.

### 5.3 Channel C — the human edits

The Web UI Memory tree gains a `notes` directory (one-line change in
`AdminService._memory_scope_roots`). Users can edit, delete, pin, and link notes directly.
Format tolerance (4.2) exists for this channel.

### 5.4 The diary-trace rule

A note may hold knowledge that did not originate in the diary — a web search result, a tool
output, something the agent worked out on its own. When that happens, the note records
`source.url` or `source.tool`, **and the diary gets an entry recording that the agent wrote it
down**. The diary therefore remains the complete life stream even when it is not the origin of
every fact. This is the accommodation added to `GOAL.md` Principle 8 in the same change.

The invariant is "no note without a diary trace", not "notes are byte-reproducible from the
diary". A rebuild path exists (7.4 gardening run over a diary date range) and will recover
diary-derived notes; `ref` notes with external sources are not reproducible, and their diary
trace is what keeps them accountable.

## 6. Organize

### 6.1 Lifecycle

`fleeting` (distilled, unrefined) -> `permanent` (atomic, linked, gardening-confirmed) ->
`archived` (long unused; archived, never deleted, so the trail of thought survives).

### 6.2 Gardening

The mechanism that keeps the notebook from rotting. It rides the existing summary cadence
(`check_and_generate_summaries`, driven by the heartbeat), guarded by the same maintenance and
process locks, and runs at most once per day via `.notes_gardened`.

1. **Promote** — `fleeting` notes that were retrieved or linked within the TTL are rewritten by
   the LLM into atomic `permanent` notes; untouched ones go to `archived`.
2. **Link** — for each `permanent` note, find the top 3 neighbours and record them in `links`.
   This is where the no-orphans rule is enforced.
3. **Hub** — when a tag or cluster passes the threshold (5 permanent notes), create or update a
   `kind: hub` note whose body is a linked index of the cluster. Hubs are the backbone of
   injection (see 8).
4. **Converge tags** — merge synonymous tags, maintain the controlled vocabulary.
5. **Rebuild the index** — recompute `.notes_index.json` including backlinks from scratch.

## 7. Retrieve

Three layers, cheapest first.

### 7.1 L0 — automatic recall per turn, zero LLM, zero tokenizer

Enabled by default.

The obvious approach — extract query terms from the user's message — needs a tokenizer, and CJK
has no whitespace to split on. So the match runs **in reverse**: each note declares its own
trigger surfaces (`tags` + `keys`), and the current message is scanned for them. Concretely this
is `score_text(current_message, note_keys)` with the existing helper in
`xagent/utils/search_terms.py` — the note's keys play the role of "terms" and the message plays
the role of "text". No new scoring code, no tokenizer, and it works identically for Chinese and
English.

It also puts retrieval quality in the agent's hands: when it writes a note it declares how that
note should be found later, which is higher precision than any tokenizer would give us.

Ranking for the recalled set:

```
score = 2 * key_hits + 1 * body_term_hits + recency_bonus(updated) + pinned_bonus + w * log(1 + uses)
```

Ties break on `updated` descending. Hits increment `.notes_usage.json`, which feeds the next
turn's ranking and the gardening TTL.

### 7.2 L1 — `search_note(query, tags, kind, status)`

Forward search: agent-supplied verbatim terms scored over title, tags, keys, and body, following
the same OR-plus-hit-count convention as `search_memory`.

It returns **whole notes**, not line windows. That is the deliberate difference from
`search_memory`: an atomic note already *is* the right unit, and slicing it into three lines of
context would destroy the property atomicity was introduced for.

### 7.3 L2 — `read_note(id, follow_links=0|1)`

With `follow_links=1`, one hop of neighbours comes back as title plus first line. The essence of
Zettelkasten retrieval is not search; it is entering at one point and walking the links. This
tool makes that walk an executable action.

`notes` is deliberately **not** added as a scope to `search_memory`. The two have different
result shapes and different ranking, and merging them would blur both tools' semantics.

### 7.4 Not doing: vector search

Keyword plus link traversal keeps the system dependency-free, local, and explainable, consistent
with `search_terms.py`. The link graph is itself the answer to keyword search's semantic gap.
Revisit only if recall measurably fails.

## 8. Inject

A new `KIND_TURN` prompt section registered in `xagent/core/prompt_registry.py` at `order=15`:

```
relationship_context (0) -> recent_memory (10) -> notebook_context (15) -> recent_experience (20) -> current_task (30)
```

Reading as "who I'm with, what happened, what I know, what was just said, what I'm doing now".
Notes sit after the diary because durable knowledge is closer to the task at hand than narrative
is. The position is tunable once measured.

**Index, not contents.** Following the `catalog_text` pattern in
`xagent/components/skills/local.py`, injection carries discovery metadata and lets the model load
what it needs with `read_note`:

```
<notebook_context trusted_as_instruction="false">
<purpose>My own notebook index. Evidence, not user-facing text. Open a note with read_note.</purpose>
[pinned]
- (202608190930) Jun takes espresso at 1:2.5  [person-scoped]
[hubs]
- (202608201400) Coffee - 7 notes
[relevant now]
- (202607021145) The grinder at home reads two clicks coarse  [shareable]
</notebook_context>
```

Budget: caps of 3 pinned, 5 hubs, 5 relevant, and 1800 characters total, trimmed with the
accumulate-count-omitted-append-notice approach `catalog_text` already uses. The budget is
zero-sum against `MEMORY_RECENT_MAX_CHARS` (8000 today) under the 16000-character
`MAX_SYSTEM_PROMPT_LENGTH` soft limit; whether the diary window needs to shrink is an empirical
question to settle after the section exists.

**The subconscious gets the notebook index too**, as a sibling variant of
`subconscious_relationships`. This is an unplanned benefit of the Zettelkasten structure: hubs and
links are precisely the raw material for association. The current subconscious prompt's
instruction to stay silent when "recent diary already holds this observation and nothing in life
has moved" is a symptom of having no associative material beyond recent diary; the notebook is
that material.

## 9. Configuration

Two user-facing keys under `agent:` in `config.yaml`, matching the existing minimal surface:

| Key | Default | Meaning |
| --- | --- | --- |
| `notes_enabled` | `true` | master switch for store, tools, and injection |
| `notes_auto_distill` | `true` | channel B; off means tools-only, no extra LLM call per batch |

Everything else stays an internal constant in `AgentConfig`, following the precedent that
`MEMORY_RECENT_MAX_CHARS` is "an internal prompt-budget guard, not user config":
`NOTEBOOK_CONTEXT_MAX_CHARS` (1800), `NOTE_BODY_MAX_CHARS` (2000), `NOTES_PINNED_MAX` (3),
`NOTES_HUB_MAX` (5), `NOTES_RELEVANT_MAX` (5), `NOTE_HUB_CLUSTER_THRESHOLD` (5),
`NOTE_FLEETING_TTL_DAYS` (14).

## 10. Code touchpoints

| File | Change |
| --- | --- |
| `xagent/components/memory/note_memory.py` | new `Note` dataclass and `NoteStore`; layout and I/O only, policy lives upstream, mirroring the other two stores |
| `xagent/components/memory/__init__.py`, `xagent/components/__init__.py` | exports |
| `xagent/core/handlers/memory.py` | `get_notebook_context()`; `_distill_notes()` in `_run_maintenance_locked`; gardening in `_check_and_generate_summaries_locked` |
| `xagent/core/journal.py` | `distill_notes()` and `garden_notes()` plus their prompts |
| `xagent/core/config.py` | `NOTES_DIRNAME`, `NOTEBOOK_CONTEXT_NAME`, budget constants, template and builder |
| `xagent/core/prompt_registry.py` | register `notebook_context` and its subconscious variant |
| `xagent/tools/note_tool.py`, `xagent/tools/__init__.py` | `write_note`, `update_note`, `search_note`, `read_note` |
| `xagent/core/agent.py` | construct `NoteStore`, bind tools, fetch notebook context in `_build_turn_context` |
| `xagent/core/runtime/subconscious.py` | notebook injection for reflection turns |
| `xagent/interfaces/server/admin_service.py` | add `notes` to `_memory_scope_roots` |
| `xagent/interfaces/base.py`, `xagent/interfaces/cli/setup.py` | config key whitelist, validation, defaults, inline comments |
| `GOAL.md` | Principle 8 amendment (shipped with this document) |

Known trap: `tests/test_agent_config.py` asserts the **complete** `config["agent"]` dict in three
places. Any new config key breaks all three and they must be updated in the same change.

## 11. Testing plan

- `tests/test_note_memory.py` — frontmatter round-trip, tolerance for damaged files, ID collision
  and slug sanitizing, index and backlink rebuild, inline `[[id]]` scanning, archival.
- `tests/test_note_tool.py` — duplicate guard threshold behaviour, body cap rejection, key and tag
  caps, `read_note` link following.
- `tests/test_memory_handler.py` (extend) — notebook context ordering and budget trimming,
  distillation failure isolation (diary still commits), gardening runs at most once per day.
- `tests/test_agent_config.py` (extend) — the two new keys, plus the three exact-dict fixes above.

## 12. Goal-check

Mandatory under the `GOAL.md` requirement review rule.

- **Identity** — notes are first-person, in the agent's own words and judgment; reinforces the
  independent subject. Not a database participants can query at will.
- **Multi-user** — one notebook, never sharded per user. Person-linked knowledge is *attributed*
  via `source.person`, not isolated into per-user stores.
- **1:1 and group coverage** — notes are independent of conversation shape; injected identically
  in both.
- **Memory/journal perspective** — first person, attribution and uncertainty preserved; the
  distillation prompt reuses the existing `[speaker=ME]` marker rules.
- **Unified memory** — a single notebook; no per-user memory silos.
- **Agent-governed sharing** — `sensitivity` and `source.person` are injected with each note and
  the model decides; no hard-coded filtering.
- **Diary-anchored carrier** — notes are regenerable projections, `source` is mandatory, and the
  diary-trace rule (5.4) keeps the diary the complete life stream. Principle 8 is amended to name
  this pattern, which also brings `GOAL.md` in line with relationship cards, already shipped and
  already exactly this kind of projection.
- **Attribution and continuity** — immutable IDs and archive-never-delete make the notebook its
  own traceable record of how the agent's understanding evolved.

## 13. Phasing

- **M1 — the notebook exists.** `NoteStore`, the four tools, `notebook_context` injection with L0
  auto-recall on, `notes` in the Web UI tree, `notes_enabled`. Channel A and C only. Validates
  whether injected notes change behaviour before spending LLM budget on them.
- **M2 — it fills itself.** Channel B distillation inside the maintenance batch, plus gardening
  (promote, link, hub, tag convergence, index rebuild) and `notes_auto_distill`. Without this the
  notebook depends entirely on the model's initiative and will probably stay empty.
- **M3 — it stays healthy.** Decay and archival tuning, subconscious injection, the diary-range
  rebuild entry point, budget rebalancing between the diary window and the notebook index.

## 14. Risks and trade-offs

- **Note explosion and noise** is the biggest risk. Three defences: the pre-write neighbour check,
  `fleeting` by default with gardening promotion, and injecting only the index.
- **Semantic overlap with the diary**, degrading into "the diary written differently". Held off by
  three hard constraints — reusable conclusion only, atomic, must link — not by asking nicely in a
  prompt.
- **Cost.** Channel B adds one LLM call per maintenance batch and gardening one pass per day. Both
  are switchable, and M1 ships without either.
- **Injection budget is zero-sum.** The notebook's ~1800 characters may have to come out of
  `MEMORY_RECENT_MAX_CHARS`. Needs measurement, not a guess.
- **Gardening is an LLM rewriting the agent's own memory.** Promotion must preserve `id`,
  `created`, and `source`, and archival must never delete, so a bad pass is recoverable.

## 15. Decisions on record

1. **Notes may hold knowledge the diary did not originate** (external sources, tool output). Cost
   of admission: mandatory `source`, plus a diary trace that the note was written. Principle 8 is
   amended accordingly rather than the feature being bent around it.
2. **L0 auto-recall is on by default.** It is the highest-value and cheapest layer; reverse key
   matching (7.1) is what makes it free.
3. **Channel B ships**, but as M2 rather than M1, default on and switchable. Tools-only is a much
   smaller build, and would very likely leave the notebook empty.
4. **Naming**: directory `notes/`, store `NoteStore`, prompt section `notebook_context`, tools
   `write_note` / `update_note` / `search_note` / `read_note`.

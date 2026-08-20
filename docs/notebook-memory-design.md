# Notebook Memory Design (`notes`)

Status: implemented. Weekly distillation and mechanical monthly gardening ship; LLM rewrite,
synonym tag convergence, and unused-note decay remain deferred (see 13).

A third memory section alongside the existing time axis (diary) and person axis (relationship
cards), covering the four load-bearing concerns: **write, organize, retrieve, inject**.

## 1. Intent

Give the agent a notebook, the way a person keeps one: a place for things it worked out once and
wants to reuse, rather than re-deriving them from a year of diary every time.

Three axes over one memory:

| Axis | Store | Question it answers |
| --- | --- | --- |
| Time | `MarkdownMemory` (daily/weekly/monthly/yearly) | What happened, and when? |
| Person | `RelationshipStore` (one card per person) | Who is this person to me? |
| Topic | `NoteStore` | What do I know, believe, or have concluded? |

The diary stays authoritative. The notebook is a **regenerable projection anchored to the diary**,
exactly as relationship cards already are (see the module docstring of
`xagent/components/memory/relationship_memory.py`). `GOAL.md` Principle 8 was amended in the same
change to name this pattern.

## 2. Boundaries

Overlap is the main design risk, so each boundary is a rule, not a preference.

- **vs diary** — the diary is narrative ("what happened", append-only, immutable). A note is a
  conclusion ("what I take from it", revisable, topic-addressed). One event always produces a diary
  entry, and a note only when it yields something reusable.
- **vs relationship cards** — relational standing (closeness, trust, tone, open threads) stays in
  the card. A durable *fact or preference* about a person becomes a note only when it has
  cross-context reuse value, and then it carries `source.person` so attribution survives.
- **vs workspace** — `workspace/` holds working files and artifacts and is disposable. Notes are
  cognitive assets that participate in prompt injection.
- **vs skills** — a skill is an executable procedure with a `name`/`description` contract loaded via
  `read_skill`. A note is a small piece of knowledge; higher count, shorter life, no contract.

## 3. Zettelkasten: what we take, what we drop

Taken:

1. **Atomicity** — one note, one idea. Target 60–600 characters, hard cap 2000 enforced by the
   write tool. This is the foundation: it makes retrieval precise and injection affordable.
2. **Immutable IDs** — timestamp IDs that never change. Titles may be rewritten freely without
   breaking links, and the file keeps its original name so no path churns.
3. **Links over taxonomy** — no directory tree, no category hierarchy. Link traversal is a
   first-class retrieval action, not a convenience.
4. **Emergent structure** — hub notes are entry points into a cluster. The agent or user can create
   them with `write_note(kind="hub")`; monthly gardening also creates or refreshes hubs when a tag
   cluster crosses `NOTES_HUB_MIN_CLUSTER`.
5. **The agent's own words** — first-person, the agent's own phrasing, never a transcript excerpt.
   This is the only constraint that reliably stops notes from degrading into a copy of the chat log,
   and it matches the first-person principle in `GOAL.md`.
6. **Write later than capture** — the diary is the fleeting inbox. Permanent notes are written in a
   processing session (in-chat tools for standing facts, weekly background distillation, monthly
   gardening), not in the same diary maintenance batch that just recorded the day. Agents do not
   forget the way people do, so delaying the permanent note is nearly free and is a better filter
   for reuse.

Dropped: ID genealogy (`1a1b`), fleeting staging status (the diary already is the inbox), unbounded
growth (archive is mandatory, delete is not offered), and tags as a taxonomy (tags are retrieval
entry points, nothing more).

## 4. Data model

### 4.1 Storage layout

```
~/.xagent/memory/
  daily/ weekly/ monthly/ yearly/        # unchanged
  relationships/<channel>/<user_id>.md   # unchanged
  notes/
    202608190930-jun-espresso-ratio.md
    202608201400-hub-coffee.md
```

Notes are flat; IDs sort chronologically on their own. Year sharding is deferred until volume
demands it.

There are **no derived files on disk**. Notes are small, so the store scans the directory and keeps
parsed notes in memory behind a cheap fingerprint (names, mtimes, sizes from a single `scandir`, no
file reads). A changed, added, or removed note invalidates the cache, including edits made outside
the process. Nothing that changes on read is written back into a note file, so note files stay
stable, diffable, and safe to hand-edit.

There is deliberately **no note cursor**. Automatic distillation runs after a weekly summary is
written (the closed-week latch). Feedstock is that week's diary range, not the summary body; the
summary may orient the model but does not replace the diary. Diary anchoring is therefore
structural: no weekly file, no background-distilled notes for that week. An empty notebook until
the first completed week is a legal cold start.

### 4.2 File format

YAML frontmatter plus first-person body, following the `SKILL.md` / note precedent. Relationship
cards use the same frontmatter fence for human-readable Memory browsing; their schema stays small
(`key`, optional `name`, `updated`) because the path already encodes channel/user_id.

```markdown
---
id: '202608190930'
title: Jun takes espresso at 1:2.5
kind: note
status: active
tags:
- coffee
- preference
keys:
- espresso
- Jun
sensitivity: person-scoped
source:
  diary:
  - '2026-08-19'
  person: feishu:ou_abc
  cursor: 18422
created: '2026-08-19'
updated: '2026-08-19'
---

Jun always wants espresso at 1:2.5 and 92C; anything thinner has "no spine" to him.
When I brew for him I just use that and stop asking.
```

| Field | Type | Notes |
| --- | --- | --- |
| `id` | 12-digit `YYYYMMDDHHMM` | immutable; collision resolves by walking forward a minute |
| `title` | string, <= 80 chars | freely rewritable |
| `kind` | `note` \| `hub` \| `ref` | atomic idea / cluster entry point / digest of an external source |
| `status` | `active` \| `archived` | archive never deletes |
| `tags` | list, <= 5 | reusable topic labels; also act as recall triggers |
| `keys` | list, <= 5, min 2 chars each | recall triggers, see 7.1 |
| `links` | list of ids | related notes; inline `[[id]]` in the body is also indexed |
| `pinned` | bool | always injected with its body; at most 3 effective, newest `updated` wins |
| `sensitivity` | `shareable` \| `person-scoped` \| `private` | see 4.3 |
| `source` | mapping | `diary` dates, `person` key, `cursor`, `url`, `tool` |
| `created` / `updated` | date | `updated` breaks ranking ties |

Filenames are `<id>-<slug>.md`, sanitized the way `RelationshipStore._safe_segment` does it; a
CJK-only title yields no slug and the file is simply `<id>.md`. The frontmatter id is authoritative,
never the filename, and rewriting a title does not rename the file.

Ids are allocated and written under a single lock (`NoteStore.create`). Allocating first and writing
afterwards lets concurrent tool calls in the same minute all claim the same id and produce several
files claiming to be one note, which would make `read_note` arbitrary and links ambiguous.

The parser tolerates human damage: unknown enum values and oversized fields are clamped, missing
fields fall back to defaults, and broken or absent YAML degrades to a body-only note (id recovered
from the filename, title from the first body line) rather than raising or dropping the note.

### 4.3 Sensitivity

The hook for the agent-governed sharing principle. The value is injected together with the note so
the model applies its own boundary rules; nothing is hard-filtered in code.

- `shareable` — general knowledge, fine to raise with anyone.
- `person-scoped` — belongs to one person's context via `source.person`; must not leak to others.
- `private` — the agent's own reflection; not volunteered to anyone.

Defaults: tool-written notes are `shareable` unless the model says otherwise; weekly-distilled notes
default to `shareable` (a week usually spans more than one person). In-chat `write_note` may still
set `person-scoped` or `private` explicitly.

## 5. Write

Three ways into one store.

### 5.1 In chat — the agent writes with tools

`write_note(title, body, keys, tags, links, sensitivity, kind)` and
`update_note(note_id, title, body, keys, tags, links, sensitivity, pinned, archive)`. Only the
fields passed to `update_note` change. Tool descriptions carry a tight contract: write only when
**this turn** produced a standing fact that will still hold across days (preference, constraint,
decision and what it turned on, approach that worked). Do not summarise the conversation and do not
guess what might be useful later — the diary and weekly background distillation cover post-hoc
conclusions. Prefer linking related notes at write time. Bodies over 2000 characters are rejected
with an instruction to split, which enforces atomicity mechanically instead of by persuasion.

**Duplicate guard, no LLM required.** Before creating, the tool asks the store for near neighbours
and scores each with `NoteStore.identity_score` (3× title, 2× keys and tags, body deliberately
excluded — a note that merely mentions the topic in passing is not another version of it). If the top
score clears `NOTES_DUPLICATE_SCORE_THRESHOLD`, the tool does *not* create; it returns
`{"status": "similar_exists", "candidates": [...]}` and lets the model choose `update_note` or
confirm a genuinely new note. This costs nothing and prevents note explosion far better than
after-the-fact merging. Weekly background distillation applies the same threshold, so a draft the
agent would have been told to fold into an existing note is not quietly written by the background
path instead.

`source.diary` is set to the day the note was written. Tool-written notes get no `source.person`:
the tool has no per-turn context, the same limitation the existing `write_memory` tool has.

### 5.2 Background — weekly distillation

Runs after `_generate_weekly` successfully writes the weekly summary file (heartbeat / summary
cadence), not inside diary maintenance and **not** via the `write_note` tool: the handler calls the
distillation LLM and then `NoteStore.create` directly. Idempotency is free: if the weekly file
already exists the generator does not re-enter, so distillation for that week does not re-run.
Distillation failure is best-effort and never rolls back the weekly summary.

The weekly file is only the **processing-session latch**. The LLM feedstock is the **week's diary
range** already fetched for `generate_summary` (`search_date_range`). The just-written weekly
summary may be passed as a short **week arc for orientation only** — not as the sole upstream and
not as a source of new facts — so notes do not become a mini rewrite of the weekly report. Waiting
until the week is closed is the recurrence filter; the diary text keeps atomic detail the summary
would have compressed away. Long weeks are soft-truncated to the same order as
`DEFAULT_JOURNAL_SOURCE_CHARS` (tail kept).

One additional LLM call (`JournalLLMService.distill_notes`) yields at most
`NOTES_DISTILL_MAX_PER_WEEK` (6) drafts. The prompt is told most weeks deserve zero notes, and that
an empty list is the normal correct answer. Existing notes are listed with `id`, title, keys, tags,
and a short snippet so the model can decline to restate them and can propose `links` by id.

Each draft is duplicate-guarded, then written as `active` with:

- `source.diary` = `[week_start, week_end]`
- default `sensitivity: shareable`
- `links` = model-proposed ids (validated against known notes) merged with up to
  `NOTES_MECHANICAL_LINK_MAX` neighbours whose `identity_score` is at least
  `NOTES_LINK_SCORE_THRESHOLD` but below the duplicate threshold

No `fleeting` status: the diary is the inbox; delaying background distillation until the weekly
file exists is the processing gap Zettelkasten requires.

### 5.3 By hand — the human edits

The Web UI Memory tree includes the `notes` directory (`AdminService._memory_scope_roots`). Users can
edit, delete, pin, and link notes directly. The format tolerance in 4.2 exists for this path.

### 5.4 Provenance instead of a diary trace

An earlier draft required that anything the diary did not originate (a web result, a tool output)
also produce a diary entry recording that a note was written. That was dropped: it would fill the
diary with bookkeeping about the agent's own memory. `source` on the note carries the same
accountability without the noise, and Principle 8 now asks for provenance rather than a diary trace.

The invariant is therefore "every note records where it came from", not "notes are reproducible from
the diary". Notes from weekly background distillation are diary-derived from that week's diary
range (triggered when the weekly file is written); notes the agent writes with tools record the day
it wrote them; `ref` notes record `source.url` or `source.tool`.

## 6. Organize

### 6.1 Lifecycle

`active` → `archived`. Archiving keeps the file and stops the note being recalled, searched by
default, or injected, so the trail of thought survives a note stopping being true.

### 6.2 Gardening

Mechanical gardening runs after `_generate_monthly` successfully writes the monthly summary, under
the same `notes_auto_distill` switch (off means tools-only: no weekly distill, no monthly garden).
Best-effort: never rolls back the monthly file. No extra LLM call.

Implemented:

1. **Orphan links** — active notes with empty `links` and no backlinks get 1–2 neighbours from
   `find_similar` / `identity_score` (≥ `NOTES_LINK_SCORE_THRESHOLD`). Body is not rewritten.
2. **Hubs** — when a tag has at least `NOTES_HUB_MIN_CLUSTER` (4) active non-hub notes and no hub
   already claims that tag in title/keys/tags, create a `kind: hub` note whose body lists members as
   `[[id]] title` and whose `links` point at them. An existing hub for that tag is refreshed instead
   of duplicated.

Still deferred: LLM rewrite of raw notes, synonym tag convergence, and unused/unlinked decay (no
read counters; nothing that changes on read is written back — see 4.1). The subconscious reflection
turn still has no tools; it reads the notebook for association, while write-back stays on the
weekly/monthly processing path.

## 7. Retrieve

Three layers, cheapest first.

### 7.1 L0 — automatic recall per turn, zero LLM, zero tokenizer

Enabled by default; this is the layer that makes the notebook worth injecting.

The obvious approach — extract query terms from the user's message — needs a tokenizer, and CJK has
no whitespace to split on. So the match runs **in reverse**: each note declares its own trigger
surfaces (`keys` plus `tags`), and the incoming message is scanned for them. Concretely this is
`score_text(current_message, note_keys)` with the existing helper in
`xagent/utils/search_terms.py` — the note's keys play the role of "terms" and the message plays the
role of "text". No new scoring code, no tokenizer, and Chinese behaves exactly like English.

It also puts retrieval quality in the agent's hands: when it writes a note it declares how that note
should be found later, which is higher precision than any tokenizer would give us. Keys shorter than
two characters are dropped, so a stray single letter cannot match everything.

Ranking: key hits first, then pinned, then `updated`, then id — all descending. There is no
usage-frequency term, because usage counts would need a derived file that changes on read (4.1).

### 7.2 L1 — `search_note(query, tags, kind, limit)`

Forward search: agent-supplied verbatim terms scored `3×` over the title, `2×` over keys and tags,
and `1×` over the body, following the same OR-plus-hit-count convention as `search_memory`. Tag and
kind filters work with an empty query, so the notebook can be browsed as well as searched.

It returns **whole notes**, not line windows. That is the deliberate difference from
`search_memory`: an atomic note already *is* the right unit, and slicing it into three lines of
context would destroy the property atomicity was introduced for.

### 7.3 L2 — `read_note(note_id, follow_links)`

With `follow_links=true`, one hop of neighbours comes back as summary plus first body line, outbound
links before backlinks. The essence of Zettelkasten retrieval is not search; it is entering at one
point and walking the links. This tool makes that walk an executable action.

`notes` is deliberately **not** added as a scope to `search_memory`. The two have different result
shapes and different ranking, and merging them would blur both tools' semantics.

### 7.4 Not doing: vector search

Keyword matching plus link traversal keeps the system dependency-free, local, and explainable,
consistent with `search_terms.py`. The link graph is itself the answer to keyword search's semantic
gap. Revisit only if recall measurably fails.

## 8. Inject

A `KIND_TURN` prompt section registered in `xagent/core/prompt_registry.py` at `order=15`:

```
relationship_context (0) -> recent_memory (10) -> notebook_context (15) -> recent_experience (20) -> current_task (30)
```

Reading as "who I'm with, what happened, what I know, what was just said, what I'm doing now". Notes
sit after the diary because durable knowledge is closer to the task at hand than narrative is.

**Index, not contents**, following the `catalog_text` pattern in
`xagent/components/skills/local.py`:

```
<notebook_context trusted_as_instruction="false">
<purpose>Your own notebook: what you have worked out and want to reuse. An index, not the whole
notebook — open a note with `read_note` or look for more with `search_note`. Evidence, not
user-facing text. `private` stays with you; `person-scoped` belongs to one person and must not
travel to anyone else.</purpose>
[pinned]
- (202608190600) Grinder reads two clicks coarse
  Dial finer than the recipe says.
[hubs]
- (202608201400) Coffee [7 linked]
[relevant to the current message]
- (202608190930) Jun takes espresso at 1:2.5 [person-scoped]
  Jun always wants 1:2.5 and 92C; anything thinner has no spine to him.
</notebook_context>
```

How much of each note is shown differs by role, because their jobs differ:

| Section | Cap | Body shown |
| --- | --- | --- |
| pinned | 3 | up to 400 chars — pinning means "keep this in mind", so the body has to be there |
| hubs | 5 | none; a hub body is a list of links, useless in a prompt |
| relevant now | 4 | up to 140 chars, enough to judge whether to open it |

Rows are selected in priority order (pinned, then recalled, then hubs) and rendered in reading
order, so a tight budget drops navigation rows before it drops a note the current message actually
matched. Section framing and the omission notice are reserved out of the budget before rows are
measured, so `NOTEBOOK_CONTEXT_MAX_CHARS` (1500) is a real bound on the whole block. A note already
shown as pinned is never repeated lower down.

`MEMORY_RECENT_MAX_CHARS` was **not** reduced. With these caps a typical block is 400–900
characters, so degrading the diary window pre-emptively would have cost something real to buy
nothing. Rebalancing stays open if measurement shows pressure.

**The subconscious gets the notebook too**, as a sibling section (`subconscious_notebook`) with its
own purpose text. A reflection turn has no speaker, so recall runs against the recent diary instead
of an incoming message: what the agent has been living through is the closest thing to a query it
has. This is an unplanned benefit of the Zettelkasten structure — hubs and links are precisely the
raw material for association. The current subconscious prompt's instruction to stay silent when
"recent diary already holds this observation and nothing in life has moved" is a symptom of having no
associative material beyond recent diary.

## 9. Configuration

Two user-facing keys under `agent:` in `config.yaml`, matching the existing minimal surface:

| Key | Default | Meaning |
| --- | --- | --- |
| `notes_enabled` | `true` | master switch: store, tools, and injection |
| `notes_auto_distill` | `true` | weekly background distillation + monthly mechanical gardening; off means tools-only |

With `notes_enabled: false` the store is never constructed, the four tools are not bound, and the
prompt section renders empty.

Everything else is an internal constant in `AgentConfig`, following the precedent that
`MEMORY_RECENT_MAX_CHARS` is "an internal prompt-budget guard, not user config":
`NOTEBOOK_CONTEXT_MAX_CHARS` (1500), `NOTEBOOK_PINNED_MAX` (3), `NOTEBOOK_HUB_MAX` (5),
`NOTEBOOK_RELEVANT_MAX` (4), `NOTEBOOK_PINNED_BODY_MAX_CHARS` (400),
`NOTEBOOK_SNIPPET_MAX_CHARS` (140), `NOTES_DISTILL_MAX_PER_WEEK` (6),
`NOTES_DISTILL_CONTEXT_NOTES` (30), `NOTES_DUPLICATE_SCORE_THRESHOLD` (3),
`NOTES_LINK_SCORE_THRESHOLD` (2), `NOTES_MECHANICAL_LINK_MAX` (2), `NOTES_HUB_MIN_CLUSTER` (4),
and the schema caps in `note_memory.py` (`MAX_BODY_CHARS` 2000, `MAX_TITLE_CHARS` 80,
`MAX_TAGS`/`MAX_KEYS` 5).

## 10. Code touchpoints

| File | Change |
| --- | --- |
| `xagent/components/memory/note_memory.py` | new `Note` dataclass and `NoteStore`; layout and I/O only, policy lives upstream, mirroring the other two stores |
| `xagent/components/memory/__init__.py`, `xagent/components/__init__.py` | exports |
| `xagent/core/handlers/memory.py` | `get_notebook_context()`, `_render_notebook_sections()`, `_distill_notes_from_weekly()`, `_garden_notes_after_monthly()` |
| `xagent/core/journal.py` | `distill_notes()` over a week's diary (optional week-arc orientation) plus prompts and draft parsing (including `links`); `_strip_code_fence` shared with relationship parsing |
| `xagent/core/config.py` | `NOTES_DIRNAME`, section names, budget constants, templates and builders |
| `xagent/core/prompt_registry.py` | `notebook_context` and `subconscious_notebook` sections at `order=15` |
| `xagent/core/handlers/message.py` | `notebook_context` threaded into `PromptAssembleContext` |
| `xagent/tools/note_tool.py`, `xagent/tools/__init__.py` | the four note tools |
| `xagent/core/agent.py` | construct `NoteStore`, bind tools, `_notebook_context_for_turn` |
| `xagent/core/runtime/subconscious.py` | `_collect_notebook_context` for reflection turns |
| `xagent/interfaces/server/admin_service.py` | `notes` in `_memory_scope_roots` |
| `xagent/interfaces/base.py`, `xagent/interfaces/cli/setup.py` | config keys: whitelist, validation, defaults, inline comments |
| `GOAL.md` | Principle 8 amendment |

## 11. Tests

`tests/test_note_memory.py` covers the store (frontmatter round-trip including the id staying a
string through YAML, damage tolerance, id collision, slug and CJK filenames, normalization clamps,
inline `[[id]]` links, backlinks, neighbours, archive, cache invalidation on external edits),
retrieval (Chinese and English recall, ranking, archived exclusion, minimum key length, tag/kind
filters, similarity), the four tools (duplicate guard, body cap, partial updates, link walking,
disabled state), weekly distillation prompts and draft parsing (including links; diary feedstock
with week-arc orientation), wiring (diary maintenance does not distil; weekly latch does; diary
range as feedstock and summary as arc; provenance as week range; write-time links; per-week cap;
duplicate skip; switch-off; failure isolation from the weekly file; monthly orphan linking and hub
create/update), and injection (section grouping, caps, budget bound, priority order, sensitivity
marking, layer placement between diary and recent experience).

`tests/test_subconscious.py` adds notebook injection into reflection turns and rejection of a
non-text notebook context. `tests/test_agent_config.py` adds the two config keys and their
validation.

## 12. Goal-check

Mandatory under the `GOAL.md` requirement review rule.

- **Identity** — notes are first-person, in the agent's own words and judgment; reinforces the
  independent subject. Not a database participants can query at will.
- **Multi-user** — one notebook, never sharded per user. Person-linked knowledge is *attributed* via
  `source.person`, not isolated into per-user stores.
- **1:1 and group coverage** — notes are independent of conversation shape; injected identically in
  both. Weekly distillation defaults to `shareable` because a week usually spans more than one
  person; in-chat `write_note` may still mark person-scoped explicitly.
- **Memory/journal perspective** — first person, attribution and uncertainty preserved; the
  distillation prompt reads that week's diary (with the weekly summary only as orientation), not
  raw speaker-marked message batches.
- **Unified memory** — a single notebook; no per-user memory silos.
- **Agent-governed sharing** — `sensitivity` and `source.person` are injected with each note and the
  model decides; no hard-coded filtering.
- **Diary-anchored carrier** — the notebook is a regenerable projection, `source` is mandatory, and
  weekly background distillation can only run for a week that already has a summary file. The
  system works with the notebook empty, disabled, or deleted.
- **Attribution and continuity** — immutable ids and archive-never-delete make the notebook its own
  traceable record of how the agent's understanding evolved.

## 13. What is deferred

- **LLM gardening rewrite**: sharpen raw notes; synonym tag convergence; unused/unlinked decay
  without read counters. Mechanical orphan linking and hub clustering already ship (6.2).
- **Rebuild from a diary range**: an offline entry point that regenerates diary-derived notes.
- **Budget rebalancing** between the diary window and the notebook index, once there is real usage
  to measure.
- **Year sharding** of the notes directory, and a derived index file, if volume ever makes the
  scan-and-cache approach too slow.

## 14. Risks and trade-offs

- **Note explosion and noise** remains a risk, but timing is now the first filter: background
  distillation waits for a weekly summary file. Defences: closed-week latch, diary feedstock (not
  summary rewrite), pre-write neighbour check, per-week cap with a restraint-heavy prompt,
  inject-only-index, and monthly orphan/hub gardening. Without LLM rewrite and decay, a noisy
  notebook still needs a human or the agent to archive entries.
- **Cold start until the first completed week.** In-chat tools cover standing facts that appear in
  conversation; an empty notebook before the first weekly summary is intentional.
- **Semantic overlap with the diary**, degrading into "the diary written differently". Held off by
  hard constraints — reusable conclusion only, atomic body cap, duplicate guard — plus prompt
  language that names the diary's job explicitly and forbids rewriting the week arc into notes.
- **Cost.** Weekly background distillation adds one LLM call per newly written weekly summary (not
  per diary batch). It is switchable, and the notebook still works tool-only. Monthly gardening is
  mechanical and free of an LLM call.
- **Double compression avoided.** Distilling from the summary body alone would drop atomic detail
  and invite mini-weekly-report notes; the summary is orientation only.
- **Recall depends on the agent declaring good keys.** A note with weak keys is nearly unreachable by
  L0 and only findable via `search_note`. The tool description leans on this hard.
- **Injection budget is zero-sum** if the notebook grows past its caps. Currently bounded well below
  the diary window's share.

## 15. Decisions on record

1. **Notes may hold knowledge the diary did not originate** (external sources, tool output). Cost of
   admission: mandatory `source`. The originally proposed diary trace was dropped as diary noise
   (5.4), and Principle 8 asks for provenance instead.
2. **L0 auto-recall is on by default.** It is the highest-value and cheapest layer; reverse key
   matching (7.1) is what makes it free.
3. **Weekly background distillation ships on the weekly summary cadence**, default on and
   switchable. Diary maintenance no longer distils. Distilled notes land as `active` with
   write-time links; there is no `fleeting` status because the diary is already the inbox.
   Feedstock is that week's diary range; the weekly summary is orientation only. Writes go through
   `NoteStore.create`, not the `write_note` tool.
4. **No fleeting staging.** Adding a second inbox without a promotion path dies; adding one with
   promotion is gardening by another name. Delay + closed-week latch is the chosen filter.
5. **Naming**: directory `notes/`, store `NoteStore`, prompt sections `notebook_context` and
   `subconscious_notebook`, tools `write_note` / `update_note` / `search_note` / `read_note`.

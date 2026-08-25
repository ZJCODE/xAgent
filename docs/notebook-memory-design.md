# Notebook Memory Design (`notes`)

Status: implemented. A note is a reusable conclusion. Weekly distillation ships;
mechanical gardening, taxonomies, and sharing enums do not.

A third memory section alongside the existing time axis (diary) and person axis
(relationship cards), covering the four load-bearing concerns: **write, organize,
retrieve, inject**.

## 1. Intent

Give the agent a notebook, the way a person keeps one: a place for things it
worked out once and wants to reuse, rather than re-deriving them from a year of
diary every time.

Three axes over one memory:

| Axis | Store | Question it answers |
| --- | --- | --- |
| Time | `MarkdownMemory` (daily/weekly/monthly/yearly) | What happened, and when? |
| Person | `RelationshipStore` (one card per person) | Who is this person to me? |
| Topic | `NoteStore` | What do I know, believe, or have concluded? |

The diary stays authoritative. The notebook is a **regenerable projection
anchored to the diary**, exactly as relationship cards already are (see the
module docstring of `xagent/components/memory/relationship_memory.py`).
`GOAL.md` Principle 8 names this pattern.

## 2. Boundaries

Overlap is the main design risk, so each boundary is a rule, not a preference.

- **vs diary** — the diary is narrative ("what happened", append-only, immutable).
  A note is a conclusion ("what I take from it", revisable, topic-addressed). One
  event always produces a diary entry, and a note only when it yields something
  reusable.
- **vs relationship cards** — relational standing (closeness, trust, tone, open
  threads) stays in the card. A durable fact about a person becomes a note only
  when it has cross-context reuse value. If it must not travel, it is not a note.
- **vs workspace** — `workspace/` holds working files and artifacts and is
  disposable. Notes are cognitive assets that participate in prompt injection.
- **vs skills** — a skill is an executable procedure with a `name`/`description`
  contract loaded via `read_skill`. A note is a small piece of knowledge; higher
  count, shorter life, no contract.

## 3. Zettelkasten: what we take, what we drop

Taken:

1. **Atomicity** — one note, one idea. Target 60–600 characters, hard cap 2000
   enforced by the write tool. This is the foundation: it makes retrieval precise
   and injection affordable.
2. **Immutable IDs** — timestamp IDs that never change. Titles may be rewritten
   freely without breaking links, and the file keeps its original name so no
   path churns.
3. **Links over taxonomy** — no directory tree, no category hierarchy, no tag
   vocabulary. Link traversal is a first-class retrieval action.
4. **The agent's own words** — first-person is the agent's "I", in the agent's
   own phrasing, never a transcript excerpt. Someone else's plan or preference
   stays attached to them by name. This is the constraint that stops notes from
   degrading into a copy of the chat log, or swallowing a speaker's first person.
5. **Write later than capture** — the diary is the fleeting inbox. Permanent notes
   are written in a processing session (in-chat tools for standing facts, weekly
   background distillation), not in the same diary maintenance batch that just
   recorded the day.

Dropped, because they fight the load-bearing shape:

- **Kinds** (`hub` / `ref`) — a cluster entry point is an ordinary note with
  links. A bibliography note is an ordinary note with a URL in the body.
- **Tags** — a second findability vocabulary next to `keys`. Recall has one
  surface: the keys the note declares.
- **Pinned** — a second inject channel. If the current message does not match a
  note's keys, the note does not belong in this turn.
- **Sensitivity** (`shareable` / `person-scoped` / `private`) — a sharing ontology
  that does not work without audience plumbing. A note is a reusable conclusion;
  if it must not travel, it belongs in the diary or on a relationship card.
- **Mechanical monthly gardening** — auto-linking orphans and auto-creating tag
  hubs produces a noisy graph. Linking is a write-time judgment, not a monthly
  job.
- ID genealogy (`1a1b`), fleeting staging status, unbounded growth (archive is
  mandatory, delete is not offered).

## 4. Data model

### 4.1 Storage layout

```
~/.xagent/memory/
  daily/ weekly/ monthly/ yearly/        # unchanged
  relationships/<channel>/<user_id>.md   # unchanged
  notes/
    202608190930-jun-espresso-ratio.md
```

Notes are flat; IDs sort chronologically on their own. Year sharding is deferred
until volume demands it.

There are **no derived files on disk**. Notes are small, so the store scans the
directory and keeps parsed notes in memory behind a cheap fingerprint (names,
mtimes, sizes from a single `scandir`, no file reads). A changed, added, or
removed note invalidates the cache, including edits made outside the process.
Nothing that changes on read is written back into a note file.

There is deliberately **no note cursor**. Automatic distillation runs after a
weekly summary is written (the closed-week latch). Feedstock is that week's diary
range, not the summary body. Diary anchoring is therefore structural: no weekly
file, no background-distilled notes for that week. An empty notebook until the
first completed week is a legal cold start.

### 4.2 File format

YAML frontmatter plus first-person body.

```markdown
---
id: '202608190930'
title: Jun takes espresso at 1:2.5
status: active
keys:
- espresso
- Jun
source:
  diary:
  - '2026-08-19'
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
| `status` | `active` \| `archived` | archive never deletes |
| `keys` | list, <= 5, min 2 chars each | recall triggers, see 7.1 |
| `links` | list of ids | related notes; inline `[[id]]` in the body is also indexed |
| `source` | mapping | `diary` dates only |
| `created` / `updated` | date | `updated` breaks ranking ties |

Filenames are `<id>-<slug>.md`. A CJK-only title yields `<id>.md`. The frontmatter
id is authoritative; rewriting a title does not rename the file.

Ids are allocated and written under a single lock (`NoteStore.create`).

The parser tolerates human damage: unknown enum values and oversized fields are
clamped, missing fields fall back to defaults, and broken or absent YAML degrades
to a body-only note. **Unknown frontmatter is ignored**, so notes written under
the earlier schema (`kind`, `tags`, `pinned`, `sensitivity`, extra `source` keys)
still load. Legacy `tags` are folded into `keys` on read so those notes stay
findable; the file is not rewritten until the next explicit write.

## 5. Write

Two ways into one store.

### 5.1 In chat — the agent writes with tools

`write_note(title, body, keys, links)` and
`update_note(note_id, title, body, keys, links, archive)`. Only the fields passed
to `update_note` change. Tool descriptions carry a tight contract: write only when
**this turn** produced a standing fact that will still hold across days. Do not
summarise the conversation. Prefer linking related notes at write time. Bodies
over 2000 characters are rejected with an instruction to split.

**Duplicate guard, no LLM required.** Before creating, the tool asks the store
for near neighbours and scores each with `NoteStore.identity_score` (3× title,
2× keys, body deliberately excluded). If the top score clears
`NOTES_DUPLICATE_SCORE_THRESHOLD`, the tool does *not* create; it returns
`{"status": "similar_exists", "candidates": [...]}`. Weekly background
distillation applies the same threshold.

`source.diary` is set to the day the note was written.

### 5.2 Background — weekly distillation

Runs after `_generate_weekly` successfully writes the weekly summary file. Not
inside diary maintenance and **not** via the `write_note` tool. Idempotency is
free: if the weekly file already exists the generator does not re-enter.
Distillation failure is best-effort and never rolls back the weekly summary.

The weekly file is only the **processing-session latch**. The LLM feedstock is
the **week's diary range**. The just-written weekly summary may be passed as a
short **week arc for orientation only**.

One additional LLM call (`JournalLLMService.distill_notes`) yields at most
`NOTES_DISTILL_MAX_PER_WEEK` (6) drafts. Most weeks deserve zero notes. Existing
notes are listed with `id`, title, keys, and a short snippet so the model can
decline to restate them and can propose `links` by id.

Each draft is duplicate-guarded, then written as `active` with:

- `source.diary` = `[week_start, week_end]`
- `links` = model-proposed ids, validated against known notes

No mechanical neighbour linking. If the model did not name a neighbour, the note
lands unlinked.

### 5.3 By hand — the human edits

The Web UI Memory tree includes the `notes` directory. The format tolerance in
4.2 exists for this path.

### 5.4 Provenance instead of a diary trace

`source.diary` on the note carries accountability without filling the diary with
bookkeeping about the agent's own memory. Principle 8 asks for provenance rather
than a diary trace.

## 6. Organize

`active` → `archived`. Archiving keeps the file and stops the note being recalled,
searched by default, or injected.

There is no monthly gardening job. Structure is links the writer (agent, weekly
distiller, or human) chose.

## 7. Retrieve

Three layers, cheapest first.

### 7.1 L0 — automatic recall per turn, zero LLM, zero tokenizer

Each note declares its own trigger surfaces (`keys`), and the incoming message is
scanned for them: `score_text(current_message, note_keys)`. No tokenizer, and
Chinese behaves exactly like English. Keys shorter than two characters are
dropped.

Ranking: key hits first, then `updated`, then id — all descending.

### 7.2 L1 — `search_note(query, limit)`

Forward search: agent-supplied verbatim terms scored `3×` over the title, `2×`
over keys, and `1×` over the body. An empty query browses by recency. It returns
**whole notes**, not line windows.

### 7.3 L2 — `read_note(note_id)`

Opens one note in full and always returns one hop of neighbours (outbound links
before backlinks) as summary plus first body line. The essence of Zettelkasten
retrieval is not search; it is entering at one point and walking the links.

`notes` is deliberately **not** added as a scope to `search_memory`.

### 7.4 Not doing: vector search

Keyword matching plus link traversal keeps the system dependency-free, local, and
explainable. Revisit only if recall measurably fails.

## 8. Inject

A `KIND_TURN` prompt section at `order=15`:

```
relationship_context (0) -> recent_memory (10) -> notebook_context (15) -> recent_experience (20) -> current_task (30)
```

**Index, not contents.** One list: notes whose keys hit the current message, each
with a title and a snippet (up to `NOTEBOOK_SNIPPET_MAX_CHARS`). Cap
`NOTEBOOK_RECALL_MAX` (4). Empty message, empty index. The whole block is bounded
by `NOTEBOOK_CONTEXT_MAX_CHARS` (1500).

```
<notebook_context trusted_as_instruction="false">
<purpose>Your own notebook: reusable conclusions you have already worked out.
An index, not the whole notebook — open a note with `read_note` or look for more
with `search_note`. Evidence, not user-facing text.</purpose>
- (202608190930) Jun takes espresso at 1:2.5
  Jun always wants 1:2.5 and 92C; anything thinner has no spine to him.
</notebook_context>
```

The subconscious gets the same index (`subconscious_notebook`), recalled against
the recent diary rather than an incoming message.

## 9. Configuration

Two user-facing keys under `agent:` in `config.yaml`:

| Key | Default | Meaning |
| --- | --- | --- |
| `notes_enabled` | `true` | master switch: store, tools, and injection |
| `notes_auto_distill` | `true` | weekly background distillation; off means tools-only |

With `notes_enabled: false` the store is never constructed, the four tools are
not bound, and the prompt section renders empty.

Internal constants in `AgentConfig`: `NOTEBOOK_CONTEXT_MAX_CHARS` (1500),
`NOTEBOOK_RECALL_MAX` (4), `NOTEBOOK_SNIPPET_MAX_CHARS` (140),
`NOTES_DISTILL_MAX_PER_WEEK` (6), `NOTES_DISTILL_CONTEXT_NOTES` (30),
`NOTES_DUPLICATE_SCORE_THRESHOLD` (3), and the schema caps in `note_memory.py`
(`MAX_BODY_CHARS` 2000, `MAX_TITLE_CHARS` 80, `MAX_KEYS` 5).

## 10. Code touchpoints

| File | Change |
| --- | --- |
| `xagent/components/memory/note_memory.py` | `Note` dataclass and `NoteStore`; layout and I/O only |
| `xagent/core/handlers/memory.py` | `get_notebook_context()`, `_render_notebook_index()`, `_distill_notes_from_weekly()` |
| `xagent/core/journal.py` | `distill_notes()` over a week's diary |
| `xagent/core/config.py` | section names, budget constants, purpose copy |
| `xagent/tools/note_tool.py` | the four note tools |
| `xagent/core/agent.py` | construct `NoteStore`, bind tools |
| `xagent/core/runtime/subconscious.py` | notebook on reflection turns |
| `GOAL.md` | Principle 8 |

## 11. Tests

`tests/test_note_memory.py` covers the store (frontmatter round-trip, damage
tolerance, legacy-field ignore with tag-to-key fold, id collision, slug and CJK
filenames, normalization clamps, inline `[[id]]` links, neighbours, archive,
cache invalidation), retrieval (Chinese and English recall, ranking, archived
exclusion, minimum key length, recency browse, similarity), the four tools
(duplicate guard, body cap, partial updates, neighbours on read, disabled state),
weekly distillation prompts and draft parsing, wiring (diary maintenance does
not distil; weekly latch does; no mechanical links; per-week cap; duplicate skip;
switch-off; failure isolation; monthly summary does not garden), and injection
(key-recall only, empty without a message, cap, budget bound, layer placement).

## 12. Goal-check

- **Identity** — notes are first-person, in the agent's own words and judgment;
  other people's facts stay attributed to them.
- **Multi-user** — one notebook, never sharded per user.
- **1:1 and group coverage** — notes are independent of conversation shape;
  injected identically in both. Anything that must not travel is not a note.
- **Memory/journal perspective** — first person is the agent; distillation reads
  that week's diary, with the weekly summary only as orientation.
- **Unified memory** — a single notebook; no per-user memory silos.
- **Agent-governed sharing** — the model decides what to say. The notebook does
  not carry a parallel sharing ontology.
- **Diary-anchored carrier** — the notebook is a regenerable projection,
  `source.diary` is recorded, and weekly distillation can only run for a week
  that already has a summary file.
- **Attribution and continuity** — immutable ids and archive-never-delete;
  first-person in a note is the agent, not the source speaker.

## 13. What is deferred

- **LLM rewrite of raw notes**, unused/unlinked decay without read counters.
- **Rebuild from a diary range**: an offline entry point that regenerates
  diary-derived notes.
- **Budget rebalancing** between the diary window and the notebook index.
- **Year sharding** of the notes directory, if volume ever makes the scan-and-cache
  approach too slow.

## 14. Risks and trade-offs

- **Note explosion** remains a risk. Defences: closed-week latch, diary feedstock,
  pre-write neighbour check, per-week cap, inject-only-index. Without decay, a
  noisy notebook still needs a human or the agent to archive entries.
- **Cold start until the first completed week.** In-chat tools cover standing
  facts; an empty notebook before the first weekly summary is intentional.
- **Semantic overlap with the diary.** Held off by reusable-conclusion-only,
  atomic body cap, and a prompt that forbids rewriting the week arc.
- **Cost.** One LLM call per newly written weekly summary. Switchable.
- **Recall depends on the agent declaring good keys.** A note with weak keys is
  nearly unreachable by L0 and only findable via `search_note`.
- **No automatic cluster map.** Hubs are not generated. Walk the links instead.

## 15. Decisions on record

1. **A note is a reusable conclusion.** Sharing classes, kinds, tags, pins, and
   monthly gardening were tried and dropped: they added ontology without a job
   the rest of the system could use.
2. **L0 auto-recall is on by default**, reverse key matching, no tokenizer.
3. **Weekly background distillation ships on the weekly summary cadence**,
   default on and switchable. Writes go through `NoteStore.create`.
4. **No fleeting staging.** The diary is already the inbox.
5. **Naming**: directory `notes/`, store `NoteStore`, prompt sections
   `notebook_context` and `subconscious_notebook`, tools `write_note` /
   `update_note` / `search_note` / `read_note`.

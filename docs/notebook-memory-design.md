# Diary-derived memory: people and topics

The diary is the Agent's authoritative first-person experience stream. Two small,
regenerable views make future recollection easier:

| View | Question | Content |
| --- | --- | --- |
| Diary | What happened? | Experiences, attribution, important details and reasons |
| Relationships | How do I understand and interact with this person? | Relevant background, preferences, boundaries, important shared experience |
| Notes | What conclusion can I reuse about this topic? | Project constraints, decisions and reasons, verified methods, grounded understanding |

Personal information belongs on the person's card. Temporary device status,
greetings, tests and ordinary explanations normally stay in the diary. Executable
commitments use the existing task system. Storing a recollection never grants
permission to disclose it to another person.

## Write once, after the diary

Notebook storage and its reading tools are always available. Conversations have
no direct note-writing tools. After a diary commit, one maintenance model call
proposes relationship and notebook changes together. The same heartbeat catches
up when a prior projection failed, even without new messages.

The model sees the new committed daily slice, matching identity metadata,
relevant relationship cards, relevant notes and bounded one-hop summaries.
Existing memory context is capped at 6000 characters; note recall and snippets
use the ordinary notebook budgets. Whole diary entries stay intact when batching.
Only full input notes may be revised or removed; one-hop summaries supply link
targets without granting permission to overwrite their unseen contents.
Raw speaker metadata resolves identity; it never independently establishes a fact.
The diary writer retains the substance of important preferences, constraints,
decisions and reasons, so projections do not become the only record of them.

The single policy in `JournalLLMService.build_projection_system_prompt` asks for
information with a concrete future use and diary support. It excludes replay,
to-dos, temporary status and guesses about personality or closeness from isolated
behavior. Facts retain their subject and scope; "I" always means the Agent.
There is no minimum length, required section layout, or obligation to write.

The result contains only changes:

- `relationships`: supplied person key to complete revised prose, or `null` to remove.
- `notes`: complete replacement `title`, `body`, `keys`, `links`, with an existing
  `id` for revision or no id for creation. An existing id with `body: null` removes.
- Omitted records stay unchanged. `{}` means no changes.

The program validates ids, ownership, content lengths and references, assigns new
ids, and records actual diary dates. An unchanged conclusion is not rewritten:
its file bytes, modification time and source dates stay stable. A shared topic
word is not sufficient to reject a genuinely different conclusion as a duplicate.

## Files

Both views use YAML frontmatter and natural-language Markdown bodies:

```text
memory/
  daily/ weekly/ monthly/ yearly/
  relationships/<channel>/<user_id>.md
  notes/<id>-<slug>.md
```

Relationship frontmatter contains `key`, an available human `name`, `updated`,
and `source: {diary: [YYYY-MM-DD, ...]}`. The body is a sparse person card without
fixed headings, numeric closeness or personality labels. Identity keys remain
channel-specific; a display name never merges two people.

Note frontmatter contains `id`, `title`, `keys`, `links`, `updated`, and
`source: {diary: [YYYY-MM-DD, ...]}`. One note is one conclusion, with necessary
conditions and reasons expressed in prose. The immutable 12-digit id survives
title changes; the existing filename also stays stable.

Notes retain the existing caps: title 80 characters, body 2000, at most five
trigger keys of at least two characters. These are internal limits, not user
settings. Files remain hand-editable, and reads do not rewrite them. There is no
migration, historical rebuild command, or legacy-field translation.

## Meaningful links, derived backlinks

A note may reference an existing note when it explains, supports, constrains,
contradicts or applies that conclusion. State the reason in the prose. Shared
keywords alone are insufficient, and an independent note needs no link.

The `links` field and body `[[id]]` references form one normalized, deduplicated
outgoing set. Backlinks are derived from that set at read time, never stored in
another file. Invalid targets and self-links are excluded from effective links;
a dangling reference does not prevent reading the body. Cycles do not recurse.
New notes may link to supplied existing ids; subsequent experience can add links
between notes created in an earlier pass. There is no model call just to fill in
a graph, generate a category hub, or connect orphans.

## Find, open, follow

- `search_note(query, limit)`: keyword search over title, keys and body, returning
  only ids, titles and short snippets. An empty query browses recent notes.
  Default limit 8, maximum 20, snippet limit 140 characters plus an ellipsis.
- `read_note(note_id)`: the full note and sources, plus at most three deduplicated
  one-hop summaries, marked `outgoing`, `incoming`, or `both`. Open another id
  to follow a reference. No recursive expansion or extra tool is needed.
- Automatic recall: match declared topic/project keys to the current text and
  inject at most four title/snippet rows within 1500 characters. It does not
  expand the graph. Relationship cards follow the current speaker and room peers.

Subconscious turns use the same notebook index and the same unified diary.
Neither an internal thought nor an existing note proves that a message was sent.

## Failure recovery

`memory/.memory_projection.json` contains only operational state: the verified
diary byte cursor, known identity metadata (including prior display names), and
at most one generated result waiting to be applied. It is outside searchable
Markdown scopes and is not another source of claims.

Identity metadata is captured before diary writing. The projector reads diary
bytes only after the journal receipt is completed. It saves its exact operations,
including assigned note ids, before changing any projection files. After all
writes finish, it advances the cursor and clears the pending result.

Model/validation failure keeps the cursor for retry. Partial file writes replay
the saved result after restart without another model call or fresh ids. Disk
workers finish before cancellation releases the maintenance/process lock. Diary
commits proceed independently of projection failure; their text is never appended
again just because projection failed. A processed diary prefix changed by hand
is reported rather than silently overwriting or trusting stale evidence.

## Verification and goal check

Tests cover source ordering, no-change files, correction/removal, distinct
conclusions under the same topic, bounded search/read results, references and
backlinks, ambiguous identities, model failure, partial writes, cancellation,
and restart recovery. Fixture responses validate the runtime contract; assessing
semantic selectivity also requires reviewing real model outputs.
The opt-in `scripts/subconscious_review.py` scenarios `memory_correction` and
`memory_selectivity` exercise the configured model with synthetic text and retain
raw journal outputs for human review.

- **Identity:** prose is the Agent's recollection; other people's facts stay attributed.
- **Multi-user:** stable person keys remain distinct, including same-name contacts.
- **1:1 and group:** one shared policy applies to private and room experiences.
- **Memory/journal perspective:** the diary preserves first-person experiences and evidence.
- **Unified memory:** one diary and one topic notebook, not per-user memory silos.
- **Agent-governed sharing:** retrieval is not consent; disclosure remains contextual.
- **Diary-anchored carrier:** both views are disposable and derivable, with real diary sources.
- **Attribution and continuity:** stable note ids, explicit subjects, scoped preferences and corrections.

# Seeing stored images by path

Status: implemented. This document describes what the code does.

## 1. First principle

Split by **how you look it up later**, not by content type.

An image is one fact that can be used in several ways. Those ways are different jobs:

| Job | How you look it up | Channel |
| --- | --- | --- |
| Keep the file | Workspace path | Disk |
| See it | Same path, opened onto the model's eyes | Vision input on `current_task` |
| Remember that it happened | Diary | First-person narrative |
| Show it to someone | Same path | `attach_artifact` (outbound) |

The file is the handle. Seeing is a channel. The diary is the story. Do not invent a fourth store of "what the picture contained" in prose, and do not wrap seeing in a skill or an "understand image" tool.

This is the same cut as the memory write split: diary / notes / cards are how you will retrieve, not kinds of sentence. Here path / eyes / diary / outbound attachment are how you will retrieve, not kinds of pixel.

## 2. Intent

If a picture already lives in the workspace, the agent can see it again the same way it sees a picture the user attached this turn.

That is the whole feature. Everything else is a consequence.

## 3. Why this, and not the alternatives

**Pixels are only on the originating user message.** History keeps the path (attachment manifest, `[Attached image: N]`). The next turn does not reopen the file. A bare `assets/.../foo.png` in text is not treated as vision input. A tool that returns image bytes is stripped from the model and shown to the user. So the path exists, and the eyes are closed.

Dropped alternatives, and why:

- **Write a seeing-record into the experience stream when first viewed.** That is a parallel memory of the file. It goes stale, it duplicates the pixels in words, and it is the wrong axis. Path is enough.
- **Skill (`SKILL.md`).** A skill is a procedure loaded as text via `read_skill`. It cannot open the vision channel. Domain methods ("how to read this kind of gel") may become skills later; *whether the agent can look* is not a skill.
- **`understand_image` / caption / OCR tool.** That is a second pair of eyes that returns words. The main model already sees. Tool results are observations; current image tool results are specifically *not* shown to the model. Understanding is what happens after the pixels are on `current_task`.
- **Re-inject every historical image every turn.** Looking is on demand. Cost and context are the reason path exists.
- **`generate_image`.** Removed. This proposal does not make pictures.
- **`attach_artifact`.** Outbound: show or send a file to a person. Inbound seeing is the opposite direction.

## 4. Mechanism

One injection site. Two ways to name a path.

All vision input is assembled onto `current_task` as `image_url` blocks — the same place user attachments already go (`MessageHandler.build_turn_context_messages`). Do not add a second multimodal slot.

### 4.1 User names a stored image this turn

If the current user message already carries image attachments, keep today's behavior.

Also accept, as image sources for *this* turn, a workspace blob URL or a workspace-relative path that resolves to an image file inside the workspace. Today a blob URL in the current message can work; a bare path does not. That is a hole in the handle, not a new faculty.

This covers "look at `assets/inbound/.../shot.png`" without a tool call.

### 4.2 Agent names a stored image mid-turn

"What did line 3 of that screenshot say?" usually does not contain the path. The path is already in `recent_experience`. The agent must be able to *request* the eyes open.

Mid-turn, the only way the agent asks the runtime for anything is a tool call. The tool is a **channel request**, not an understanding API.

Bind it only when `supports_vision` is true (same pattern as `web_search` only when a search provider is on). Name it by the job, for example `see_image`.

Contract:

- **In:** one workspace path (workspace-relative, blob URL, or absolute path inside the workspace — same resolver family as `attach_artifact`).
- **Effect:** the path is queued for the **next model iteration** of this turn as `current_task` vision input.
- **Observation text:** a short ack (`path`, size, mime). Not a caption. Not "I see a login form". The pixels are the payload; the text only confirms the channel opened.
- **Not:** user-visible delivery. If the person should receive the file, that remains `attach_artifact`.

If `supports_vision` is false, the tool is absent and `capability_limits` already says image understanding is unavailable.

### 4.3 Shared inject pipeline

User attachments, paths named in the current user message, and paths requested via `see_image` all go through one helper: resolve inside workspace → read bytes → compress for transport (existing `compress_image_bytes_for_transport`) → data URI → `image_url` on `current_task`.

Cap remains `MAX_IMAGES_PER_MESSAGE` (5). Extra paths fail closed with a clear observation, they are not silently dropped without a reason.

Workspace-only. No fetch of arbitrary `https://` URLs as a side effect of this job (that is a different, SSRF-shaped problem). Existing remote URL attachments on the *originating* message stay as they are until a later unification.

### 4.4 What a follow-up actually does

1. Recent experience already lists the path.
2. The agent calls `see_image` with that path.
3. The next iteration of the same turn has the pixels on `current_task`.
4. It answers from what it sees.

No new memory write. No nested vision model. No skill load.

## 5. Diary, notes, subconscious

- **Diary** keeps using the text transcript. The path in the manifest is provenance. If the agent looked and then spoke, the spoken turn is what maintenance can narrate. Maintenance does **not** receive pixels in this proposal.
- **Notes** are for reusable conclusions, not for caching OCR. If a screenshot yields a durable fact, `write_note` remains the knowledge write path, with the file path as source.
- **Subconscious** stays `include_images=False`. Reflection does not open the eyes.

## 6. Goal check

- **Identity.** Seeing is the agent's own perception, not a per-user helper mode.
- **Multi-user.** Attribution stays on the message that introduced the file (who sent it, which channel/room). The workspace is the agent's, not a user silo.
- **1:1 and group.** Same path handle in both. Group context is already on the transcript headers.
- **Memory/journal perspective.** No first-person "I saw…" record is required at view time. The diary remains narrative over the experience stream.
- **Unified memory.** Files live in one workspace, like other artifacts. Not a per-user image store.
- **Agent-governed sharing.** Opening a file onto the agent's eyes is perception. What it then *tells* someone is a sharing decision, the same as with diary or notes. This change does not add file ACL; `run_command` can already read workspace files.
- **Diary-anchored carrier.** No parallel image-memory schema. Path is provenance; diary stays sufficient if the image files are discarded (the day still happened; the pixels are gone).
- **Attribution and continuity.** Continuity is "I can look at the file again", not "I stored a caption". Who sent the picture remains on the original message.

## 7. Deferred

- Heuristic auto-open of "the last image from this person" without `see_image`.
- Giving diary maintenance vision.
- Marking Anthropic (and any other actually capable provider) in `VISION_CAPABLE_PROVIDERS`. Adjacent, not this job.
- Domain skills for *how* to read a class of image.
- Changing inbound Web/API first-upload to compress on disk. Inject-time compress is enough for the eyes; on-disk original can stay.

## 8. Acceptance

- A follow-up that does not re-attach the picture can still result in `image_url` on `current_task` after `see_image`.
- The tool observation is not a description of image contents.
- `attach_artifact` still does not put pixels on the model.
- With `supports_vision: false`, `see_image` is not bound.
- Paths outside the workspace are rejected.
- More than five images in one inject fail closed.

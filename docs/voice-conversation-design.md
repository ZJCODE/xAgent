# Voice conversation: first-principles analysis and a Soniox-native redesign

Status: analysis and proposal. Nothing here is implemented yet. Written after
running the current voice channel on a physical device in a real room.

Scope: `xagent/interfaces/voice/` (runtime, soniox, audio, config, factory) and
the places in `xagent/core/` that the voice channel reaches into.

Sources: the `soniox==2.8.0` Python SDK read directly, plus the Soniox docs —
[endpoint detection](https://soniox.com/docs/stt/rt/endpoint-detection),
[manual finalization](https://soniox.com/docs/stt/rt/manual-finalization),
[speaker diarization](https://soniox.com/docs/stt/concepts/speaker-diarization),
[STT WebSocket API](https://soniox.com/docs/api-reference/stt/websocket-api),
[TTS WebSocket API](https://soniox.com/docs/api-reference/tts/websocket-api),
[TTS realtime generation](https://soniox.com/docs/tts/rt/real-time-generation),
[TTS timestamps](https://soniox.com/docs/tts/rt/timestamps),
[emotion & tone](https://soniox.com/docs/tts/concepts/emotion-and-tone),
[language mixing](https://soniox.com/docs/tts/concepts/language-mixing),
[temporary API keys](https://soniox.com/docs/guides/temporary-api-keys),
[pricing](https://soniox.com/pricing), and Soniox's own voice-agent guidance in
[the LiveKit integration guide](https://soniox.com/docs/integrations/livekit/voice-agent)
and [the voice-agent architecture wiki](https://soniox.com/wiki/voice-agent-architecture).

## 1. The category error

The voice channel today is **text chat with a codec bolted on each end**.

```
mic → Soniox STT → one <end> token → one string
    → agent.chat_events(...)          # the same call Feishu/Weixin/API make
    → text deltas → Soniox TTS → speaker
    (mic hard-muted for the entire reply)
```

That is a faithful description of `VoiceRuntime.run_forever` (`runtime.py:157`)
and `_speak` (`runtime.py:212`). It is a correct, clean implementation of the
wrong object. Nearly every complaint that shows up on a real device is a
consequence of this one framing, not of a missing feature.

A spoken conversation is not a chat transcript delivered as audio:

| Axis | Text chat | Spoken conversation |
| --- | --- | --- |
| Time | asynchronous; delay is free | latency is semantic — silence *means* something |
| Floor | implicit, message-atomic | negotiated continuously, interruptible at any instant |
| Channel | addressed, lossless, private | one shared acoustic space with the TV, other people, and your own speaker in it |
| Message | reviewable, skimmable, structured | linear, ephemeral, no scrollback, no markup |
| Identity | given by the transport | must be recovered from the acoustics |
| Failure | visible, re-readable | indistinguishable from silence |
| Cost | per turn | per second of wall time the session is open |

Soniox says the same thing in its own words: the orchestrator — "the state
machine that runs the conversation … listening, thinking, speaking,
interrupted" — is "the part that makes talking to an agent feel like a
conversation instead of a sequence of requests," and "writing this well is most
of the work." xAgent currently has no orchestrator. It has a `for` loop.

## 2. Ground truth: what the runtime actually does

`LISTEN → THINK → SPEAK → 0.5 s cooldown → LISTEN`, strictly serial. Four
properties fall out of the code and drive everything below:

1. **The mic is dead during SPEAK.** The capture callback returns early whenever
   `pause_event` is set (`audio.py:696`) and the STT sender pauses the session
   (`soniox.py:212-218`). Nothing said while the agent talks exists anywhere.

2. **The STT event stream is not read during THINK.** `run_forever` awaits the
   whole reply before pulling the next utterance (`runtime.py:173`), so the
   recognizer generator stays suspended at its `yield` inside `_iter_session`
   (`soniox.py:150-171`) while the sender thread keeps shipping audio. Endpoints
   found during thinking are not dropped — they queue and fire back to back
   afterwards. This is the "it answered my last three sentences in a row" bug.

3. **The agent is called with no channel instructions** (`runtime.py:353-359`),
   so the model composes in exactly the register it uses for Feishu.

4. **Five overlapping stop flags** coordinate the pipeline: `pause_event`,
   `stop_event`, `playback_stop_event`, the synthesizer's `_cancel_event`, and
   the STT's `session_stop`. Nothing owns "who has the floor right now."

## 3. Capability ledger: what Soniox gives us, and what we use

This is the shortest route to the point. The platform under this channel is
built for voice agents; the integration uses roughly a third of it.

| Soniox capability | Available | xAgent today |
| --- | --- | --- |
| Semantic endpointing → `<end>` | yes | used |
| `endpoint_sensitivity` / `_latency_adjustment_level` / `max_endpoint_delay_ms` | yes | frozen constants (`config.py:24-26`) |
| Manual `finalize()` → `<fin>` | yes | never called; `<fin>` explicitly discarded (`soniox.py:165`) |
| `pause(finalize=True)` — flush before pausing | default | called with `finalize=False` (`soniox.py:214`) |
| Token `confidence` | yes | discarded (`soniox.py:59`) |
| Token `speaker` (diarization, ≤15 speakers) | yes | **disabled** (`soniox.py:195`) |
| Token `start_ms` / `end_ms` | yes | discarded |
| `final_audio_proc_ms` / `total_audio_proc_ms` | yes | discarded |
| Per-token `language` + language identification | yes | majority-voted per utterance, then misused (§7) |
| `context` (`general`/`text`/`terms`, ≤8000 tokens) | yes | static YAML only (`config.py:71-112`) |
| `language_hints_strict` | yes | unused |
| Realtime translation (one-way / two-way) | yes | unused |
| `client_reference_id` for usage attribution | yes | unused |
| Temporary API keys (scoped, expiring) | yes | unused |
| TTS `tts-rt-v2` | yes | pinned to `tts-rt-v1` (`config.py:20`), removed Aug 31 2026 and auto-routed |
| TTS audio tags (`[warm]`, `[pause]`, `[laughs]`) | v2 | unused |
| TTS `reduce_silence` | v2 | unused |
| TTS `return_timestamps` (character-level) | yes | unused, and a test pins it to `None` (`test_voice_runtime.py:428`) |
| TTS multi-stream (5 per connection) + keepalive | yes | a new connection per turn, no keepalive |
| TTS `cancel` | yes | called on error only, never for barge-in |
| Voice cloning | yes | unused |

Three entries in that table are not "unused features" but active defects:
`enable_speaker_diarization=False`, `return_timestamps` unset, and the missing
TTS keepalive. They are covered in §6, §5 and §9 respectively.

## 4. Time — latency is part of the message

**First principle.** In speech, the gap before a reply carries meaning. Human
turn-taking gaps cluster near 200 ms; past ~1 s a listener reads hesitancy, past
~2–3 s they assume it broke and speak again — which, given §2.2, produces a
duplicate turn. Latency here is a correctness property, not a performance one.

Where the budget goes:

| Stage | Code | Cost |
| --- | --- | --- |
| Endpoint detection | `max_endpoint_delay_ms = 1500` (`config.py:24-26`) | up to 1.5 s before the agent is called |
| Context assembly | `_build_turn_context` — SQLite history, diary, relationship cards, notebook recall, skills catalog, serial (`agent.py:334-379`) | 10s–100s ms, worse on SD storage |
| Model TTFT | reasoning-capable default with a ≤16 kB instruction block | usually the largest term |
| Tool loops | up to 50 iterations (`DEFAULT_MAX_AGENT_LOOPS`) | unbounded, and silent |
| TTS connect | a fresh websocket per turn (`soniox.py:280`) | one handshake |
| Output device open | `RawOutputStream` per playback (`audio.py:757`) | device-dependent, plus a click |

`_TurnTiming` already logs endpoint→first-text→first-audio→total
(`runtime.py:47-74`). The instrument exists; nothing reads it and changes
behaviour. Soniox also hands us an audio clock for free —
`final_audio_proc_ms` / `total_audio_proc_ms` on every event — which measures
how far the recognizer is behind wall clock and would expose a device that is
falling behind rather than one that is merely slow.

What to do, by payoff:

- **Preemptive generation.** Soniox streams non-final tokens throughout the
  utterance. Start context assembly and the LLM call on the evolving non-final
  transcript, and hold TTS until `<end>` confirms the turn. This is LiveKit's
  `preemptive_generation` (LLM preemptive, TTS deferred) and it removes the
  assembly + TTFT term from the perceived gap almost entirely. Cancel and re-run
  if the final transcript diverges.
- **Speak an acknowledgement before the answer exists.** What the user feels is
  time-to-first-*audio*. A 300 ms locally-decided filler covers the remaining
  window at no model cost, and with v2 audio tags it can sound like a person
  thinking (`[curious] 嗯…`) rather than a beep.
- **Keep the pipes warm.** Open the output device once per process. Hold one TTS
  connection open across turns and open a stream per utterance — the API is
  built for this (5 streams per connection), as long as the keepalive in §9 is
  sent. This deletes one handshake from every single turn.
- **`reduce_silence: true`.** A v2 flag that trims inter-word and inter-sentence
  pauses without changing pronunciation speed. Free perceived tempo; strictly
  better than raising `speed`, which makes the agent sound rushed.
- **Enforce the budget.** If first audio has not happened by *N* ms, emit the
  filler. If a tool loop exceeds *M* seconds, say so. A voice profile should cap
  `max_agent_loops` far below 50.

## 5. Floor control — Soniox ends turns, local VAD starts them

**First principle.** Endpointing is a decision about whether the speaker yielded
the floor, and it needs prosody, syntax, semantics and dialogue state.

Soniox already does exactly this: its endpointing is *semantic*, using "pauses,
intonation, speech patterns, and conversational context" to distinguish a
finished thought from a mid-sentence pause. That is better than any VAD we would
write, and the current design is right to rely on it.

Soniox's own guidance names the split to aim for: **"Soniox decides when a turn
ends, Silero decides when one starts."** Server endpointing owns turn *end*;
a local VAD owns interruption, i.e. turn *start* during playback. xAgent has the
first half and none of the second.

### 5a. The endpoint settings are the vendor preset — the aggregation is missing

`latency_adjustment_level=2, sensitivity=0.3, max_endpoint_delay_ms=1500` is
verbatim Soniox's "recommended configuration for lower latency". So the numbers
are not careless. But the docs are explicit about the trade: more aggressive
endpointing "can also produce more endpoints, which means longer speech may be
split into more segments."

That is fine for a captioning UI and wrong for xAgent, because **one `<end>`
becomes one complete agent turn** — memory write, diary scheduling, the lot.
A person who pauses to think mid-thought gets two answers to half a question
each. The fix is not slower endpointing; it is an **utterance aggregator**
between STT and the agent: on `<end>`, hold a short grace window, and if more
speech arrives, append and re-decide instead of dispatching. Soniox ships
exactly this concept as `RealtimeUtteranceBuffer` in the Node SDK; the Python
SDK has no equivalent, so it is ours to write.

Once aggregation exists, the settings become policy rather than constants, and
can adapt: shorter delay after a question-shaped partial, longer after a
trailing conjunction. The LiveKit + Soniox reference agent runs
`max_endpoint_delay_ms=1000` alongside Silero, which is a reasonable target once
local VAD is in the loop.

### 5b. Barge-in is architecturally excluded right now

The mic is hard-muted for the whole reply (`runtime.py:221`, `audio.py:696`), so
a wrong 40-second answer can only be waited out or Ctrl-C'd. The primitives are
all present and unused: `SonioxRealtimeTTS.cancel()` (`soniox.py:248`),
`Agent.abort()` / `AgentInbox.request_abort()` (`inbox.py:124-133`). The
pre-Soniox config even had `enable_interruptions` and wake phrases
(`cli/setup.py:395-398`); the rewrite dropped them. It did more than drop them:
`_LEGACY_VOICE_KEYS` (`config.py:34-43`) now makes `enable_interruptions`,
`wake` and `return_timestamps` *validation errors*. The three config keys this
document argues hardest for are the three the schema currently refuses.

The blocker is that muting is the wrong echo control. The right tool is AEC, and
this is the easy case because the reference signal is the audio we just
generated. With AEC the mic stays open during playback, which is the
precondition for everything else. Cheapest first: a USB conference speakerphone
or mic array that does AEC on-device (which also removes the resampling cost in
§6d); `speexdsp`/WebRTC AEC in the capture path; or, as a stopgap, energy-gated
barge-in that only triggers when input exceeds the played-back reference.

`_PLAYBACK_MICROPHONE_COOLDOWN_SECONDS = 0.5` (`runtime.py:27`) is the tell: a
fixed guess that is too long for a fast exchange and too short for a reverberant
room. It disappears once AEC exists.

### 5c. Stopping on any sound is worse than not stopping

With the mic open, "嗯", "对", "ok" must **not** interrupt. Barge-in needs a
classifier over {backchannel, correction, new turn} from VAD energy plus the
non-final transcript. LiveKit's parameters name the useful knobs directly:
`min_duration` and `min_words` — require actual words, not a cough, before
cutting the agent off. Get this wrong permissively and the agent cannot finish a
sentence in a room with two people in it.

### 5d. Barge-in has a memory consequence, and Soniox solves it

When playback is cut 3 seconds into a 20-second reply, the user heard three
seconds — but `chat_events` has already stored the entire `visible_text` as the
assistant message (`agent.py:832-839`). The diary and working context then hold
sentences the agent never said, and the next turn reasons from them. Because the
diary is the authoritative memory carrier (GOAL.md principle 8), this silently
corrupts the memory stream. It is the failure mode that would bite hardest the
moment barge-in ships, and it is invisible until it does.

Soniox built the fix and Soniox names this exact use case: `return_timestamps`
returns character-level start/end times so applications can "stop cleanly during
interruptions, and continue from the correct point". With the character timeline
and the playback position, the runtime knows precisely which prefix was heard.
Store that prefix as what was said; keep the remainder as unsaid. Today the flag
is unset, and `test_voice_runtime.py:428` asserts it stays `None`.

### 5e. An utterance during THINK should steer, not queue

Today it queues and replays (§2.2). What a person means by "no wait, Tuesday" is
an amendment to the request in flight. `InboxKind.STEER` already exists
(`inbox.py:28`) and no channel uses it. Voice needs it most.

### 5f. Proposal: one explicit floor

Replace the five flags with a `ConversationFloor` owned by the voice runtime:

```
IDLE → USER_SPEAKING → AGENT_THINKING → AGENT_SPEAKING → IDLE
                ↑                            │
                └──────── BARGE_IN ──────────┘
```

Inputs: local VAD, Soniox partials / `<end>` / `<fin>`, playback position from
TTS timestamps. Side effects of transitions: mic gating, `Agent.abort()`,
`synthesizer.cancel()`, filler emission, memory truncation. Every proposal in §4
and §5 is a transition rule on this object. Without it, each one becomes another
boolean.

## 6. The room is not a socket

### 6a. Cost: the meter runs on the session, not the speech

Soniox real-time STT is $0.12/hour, and the manual-finalization doc states the
billing rule plainly: **"You are charged for the full stream duration, not just
the audio processed."** An always-open session is therefore ~$2.88/day, ~$86/month
per device, whether or not anyone is home. TTS, at $0.70 per *generated* hour,
is billed only for speech actually produced — a device that talks ten minutes a
day costs a few dollars a month. So on an idle device the ears cost roughly
twenty times the mouth.

This corrects an obvious-looking fix: gating audio frames while keeping the
websocket open saves bandwidth and privacy but **not money**. The lever is the
session lifecycle — close the STT session when the room has been quiet, reopen
on local VAD or a wake word. (Worth confirming against a real invoice before
sizing the work, but the docs are unambiguous.)

The same local VAD also means the room's audio stops leaving the house by
default, which is what "local-first" should mean on a device with a microphone.

### 6b. No attention model

The runtime answers *every* utterance: the television, a phone call, two other
people. An always-listening device needs an explicit "was that addressed to me"
decision, in three tiers, cheapest first: a wake word; an open-conversation
window (stay addressable for *N* seconds after an exchange, so no wake word
mid-conversation); and an LLM gate for the ambiguous remainder. Tier 3 already
exists — `Agent.decide_participation` (`agent.py:1011`) is this decision for
group chat, and a room with a microphone *is* a group chat with a lossy
transcript.

Soniox's STT `context` is the cheap accuracy lever underneath this: pass the
agent's own name and its contacts' names as `terms` so the wake word and the
names are recognized in the first place.

### 6c. Everyone in the room is `local_voice`

`enable_speaker_diarization=False` (`soniox.py:195`) and a fixed
`user_id="local_voice"` (`runtime.py:42`). Every person folds into one
relationship card, one diary voice, one set of preferences. GOAL.md principles 2
and 3 are non-negotiable, and the physical device is the most multi-user surface
the product has, yet it is the one least able to tell people apart. Feishu gets
attribution free from the transport and uses it; voice throws it away at a
config line.

Turning it on is one boolean, but the honest design has three parts, because
Soniox gives separation, not identity:

1. **Separation.** Tokens carry `speaker: "1" | "2" | …`, up to 15 per session.
   These are session-local labels that can flip as context accumulates, and they
   reset on every reconnect — including the 300-minute stream cap and every
   network blip.
2. **Binding.** Map a session label to a persistent `user_id` conversationally
   ("谁在说话？"), persist it in contacts, and re-establish cheaply after a
   reconnect. Treat the label as a hint with confidence; require corroboration
   before writing identity into a relationship card. Fall back to `local_voice`
   only when genuinely unknown.
3. **Room modelling.** Set `room_name` so multi-party voice goes down the same
   path as a Feishu group rather than a second mechanism.

**The honest trade-off:** the docs state that endpoint detection and manual
finalization both reduce diarization accuracy, because they force early
finalization — "for the highest diarization accuracy, do not use endpoint
detection." We cannot give up endpointing; it is what makes the conversation
responsive. So the correct reading is that voice diarization will be noisier
than Feishu's transport-level attribution, and the design must treat it that
way. Noisy separation is still categorically better than collapsing a family
into one person.

### 6d. Silent audio loss and a Python-loop resampler

The capture queue holds 32 blocks and drops on overflow with a bare `pass`
(`audio.py:674,699-701`). Under CPU pressure words vanish and nothing records
it; the symptom is "it misheard me" with no evidence anywhere. Count and log.

The CPU pressure is often self-inflicted: `_PCMResampler` (`audio.py:565-597`)
interpolates per sample in Python lists, and device scoring (`audio.py:241-367`)
will happily choose a 48 kHz stereo device and then convert to 16 kHz mono in
pure Python for every block, forever. Prefer a device and rate needing no
conversion; when unavoidable, use `audioop` or numpy.

## 7. Output — compose for the ear, not the eye

**First principle.** Speech is linear and ephemeral. The listener cannot skim,
re-read, or skip a list. A spoken answer is a *different answer*, not a
rendering of the text one.

### 7a. Nothing tells the model it is speaking

No channel instructions (`runtime.py:353-359`), no normalization anywhere. So
markdown is pronounced or unpredictably swallowed; URLs and paths are read
character by character; answers arrive as "三点：第一……第二……第三……", unusable
past two short items; `TURN_REPLY_PROMPT_TEMPLATE`'s "keep simple replies short"
(`core/config.py:265`) is calibrated for chat; and the same template instructs
the model to deliver files via `attach_artifact` (`core/config.py:271`), which
is meaningless on a speaker and leaks into the speech.

Two fixes, both needed. **Voice `channel_instructions`**: one idea per turn; two
or three short sentences unless asked for more; no lists or markup; do not read
URLs or paths aloud, offer to send them; say numbers and dates the way they are
spoken; end with a question only to hand the floor back. The plumbing exists —
`channel_instructions` is a first-class prompt section
(`prompt_registry.py:294-300`) and Feishu already uses it
(`feishu/adapter.py:2199`). And **a sanitizer anyway**, because prompts are
advisory while a normalizer is deterministic: strip markdown, code fences and
emoji on the delta stream in the runtime, not in the Soniox adapter.

### 7b. The TTS language is taken from the wrong speaker

`_speak` sets Soniox's `language` from the STT-detected language of the *user's*
utterance (`runtime.py:203`), majority-voted across its tokens
(`soniox.py:359-363`). The docs warn against precisely this: "Don't pick
`language` based on a few embedded words. It biases the accent of the entire
utterance." A Chinese sentence containing one English brand name can flip the
vote, and the agent's whole Chinese reply then gets English delivery. When the
user speaks English and the agent answers in Chinese, it is wrong by
construction.

`language` should come from **the language the agent actually replied in**, with
hysteresis so it tracks a stable conversation language rather than flipping per
turn, falling back to `fallback_language`. Two facts make this safe: every voice
works in every language, so the speaker identity never changes; and the model
handles embedded foreign words natively, so mixed replies need no splitting.

### 7c. Audio tags are the missing half of "a consistent self voice"

GOAL.md asks for "a consistent self voice as an independent entity". Today that
is a text-level property that TTS flattens into one register. `tts-rt-v2` audio
tags — `[warm]`, `[curious]`, `[serious]`, `[laughs]`, `[pause]` — make delivery
programmable while staying natural. Two uses, in increasing ambition: mark
runtime-generated speech (fillers, acknowledgements, failure lines) so they
sound human rather than robotic; and let the agent's own identity carry a
default tone, with tags emitted sparingly by the model itself. The docs are
specific about discipline: English tags only regardless of the spoken language,
one or two before a clause, simple tags only, and let the words carry the rest.
That last point argues for the runtime owning tags for system speech and keeping
the model's own use rare.

Voice cloning is the further end of the same axis: the agent could have one
distinct voice of its own rather than a catalogue entry named Owen.

### 7d. Rejected: buffering to sentence boundaries before TTS

Tempting, and wrong. The obvious complaint — that piping raw model deltas into
TTS must hurt prosody — is contradicted by how the API is built. Soniox
documents the opposite as the intended pattern: "Pipe each LLM token (or token
batch) straight into a Soniox text chunk … audio generation begins with the
first words … No buffering the full reply before speaking." The model streams
"before the sentence ends" by design. The current `_split_text_chunk`
(`soniox.py:352`), which only cuts at the 5000-character API limit, is correct
and should stay. If delivery sounds choppy, the lever is `reduce_silence` and
audio tags, not client-side chunking.

## 8. Memory under a microphone

- **The transcript is lossy and the diary is authoritative.** Writing ASR output
  into the diary as though it were what the person said bakes recognition errors
  into permanent first-person memory, with no later signal that a sentence was
  heard rather than read. Soniox returns `confidence` per token, and
  `_FinalToken` keeps only text and language (`soniox.py:59`). Carry confidence
  into message metadata and let memory maintenance treat low-confidence spans as
  uncertain ("I think he said…") instead of fact.
- **STT context should be live, not static YAML.** `SonioxSTTContextConfig`
  (`config.py:71-112`) is hand-written config, while the agent already knows its
  contacts' names, the last hour's topics, its own name, and its notebook terms.
  The API takes exactly these shapes — `general` key/values, free-form `text`,
  and `terms` — within an 8000-token budget, and the LiveKit reference agent
  builds its context the same way. Nothing improves perceived intelligence on a
  device more than getting a person's name right, and `language_hints_strict` is
  available for households that only ever use one or two languages.
- **Proactive speech has no time or presence guard.** Scheduled tasks and
  subconscious deliveries speak out of a physical speaker in a room
  (`runtime.py:407-427`). The only protection is prose in the subconscious
  prompt (`core/config.py:360`). A prompt is not an adequate guard for a 3 a.m.
  utterance in a bedroom. Voice needs a real quiet-hours window, a presence
  precondition (no speech heard for *N* hours → do not start talking), and
  coordination with the floor so a delivery never begins mid-utterance.

## 9. Failure — silence is the worst error message

An agent error is caught and printed to stdout (`runtime.py:181-185`), which
nobody reads on a headless device. STT reconnect backoff climbs to 30 s
(`soniox.py:34`) in silence. Every state a user could misread as "it's thinking"
must be audible and distinguishable: short spoken lines or earcons for *not
understood*, *network down*, *model error*, *still working*; never the same line
twice in a row; an audible "ears are back" after a reconnect; and a local
fallback when TTS itself is what is down.

Beyond the UX, reading Soniox's error and lifecycle contracts against the code
turns up four concrete bugs.

### 9a. A transient server error kills the channel permanently

`_RETRYABLE_ERROR_TYPES` is `{service_unavailable, max_duration_reached}`
(`soniox.py:35`), and `_is_recoverable_stt_error` returns `exc.retryable` for
any `SonioxVoiceError` (`soniox.py:381-382`). Soniox's catalogue also includes
`internal_error` (500, "the request may be retried"), `request_timeout` (408,
"retry the request"), `limit_exceeded` (429, "you may retry after a delay") and
`temp_api_key_session_expired` (403, mint a new key). All four are currently
classified fatal: the exception escapes `iter_utterances`, escapes
`run_forever`, and the voice channel exits. A single transient 500 leaves the
device deaf until someone restarts it by hand. The fix is to branch on
`error_type`, as the docs instruct ("Branch on this, not on `error_message`"),
with per-type recovery instead of one boolean.

### 9b. The TTS socket dies during long tool calls

`_speak` holds one TTS stream open for an entire agent turn including tool loops
(`runtime.py:212-283`). The TTS API requires "a keepalive (or any message) at
least every 20–30 seconds during idle gaps, otherwise the connection is closed
as idle," and nothing in `xagent/interfaces/voice/` ever calls `keep_alive()`.
A tool call longer than half a minute therefore drops the socket mid-reply and
the rest is never spoken. Two fixes, both worth doing: send keepalives, and stop
holding a synthesis stream across a tool call — speak per segment on a warm
multiplexed connection instead.

### 9c. Replies longer than two minutes are silently truncated

A TTS stream caps at two minutes of generated audio; past it the server returns
`413 max_audio_duration_reached`, delivers the last chunk *without* `audio_end`,
terminates the stream, and expects the client to "generate the remaining text on
a new stream." Nothing in `SonioxRealtimeTTS` handles this. Per-segment streams
(§9b) make it nearly unreachable, and the error still needs handling.

### 9d. Two sender threads can share one generator

`iter_utterances` reuses a single microphone generator across reconnects
(`soniox.py:86-122`) while `_iter_session` joins the previous sender with a 1 s
timeout (`soniox.py:176-180`). If the old sender is blocked in
`send_byte_chunk`, the new session starts a second thread iterating the same
generator: `ValueError: generator already executing`, or silently interleaved
audio. One reader should own the device and feed a session-scoped queue, so
session lifetime never aliases device lifetime.

### 9e. Two smaller ones

`pause(finalize=False)` (`soniox.py:214`) discards whatever partial speech was
in flight when the agent starts talking; the SDK default is `finalize=True`,
which flushes it first. And `tts-rt-v1` (`config.py:20`) was removed on
2026-08-31 and is being auto-routed to v2 — pin `tts-rt-v2` explicitly, which is
also the precondition for `reduce_silence` and audio tags.

Structurally, all of this is harder than it needs to be because the runtime is
asyncio while the adapters are the synchronous SDK driven from threads, bridged
by `_TextChunkQueue` (`runtime.py:502`), two sender threads and
`asyncio.to_thread`. The SDK ships full async clients (`AsyncSonioxClient`,
`AsyncRealtimeSTTSession`, `AsyncRealtimeTTSStream`). Moving to them deletes the
bridge, the join timeouts, and two whole bug classes.

## 10. Sequencing

**Phase 0 — configuration and hygiene, no architecture change.**
1. Voice `channel_instructions` + TTS sanitizer (§7a).
2. `tts-rt-v2`, `reduce_silence`, `return_timestamps`; TTS language from the
   reply, not the utterance (§7b, §9e).
3. Branch on `error_type`; retry 500/408/429 (§9a).
4. TTS keepalive; stop holding a stream across tool calls (§9b, §9c).
5. Enable diarization; carry `speaker` and `confidence` through to metadata
   (§6c, §8).
6. Audible failure and liveness signals (§9).
7. Log dropped capture frames; prefer a device rate that avoids the resampler
   (§6d).
8. Session-scoped capture queues; kill the generator aliasing (§9d).

**Phase 1 — the floor.**
9. `ConversationFloor` state machine replacing the five flags (§5f).
10. Utterance aggregation with a grace window after `<end>` (§5a).
11. AEC, or a duplex device that provides it, so the mic stays open (§5b).
12. Barge-in: local VAD + `min_words` classifier → `synthesizer.cancel()` +
    `Agent.abort()`, with memory truncated at the real playback cut using
    character timestamps (§5b–5d).
13. Steer instead of queue during THINK (§5e).

**Phase 2 — the room.**
14. Attention model: wake word + open-conversation window +
    `decide_participation` (§6b).
15. Speaker-to-`user_id` binding and room modelling (§6c).
16. Live STT context from contacts, topics and notebook terms (§8).
17. Session lifecycle for cost and privacy: close STT when the room is quiet
    (§6a).
18. Quiet hours and presence gate for proactive speech (§8).

**Phase 3 — polish and budget.**
19. Preemptive generation; local acknowledgement; warm TTS connection and output
    stream (§4).
20. Audio tags for system speech; consider a cloned voice for the agent (§7c).
21. Voice profile for the agent loop: lower `max_agent_loops`, spoken progress on
    long tool runs, a non-reasoning fast path for conversational turns.
22. Move to the async Soniox SDK and delete the thread bridge (§9).

Phase 0 is mostly configuration and error handling and fixes much of what a user
notices in the first five minutes. Phase 1 is what makes it feel like a
conversation.

## 11. The alternative worth naming: speech-to-speech

Everything above reconstructs what a native realtime speech-to-speech model
(OpenAI Realtime, Gemini Live, Qwen-Omni) provides in one socket: turn-taking,
barge-in, prosody, sub-second latency.

- **For it.** It solves §4, §5 and §7 nearly for free, and those are the three
  the user feels.
- **Against it.** Turn-taking *and* generation move into the vendor's model, so
  the identity prompt, diary injection, relationship cards, notebook recall and
  the tool loop must be re-expressed as that model's session config and function
  calls — the model that talks would not be the model this codebase has built a
  self for. It also pushes continuous audio to a single vendor.

Recommendation: keep the cascaded pipeline as the default, because preserving
the agent's identity and memory architecture *is* the product. The Soniox
capability ledger in §3 says the cascade is far from exhausted — most of what
makes conversation feel smooth is sitting unused in an API we already pay for.
Widen the seams so a speech-to-speech backend stays droppable: the existing
`VoiceMicrophone` / `VoiceRecognizer` / `VoiceSynthesizer` / `VoicePlayer`
protocols (`runtime.py:77-114`) are cut on the assumption of a cascade, whereas
cutting at the floor state machine (§5f) makes both designs expressible — a
`VoiceDialogModel` would own STT+LLM+TTS and report floor transitions, while the
cascade composes them locally.

## 12. Goal check (GOAL.md mandatory review)

- **Identity impact.** Positive. §7a gives the agent a consistent *spoken*
  register instead of read-aloud chat formatting; §7c lets tone and, optionally,
  a cloned voice carry that identity in audio; §8 keeps proactive speech under
  the agent's judgement rather than an unguarded timer.
- **Multi-user impact.** The main gap. §6c is a direct fix for a live violation
  of Principle 2: a physical device today collapses every person present into
  `local_voice`.
- **1:1 and group coverage.** §6b and §6c model a room as a room and reuse
  `decide_participation` rather than forking group logic for audio.
- **Memory/journal perspective.** §5d and §8 protect first-person accuracy:
  never record as said what was cut off, never record as certain what was barely
  heard.
- **Unified memory.** Unchanged. Voice stays one more channel writing into the
  same diary stream; no voice-local memory store is proposed.
- **Agent-governed sharing.** Unchanged for content. Quiet hours constrain when
  the agent speaks aloud in a shared physical space, not what it will say —
  though a shared room is itself a disclosure context, which is a further
  argument for passing speaker and room state into the turn (§6c).
- **Diary-anchored carrier.** Unchanged. Confidence and truncation markers are
  provenance on existing records, not a parallel store.
- **Attribution and continuity.** §6c restores who-said-what, currently absent
  on this channel; §5a and §5e keep one thought as one turn instead of splitting
  or duplicating it.

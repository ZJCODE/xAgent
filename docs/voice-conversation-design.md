# Voice conversation: first-principles analysis

Status: analysis and proposal. Nothing here is implemented yet. Written after
running the current voice channel on a physical device in a real room.

Scope: `xagent/interfaces/voice/` (runtime, soniox, audio, config, factory) and
the places in `xagent/core/` that the voice channel reaches into.

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

A spoken conversation is not a chat transcript delivered as audio. It differs on
seven load-bearing axes:

| Axis | Text chat | Spoken conversation |
| --- | --- | --- |
| Time | asynchronous; delay is free | latency is semantic — silence *means* something |
| Floor | implicit, message-atomic | negotiated continuously, interruptible at any instant |
| Channel | addressed, lossless, private | one shared acoustic space with the TV, other people, and your own speaker in it |
| Message | reviewable, skimmable, structured | linear, ephemeral, no scrollback, no markup |
| Identity | given by the transport | must be recovered from the acoustics |
| Failure | visible, re-readable | indistinguishable from silence |
| Cost | per turn | per second of wall time the mic is open |

Each row is a subsystem the current implementation does not have. Sections 3–9
take them one at a time. Section 10 asks whether the cascaded pipeline is the
right shape at all.

## 2. Ground truth: what the runtime actually does

The state machine is `LISTEN → THINK → SPEAK → 0.5 s cooldown → LISTEN`, strictly
serial. Four properties fall out of the code, and they matter for everything
below:

1. **The mic is dead during SPEAK.** The capture callback returns early whenever
   `pause_event` is set (`audio.py:696`), and the STT sender pauses the Soniox
   session (`soniox.py:212-218`). Nothing the user says while the agent is
   talking exists anywhere.

2. **The STT event stream is not read during THINK.** `run_forever` awaits the
   whole reply before pulling the next utterance (`runtime.py:173`), so the
   recognizer generator stays suspended at its `yield` inside `_iter_session`
   (`soniox.py:150-171`). Meanwhile the sender thread keeps shipping audio.
   Endpoints detected while the agent is thinking are not dropped — they queue,
   then fire back to back after the reply lands. This is the "it answered my
   last three sentences in a row" behaviour.

3. **The agent is called with no channel instructions.** `_agent_text_chunks`
   passes `user_message`, `user_id`, `stream`, `channel`, `inbox_kind` and
   nothing else (`runtime.py:353-359`). The model composes in exactly the
   register it uses for Feishu.

4. **Five overlapping stop flags coordinate the pipeline**: `pause_event`,
   `stop_event`, `playback_stop_event`, the synthesizer's `_cancel_event`, and
   the STT's `session_stop`. There is no single place that owns "who has the
   floor right now", so any new behaviour (barge-in, steering, fillers) has to
   be threaded through all five.

## 3. Time — latency is part of the message

**First principle.** In speech, the gap before a reply carries meaning. Human
turn-taking gaps cluster around 200 ms; beyond ~1 s a listener reads hesitancy,
beyond ~2–3 s they assume the exchange broke and start speaking again — which,
given §2.2, produces a duplicate turn. So latency is not a performance metric
here, it is a correctness metric.

Where the budget goes today, in order:

| Stage | Code | Cost |
| --- | --- | --- |
| Endpoint detection | `max_endpoint_delay_ms = 1500`, sensitivity `0.3`, latency level `2`, all hardcoded (`config.py:24-26`) | up to 1.5 s before the agent is even called |
| Context assembly | `_build_turn_context` — SQLite history, diary, relationship cards, notebook recall, skills catalog, serial awaits (`agent.py:334-379`) | 10s–100s ms, on SD-card storage more |
| Model TTFT | reasoning-capable default (`gpt-5.6-terra`) with a ≤16 kB instruction block | often the largest single term |
| Tool loops | up to 50 iterations (`DEFAULT_MAX_AGENT_LOOPS`), each a full round trip | unbounded, and completely silent |
| TTS connect | a fresh websocket per turn (`soniox.py:280`) | one RTT + handshake |
| Output device open | `RawOutputStream` opened per playback (`audio.py:757`) | device-dependent, plus a click |

The instrument already exists — `_TurnTiming` logs endpoint→first-text,
first-text→first-audio, and total (`runtime.py:47-74`). What is missing is a
*controller*: nothing reads those numbers and changes behaviour.

What to do, by payoff:

- **Separate acknowledgement from answer.** What the user feels is
  time-to-first-audio, not time-to-answer. A 300 ms locally-generated
  acknowledgement ("嗯" / "let me check") covers the entire assembly + TTFT
  window and costs no model call. This is the largest perceived-latency win
  available and it is nearly free.
- **Start assembling on the first partial, not on `<end>`.** Context assembly
  depends on the conversation so far, not on the last word of the current
  utterance. Kick it off at the first non-final token and cancel if the
  utterance turns out to be noise.
- **Own the endpoint decision.** With a local VAD (§5a) the runtime can call
  `session.finalize()` the moment it sees enough trailing silence, instead of
  waiting out `max_endpoint_delay_ms`. The SDK exposes `finalize()`
  (`soniox/realtime/stt.py:246`) and the code never calls it. Endpoint
  parameters should also be policy, not constants: shorter after a
  question-shaped utterance, longer after a trailing conjunction or mid-list.
- **Keep things warm.** Open the output stream once per process, not per turn.
  Hold one TTS connection open across turns — the SDK supports exactly this via
  `connect_multi_stream` / `keep_alive` (`soniox/realtime/tts.py:268,146`).
- **Enforce the budget.** If first audio has not happened by *N* ms, speak a
  filler. If a tool loop exceeds *M* seconds, say what is happening. A voice
  profile should also cap `max_agent_loops` far below 50.

## 4. Floor control — turn-taking is a decision, not a signal

**First principle.** Endpointing is not a property of the audio. It is a
*decision* about whether the speaker has yielded the floor, and it needs
prosody, syntax, semantics and dialogue state. Today that entire decision is
delegated to one vendor `<end>` token, and the recovery path for a wrong
decision is: none.

### 4a. Barge-in is architecturally impossible right now

The mic is hard-muted for the whole reply (`runtime.py:221`, `audio.py:696`). If
the agent starts a wrong 40-second answer, the only remedies are to wait it out
or Ctrl-C. This is the single most-reported defect of any voice assistant, and
here it is not an unimplemented feature — it is excluded by the design.

The primitives already exist and are unused: `SonioxRealtimeTTS.cancel()`
(`soniox.py:248`) and `Agent.abort()` / `AgentInbox.request_abort()`
(`inbox.py:124-133`), which the voice channel never calls. Worth noting the
pre-Soniox config had `enable_interruptions`, `wake_phrases` and `exit_phrases`;
the Soniox rewrite dropped all three (`cli/setup.py:395-398`).

### 4b. Muting is the wrong echo control

The reason for the mute is to keep the speaker out of the mic. The right tool
for that is acoustic echo cancellation — and this is the easy case, because the
reference signal is known exactly (we generated it). With AEC the mic can stay
open during playback, which is the *precondition* for barge-in. Without it,
half-duplex is forced, and `_PLAYBACK_MICROPHONE_COOLDOWN_SECONDS = 0.5`
(`runtime.py:27`) is a guess that is simultaneously too long for a fast exchange
and too short for a reverberant room.

Options, cheapest first: a hardware/driver AEC path (most USB conference
speakerphones and the Pi-oriented mic arrays do this on-device, which also
removes the resampling cost in §5d); `speexdsp`/WebRTC AEC in the capture path;
or, as a stopgap, energy-gated barge-in — allow interruption only when input
energy exceeds the played-back reference by a margin.

### 4c. Stopping on any sound is worse than not stopping

Once the mic stays open, "嗯", "对", "ok" must **not** interrupt. So barge-in
needs a classifier over {backchannel, correction, new turn}, built from energy +
VAD + the partial transcript. Get this wrong in the permissive direction and the
agent becomes unable to finish a sentence in a room with two people in it.

### 4d. A new utterance during THINK should steer, not queue

Today it queues and replays (§2.2). What a person does with "no wait, I meant
Tuesday" is amend the request in flight. `InboxKind.STEER` already exists in the
inbox enum (`inbox.py:28`) and no channel uses it. Voice is the channel that
needs it most.

### 4e. Barge-in changes what goes into memory

This is the non-obvious one. When playback is cut at 3 seconds into a 20-second
reply, the user heard three seconds — but `chat_events` has already stored the
entire `visible_text` as the assistant message (`agent.py:832-839`). The diary
and the working context then contain things the agent never actually said, and
the next turn reasons from them. Since the diary is the authoritative memory
carrier (GOAL.md principle 8), this quietly corrupts the memory stream.

Soniox TTS can return character-to-audio timestamps (`return_timestamps`,
`soniox/types/realtime.py:167`) — currently left `None`. With them, the runtime
knows the exact truncation point and can store *what was spoken*, with the
remainder marked as unsaid. Any barge-in design has to answer this question
before it ships.

### 4f. Proposal: one explicit floor

Replace the five flags with a `ConversationFloor` state machine owned by the
voice runtime:

```
IDLE → USER_SPEAKING → AGENT_THINKING → AGENT_SPEAKING → IDLE
                ↑                            │
                └──────── BARGE_IN ──────────┘
```

Driven by three inputs — local VAD, STT partials/endpoint, playback progress —
with mic gating, `Agent.abort()`, `synthesizer.cancel()`, filler emission and
memory truncation as *transition side effects* rather than ad-hoc booleans. Every
proposal in §3 and §4 becomes a transition rule on this object; without it, they
each become another flag.

## 5. Channel — the room is not a socket

### 5a. Always-on cloud streaming

Every frame goes to Soniox whenever the agent is not speaking, 24 hours a day.
Three consequences: cost scales with uptime rather than use; every sound in the
room leaves the house; a network blip takes the ears offline with backoff up to
30 s (`soniox.py:35`) and nothing tells anyone.

**First principle:** the cheapest, most private, lowest-latency place to decide
"is this speech at all" is on the device. A local VAD gate (webrtcvad, Silero)
in front of the sender removes most of the cost, most of the privacy exposure,
and most of the false triggers — and, per §3, gives the runtime its own
endpoint signal.

### 5b. No attention model

The runtime answers *every* utterance it hears. On a device in a shared room
that means the television, a phone call, and two other people's conversation.
An always-listening device needs an explicit "was that addressed to me?"
decision, in three tiers, cheapest first:

1. wake word (local, near-zero cost);
2. an open-conversation window — stay addressable for *N* seconds after an
   exchange, so no wake word is needed mid-conversation;
3. for ambiguous speech, an LLM gate.

Tier 3 already exists: `Agent.decide_participation` (`agent.py:1011`) is exactly
this decision for group chat. A room with a microphone *is* a group chat with an
unreliable transcript. The voice channel should reuse it rather than invent a
parallel mechanism.

### 5c. No speaker identity — and this one violates the product goal

`enable_speaker_diarization=False` (`soniox.py:195`) and a single fixed
`user_id="local_voice"` (`runtime.py:42`). Everyone in the room is folded into
one relationship card, one diary voice, one set of preferences.

GOAL.md principles 2 and 3 (Multi-User Distinction, Multi-Party + 1:1 Coverage)
are non-negotiable, and the physical device is the most multi-user surface the
product has — yet it is the surface least able to tell people apart. Feishu gets
speaker attribution for free from the transport and uses it; voice throws it
away at the config line.

The fix is available at the same line: Soniox tokens already carry `speaker`
(`soniox/types/common.py:26`). Turn diarization on, keep the per-token speaker
tag through `_utterance_from` (`soniox.py:359`), bind a tag to a `user_id` once
by asking ("谁在说话？") and persisting it in contacts, fall back to
`local_voice` only when unknown, and set `room_name` so multi-party voice is
modelled as a room the way Feishu groups are.

### 5d. Silent audio loss and a Python-loop resampler

The capture queue holds 32 blocks and drops on overflow with a bare `pass`
(`audio.py:674,699-701`). Under CPU pressure words disappear and nothing records
it — the symptom on a device is "it misheard me", with no evidence anywhere. At
minimum, count and log drops.

The reason for CPU pressure is often the resampler itself: `_PCMResampler`
(`audio.py:565-597`) interpolates per sample in Python lists, and the device
selection scoring (`audio.py:241-367`) will happily pick a 48 kHz stereo device
and then convert 48 kHz stereo → 16 kHz mono in pure Python, every block,
forever. Prefer a device/rate that needs no conversion; when conversion is
unavoidable, use `audioop` or numpy.

## 6. Output — compose for the ear, not the eye

**First principle.** Speech is linear and ephemeral. The listener cannot skim,
cannot re-read, cannot skip a list. So a spoken answer is a *different answer*
from the text answer, not a rendering of it.

Today nothing anywhere makes that distinction. The voice channel sends no
channel instructions (`runtime.py:353-359`), and no normalization happens
between the model and Soniox — `_split_text_chunk` only cuts at the 5000-char
API limit (`soniox.py:352`). The results on a device:

- Markdown is pronounced, or swallowed unpredictably: `**`, `#`, `-`, backticks.
- URLs, file paths, code and tables are read out character by character.
- Answers are structured for the eye — "三点：第一……第二……第三……" — which is
  unusable over audio past two or three short items.
- `TURN_REPLY_PROMPT_TEMPLATE` ("keep simple replies short", `core/config.py:265`)
  is calibrated for chat; "short" in text is still long out loud.
- The same template instructs the model to deliver files via `attach_artifact`
  (`core/config.py:271`) — meaningless on a speaker, and it leaks into speech.

Three fixes, all needed, in increasing order of effort:

1. **Voice channel instructions.** One idea per turn; two or three short
   sentences unless explicitly asked for more; no lists, no markup; do not read
   URLs or paths aloud, offer to send them; say numbers, dates and units the way
   they are spoken; end with a question only when you want the floor back. The
   plumbing already exists — `channel_instructions` is a first-class prompt
   section (`prompt_registry.py:294-300`) and Feishu already uses it
   (`feishu/adapter.py:2199`). This is the cheapest change in this document and
   it moves the experience more than anything except barge-in.

2. **A sanitizer, regardless.** Prompts are advisory; a normalizer is
   deterministic. Strip or convert markdown, code fences, emoji and URLs on the
   delta stream in the runtime — not in the Soniox adapter, which should stay
   dumb.

3. **Chunk on clause boundaries, not on arrival.** Model deltas are token
   fragments, and handing a synthesizer text with no syntactic boundary costs
   prosody. Buffer to the first sentence or clause boundary (or *N* chars ending
   at punctuation), then stream. The added latency is bounded and small; the
   output stops sounding like a stutter.

A fourth, structural: **do not hold one TTS stream across a tool call.**
`_speak` keeps a single synthesis stream open for the entire turn including tool
loops (`runtime.py:212-283`), so a 60-second tool run leaves an idle realtime
websocket in the middle of an utterance. Speak per segment; open a stream per
segment on a warm connection.

## 7. Memory under a microphone

- **The transcript is lossy and the diary is authoritative.** Writing ASR output
  into the diary as though it were what the person said bakes recognition errors
  into permanent first-person memory, and there is no later signal that a given
  sentence was heard rather than read. Soniox tokens carry `confidence`
  (`soniox/types/common.py:20`); `_FinalToken` keeps only text and language
  (`soniox.py:59`). Carry confidence through to message metadata, and let memory
  maintenance treat low-confidence spans as uncertain ("I think he said…")
  rather than as fact.
- **STT context should be live, not static YAML.** `SonioxSTTContextConfig`
  (`config.py:71-112`) is hand-written config. The agent already knows the names
  of its contacts, the topics of the last hour, its own name, and its notebook
  terms. Feeding those in per session is free accuracy on exactly the words that
  matter most — names — and nothing improves perceived intelligence on a device
  more than getting a person's name right. `language_hints_strict`
  (`soniox/types/realtime.py`) is also available and unused.
- **Proactive speech has no time or presence guard.** Scheduled tasks and
  subconscious deliveries speak out of a physical speaker in a room
  (`runtime.py:407-427`). The only protection is prose inside the subconscious
  prompt ("At night, avoid unsolicited messages", `core/config.py:360`). A
  prompt is not an adequate guard for a 3 a.m. utterance in someone's bedroom.
  The voice channel needs a real quiet-hours window and a presence
  precondition — no speech heard for *N* hours means do not start talking — plus
  coordination with the floor (§4) so a delivery never starts mid-user-utterance.

## 8. Failure — silence is the worst error message

An agent error is caught in `run_forever` and printed to stdout
(`runtime.py:181-185`). On a headless device nobody reads stdout. STT reconnect
backoff climbs to 30 s (`soniox.py:35`) in silence. A TTS failure loses the whole
reply audibly, even though the text was stored.

**First principle:** on an audio-only surface, every state the user could
misread as "it's thinking" must be audible and distinguishable. The minimum set:
a short earcon or spoken line for *not understood*, *network down*, *model
error*, and *still working*; never the same failure line twice in a row; an
audible "ears are back" after an STT reconnect; and a local fallback line when
TTS itself is what is down.

Two correctness bugs in the same area that only surface on a flaky physical
network:

- **Two sender threads can share one generator.** `iter_utterances` reuses a
  single microphone generator across reconnects (`soniox.py:86-122`) while
  `_iter_session` joins the previous sender with a 1 s timeout
  (`soniox.py:176-180`). If the old sender is blocked inside
  `send_byte_chunk`, the new session starts a second thread iterating the same
  generator: `ValueError: generator already executing`, or silently interleaved
  audio. Fix the ownership — one reader owns the device and feeds a
  session-scoped queue, so session lifetime never aliases device lifetime.
- **The sync SDK inside an async runtime.** The runtime is asyncio; the adapters
  are the synchronous SDK driven from threads, bridged by `_TextChunkQueue`
  (`runtime.py:502`), two sender threads, and `asyncio.to_thread`. The SDK ships
  full async clients (`AsyncSonioxClient`, `AsyncRealtimeSTTSession`,
  `AsyncRealtimeTTSStream`). Moving to them deletes the bridge, the thread-join
  timeouts, and both bug classes above.

## 9. Sequencing

**Phase 0 — cheap, no architecture change.**
1. Voice `channel_instructions`, TTS text normalization, clause-boundary
   chunking (§6).
2. Audible failure and liveness signals (§8).
3. Local VAD gate before the cloud sender (§5a).
4. Log dropped capture frames; prefer a device rate that avoids the Python
   resampler (§5d).
5. Session-scoped capture queues; kill the generator aliasing (§8).
6. Turn on diarization and carry `speaker` + `confidence` through (§5c, §7).

**Phase 1 — the floor.**
7. `ConversationFloor` state machine replacing the five flags (§4f).
8. AEC, or a duplex device that does it, so the mic stays open during playback.
9. Barge-in: VAD + partial-transcript classifier → `synthesizer.cancel()` +
   `Agent.abort()`, with backchannels excluded, and memory truncated at the
   actual playback cut using `return_timestamps` (§4a–4e).
10. Steer instead of queue for utterances that arrive during THINK (§4d).

**Phase 2 — the room.**
11. Attention model: wake word + open-conversation window + `decide_participation`
    (§5b).
12. Speaker-to-`user_id` binding, room modelling, confidence-aware memory
    (§5c, §7).
13. Quiet hours and presence gate for proactive speech (§7).

**Phase 3 — the budget.**
14. Local instant acknowledgement, speculative context assembly, warm TTS
    connection and output stream, client-side `finalize()` (§3).
15. A voice profile for the agent loop: lower `max_agent_loops`, spoken progress
    on long tool runs, a non-reasoning fast path for conversational turns.
16. Move to the async Soniox SDK and delete the thread bridge (§8).

Phase 0 is a week of unglamorous work that fixes most of what a user notices in
the first five minutes. Phase 1 is what makes it feel like a conversation.

## 10. The alternative worth naming: speech-to-speech

Everything above reconstructs, piece by piece, what a native realtime
speech-to-speech model (OpenAI Realtime, Gemini Live, Qwen-Omni) provides in one
socket: turn-taking, barge-in, prosody, paralinguistics, sub-second latency.

The honest trade:

- **For it.** It solves §3, §4 and §6 essentially for free, and those are the
  three the user actually feels.
- **Against it.** Turn-taking *and* generation move into the vendor's model. The
  identity prompt, diary injection, relationship cards, notebook recall and the
  tool loop would have to be re-expressed as that model's session config and
  function calls — which means the model that talks is not the model this
  codebase has carefully built a self for. It also pushes continuous audio to a
  single vendor, against the local-first premise in the README.

Recommendation: keep the cascaded pipeline as the default, because it is what
preserves the agent's identity and memory architecture — that is the product,
not the audio. But widen the existing seams so a speech-to-speech backend is a
drop-in for users who want the latency. The `VoiceMicrophone` / `VoiceRecognizer`
/ `VoiceSynthesizer` / `VoicePlayer` protocols (`runtime.py:77-114`) are already
the right idea; they are just cut at the wrong joint, because they assume the
cascade. Cutting instead at the floor state machine (§4f) makes both designs
expressible: a `VoiceDialogModel` implementation would own STT+LLM+TTS and report
floor transitions, while the cascade keeps composing them locally.

## 11. Goal check (GOAL.md mandatory review)

- **Identity impact.** Positive. §6 gives the agent a consistent *spoken* voice
  instead of read-aloud chat formatting, and §7 keeps proactive speech under the
  agent's own judgement rather than an unguarded timer.
- **Multi-user impact.** This is the main gap. §5c is a direct fix for a current
  violation of Principle 2: a physical device today collapses every person in
  the room into `local_voice`.
- **1:1 and group coverage.** §5b and §5c model a room as a room, reusing
  `decide_participation` rather than forking group logic for audio.
- **Memory/journal perspective.** §4e and §7 both protect first-person accuracy:
  never record as said what was cut off, never record as certain what was barely
  heard.
- **Unified memory.** Unchanged. Voice stays one more channel writing into the
  same diary stream; no voice-local memory store is proposed.
- **Agent-governed sharing.** Unchanged for content. §7's quiet hours constrain
  *when* the agent speaks aloud in a shared physical space, not what it is
  willing to say — though the room being shared is itself a disclosure context
  the agent should weigh, which argues for passing speaker/room state into the
  turn (§5c).
- **Diary-anchored carrier.** Unchanged. Confidence and truncation markers are
  provenance on existing records, not a parallel store.
- **Attribution and continuity.** §5c restores who-said-what, which is currently
  missing entirely on this channel; §4d keeps a corrected utterance attached to
  the turn it corrects instead of spawning a second one.

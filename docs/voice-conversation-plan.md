# Voice conversation: implementation plan

Status: proposal. Companion to `voice-conversation-design.md`, which argues
*what is wrong and why*. This document is *what to build, in what order, and how
we will know it worked*.

No code here. Every stage names the objects to create, the decisions to fix, the
numbers to start from, and the acceptance criteria that close it.

## 1. Define the target before the work

"Smooth conversation" is not a feeling we can build toward directly. Decomposed
into properties a listener actually detects, it is five things:

| Property | What the user notices when it is missing | Measured as |
| --- | --- | --- |
| Promptness | "why is it still thinking" | end-of-speech → first audio |
| Yielding | "it cut me off" / "it answered half my question" | turns dispatched while the user was still mid-thought |
| Stoppability | "I can't make it shut up" | speech onset → last audio sample |
| Brevity | "it's reading me an essay" | spoken duration per reply |
| Legibility of state | "did it even hear me?" | unexplained silences longer than budget |

These are the plan's real requirements. Everything below exists to move one of
these five numbers, and any proposed work that moves none of them is out of
scope.

Starting targets, to be revised once we have a baseline:

- **end-of-speech → first audio**: p50 ≤ 1.2 s, p95 ≤ 2.2 s. (Decomposes into
  silence→endpoint, which the Soniox endpoint settings and the aggregation grace
  window own, plus endpoint→first audio, which the agent pipeline owns. Target
  the second at p50 ≤ 600 ms.)
- **speech onset → last audio sample on barge-in**: ≤ 250 ms including fade.
- **spoken reply duration**: median ≤ 12 s, p90 ≤ 30 s.
- **premature dispatch rate** (user speaks again within 1.5 s of a dispatch):
  ≤ 10%.
- **unexplained silence**: zero gaps over 2.5 s with no audio and no earcon.
- **fatal channel exits**: zero per 30 device-days.

We can compute all of these today from data we already receive and throw away:
`_TurnTiming` has the pipeline timings, Soniox token `end_ms` plus
`total_audio_proc_ms` give the true end-of-speech instant on the audio clock,
and TTS audio duration gives reply length. Stage 1 therefore includes a metrics
sink, because a plan whose first act is not measurement is a plan that will
argue about taste later.

## 2. Five invariants the runtime must hold

These are the first-principles constraints. Each one is violated today, and each
is checkable in a unit test once the floor exists (§5).

1. **One floor holder.** The agent stops producing audio within the barge-in
   budget of a confirmed user turn-start. (Not "only one party makes sound" —
   overlap is normal in speech; what matters is who yields, and how fast.)
2. **No silent drops.** Every user utterance is dispatched, merged into a
   dispatch, or discarded with a recorded reason. Today speech during playback
   vanishes and speech during thinking silently queues.
3. **Spoken equals stored.** What the memory system records as said is what was
   audible in the room. Today a cut-off reply is stored in full.
4. **Proactive speech is permitted, not assumed.** The agent speaks unprompted
   only when the floor is idle, the hour allows, and someone has been present.
5. **Every state over budget is audible.** A state the user could mistake for
   "thinking" must announce itself.

## 3. Ordering principle

Order by *user-felt payoff per unit of risk*, not by architectural tidiness, and
push anything that needs a hardware decision as late as its dependencies allow.

Two scheduling observations drive the shape below:

- **Finishing the user's sentences is fixable without the architecture.**
  Utterance aggregation sits between STT and the agent and needs neither echo
  cancellation nor the floor state machine. It is one of the top two complaints
  and it can ship in stage 2.
- **Barge-in is the only item gated on hardware.** It needs acoustic echo
  cancellation, which is a procurement decision (§10). Start that decision now
  and run it in parallel with stages 1–2 so stage 3 is not blocked waiting.

## Stage 1 — Make it sound like speech

**User-felt goal:** it stops reading markdown out loud, stops lecturing, and
stops speaking Chinese with an English accent.

This stage is prompt, configuration and one small text module. No architecture,
no new dependency, fully reversible.

### 1.1 Voice channel instructions

Add a voice `channel_instructions` block. The plumbing exists
(`prompt_registry.py`, and Feishu already uses it); only the content is new.
Draft rules, to be tuned against recordings:

- One idea per turn. Two or three short sentences unless explicitly asked for
  more.
- No lists, headings, markup, code, tables, or emoji. If the answer is
  inherently a list, say the headline and offer to go through it.
- Do not read URLs, file paths, or IDs aloud; offer to send them instead.
- Speak numbers, dates, times and units the way a person says them.
- Do not announce structure ("three things:"); a listener cannot see the
  numbering and will lose it.
- End with a question only when you actually want the floor back.
- Never mention the channel, the microphone, or that you cannot show files.

Also scope out the parts of `TURN_REPLY_PROMPT_TEMPLATE` that only make sense in
a text channel — notably the `attach_artifact` delivery instruction, which is
meaningless on a speaker and leaks into the speech.

### 1.2 A streaming-safe TTS sanitizer

Prompts are advisory; a normalizer is deterministic. Both are needed.

The non-obvious constraint is that the sanitizer runs on the delta stream and
cannot see the future: `**` can arrive split across two deltas. So it needs a
small lookahead buffer with a **bounded** flush — hold at most a few characters
or a few tens of milliseconds, whichever comes first, then emit regardless. This
preserves the streaming property that Soniox's API is designed around (§7d of
the analysis) while still catching markup.

Scope: strip emphasis markers, headings, list bullets, code fences, emoji;
convert bare URLs and paths to a spoken placeholder. Keep it small and boring —
it is a safety net under the prompt, not a rewriter.

### 1.3 Fix the TTS language selection

Today `language` comes from the majority-voted STT language of the *user's*
utterance, which the Soniox docs specifically warn against because it biases the
accent of the whole reply. Replace with a `conversation_language` that:

- is derived from the language the **agent actually replied in**,
- carries hysteresis so it tracks the conversation rather than flipping per turn
  (switch only when consecutive turns agree, or when the user asks explicitly),
- falls back to `fallback_language`.

Two facts make this safe: every Soniox voice speaks every language, so speaker
identity never changes; and the model handles embedded foreign words natively,
so mixed replies need no splitting.

### 1.4 Soniox configuration corrections

- Pin `tts-rt-v2` explicitly. `tts-rt-v1` was removed on 2026-08-31 and is being
  auto-routed; being implicit here also blocks the next two items.
- Set `reduce_silence: true`. Trims inter-word and inter-sentence pauses without
  changing pronunciation speed — better perceived tempo than raising `speed`,
  which just sounds rushed.
- Set `return_timestamps: true` now, even though nothing consumes it until stage
  3. It costs nothing, and it forces the config-schema change in 1.5 early.

### 1.5 Unblock the config schema

`_LEGACY_VOICE_KEYS` currently turns `enable_interruptions`, `wake` and
`return_timestamps` into validation errors — the three keys this plan needs
most. Rescope that guard so it rejects only genuinely dead Qwen-era shapes, and
update the test that pins `return_timestamps` to `None`.

### 1.6 Metrics sink

Promote `_TurnTiming` from log lines to a structured record per turn, written
where the CLI can summarise it: end-of-speech instant (from token `end_ms` and
the audio clock), endpoint, first text, first audio, playback end, reply audio
duration, interruption flag, error class. This is what makes every later stage
arguable with numbers instead of impressions.

**Acceptance:** median spoken reply ≤ 12 s; zero markup artefacts audible across
the field-test script (§9); language of delivery matches the language of the
reply in all mixed-language scenarios; per-turn metrics land in a file.

## Stage 2 — Stop finishing my sentences, stop going quiet

**User-felt goal:** one thought gets one answer, and the device is never
silently doing nothing.

Still no architecture change. This stage is where the second-biggest complaint
gets fixed.

### 2.1 Utterance aggregation

Insert an aggregator between the recognizer and the agent. Today one `<end>`
becomes one complete agent turn, and Soniox's own docs note that the low-latency
endpoint preset in use "may split longer speech into more segments". That
mismatch is the whole defect.

Design:

- On `<end>`, do not dispatch. Start a grace window and keep the segment.
- If speech resumes within the window, append and wait for the next `<end>`.
- On expiry, dispatch the concatenation as one turn.
- Grace window is adaptive, starting around 300–400 ms: extend when the segment
  ends in a conjunction or filler ("而且", "然后", "所以", "uh"), shorten when it
  is question-shaped or a short imperative.
- Hard caps so a monologue still lands: max accumulated duration (~30 s) and max
  segments.

Note the aggregation window adds to the perceived gap, which is why stage 5's
preemptive generation matters — the LLM can run on the accumulating transcript
during the window, so the cost is hidden rather than paid.

### 2.2 Audible state and failure catalogue

A small table of short spoken lines, each with a category, a cooldown, and a
repeat cap: *didn't catch that*, *ears are offline*, *back online*, *still
working on it*, *something went wrong*. Rules:

- never the same line twice in a row — repetition is its own failure mode;
- "still working" fires on a timer during long tool loops, not once;
- reconnects announce their recovery, not their attempts.

Because these lines are short, fixed, and generated by the runtime rather than
the model, they are the natural first use of v2 audio tags — `[warm]`,
`[curious]` — so that the device's own utterances do not sound like a beep.

**Cache them to disk on first successful synthesis.** That gives a local
fallback for the one case that otherwise has no voice at all: TTS itself being
down.

### 2.3 Error taxonomy

Replace the boolean retryable/fatal split with a branch on Soniox's
`error_type`, as the docs instruct. Concretely, `internal_error` (500),
`request_timeout` (408) and `limit_exceeded` (429) are documented as retryable
and are currently fatal, so one transient server error takes the device deaf
until a human restarts it. `limit_exceeded` wants a longer backoff than a 500;
`temp_api_key_session_expired` wants a credential refresh, not a retry.

### 2.4 TTS connection lifecycle

Three related fixes:

- Send keepalives. The API closes idle connections after 20–30 s and nothing in
  the package ever calls `keep_alive()`, so any tool call over half a minute
  drops the socket mid-reply.
- Stop holding one synthesis stream across a whole agent turn. Speak per
  segment, on a warm multiplexed connection (5 streams per connection).
- Handle `max_audio_duration_reached` (2 minutes per stream) by continuing on a
  new stream. Per-segment streams make this nearly unreachable; handle it anyway.

### 2.5 Capture-path hygiene

Count and log dropped capture frames instead of `pass`, so "it misheard me"
leaves evidence. Prefer a device and sample rate that need no conversion, so the
per-sample Python resampler stays off the hot path on ARM hardware.

**Acceptance:** premature dispatch rate ≤ 10% on the field-test script; zero
unexplained silences over 2.5 s; a forced 500, a forced socket drop and a
60-second tool call each recover with the user hearing what happened; zero fatal
exits in a 72-hour soak.

## Stage 3 — Let me interrupt

**User-felt goal:** the agent can be stopped mid-sentence, and stopping it does
not corrupt what it remembers saying.

This is the architecture stage and the only one gated on hardware.

### 3.1 Echo cancellation (decision, then plumbing)

Muting is the wrong echo control; AEC is the right one, and this is the easy
case because the reference signal is the audio we generated. Recommendation:
**buy the problem away with a conference speakerphone or mic array that does AEC
on-device.** It is the cheapest path, it removes the software AEC dependency
entirely, and such devices usually expose a clean 16 kHz mono capture stream,
which also deletes the resampler concern from 2.5. Software AEC (`speexdsp` or
WebRTC) stays as the fallback for devices we do not control. See §10.

### 3.2 `ConversationFloor`

One state machine, replacing the five overlapping flags (`pause_event`,
`stop_event`, `playback_stop_event`, the synthesizer's `_cancel_event`, the STT
`session_stop`).

States: `IDLE`, `USER_SPEAKING`, `AGGREGATING`, `THINKING`, `SPEAKING`,
`EARS_DOWN`.

Inputs: local VAD speech start/stop; Soniox partials, `<end>`, `<fin>`;
aggregation timer; agent first-text / done / error; playback position; STT
session lost/restored; proactive-speech request.

Outputs, as transition side effects rather than ad-hoc calls: microphone gate,
STT session open/close and `finalize()`, LLM start/cancel/steer, TTS stream
open/cancel, notice emission, memory commit or truncation.

**Design rule: the floor is pure.** No sockets, no audio devices, no sleeps —
inputs are events, outputs are commands. That is what makes the five invariants
in §2 unit-testable on a machine with no microphone, which is the only way this
subsystem ever gets real test coverage.

### 3.3 Barge-in detection

Two-stage, following the split Soniox recommends — the server decides when a
turn *ends*, a local VAD decides when one *starts*:

- VAD arms on voiced audio exceeding a minimum duration (~250 ms).
- STT partials confirm with a minimum word count (~2 words) before the agent
  actually stops.

Requiring words is what keeps a cough, a door, or the dog from stopping the
agent. Backchannels ("嗯", "对", "ok") must not interrupt either: exclude short
utterances matching a per-language backchannel set. Getting this wrong in the
permissive direction is worse than having no barge-in at all, because the agent
becomes unable to finish a sentence in a room with two people in it.

On confirmed barge-in: fade playback out over 20–50 ms (a hard stop clicks),
`synthesizer.cancel()`, `Agent.abort()`, then transition to `USER_SPEAKING`.

Add false-interruption recovery: if a barge-in produces no dispatchable
utterance within about a second, the agent should say something short rather
than leave the room in silence it caused.

### 3.4 The spoken ledger and memory truncation

This is the part that is invisible until barge-in ships, and then corrupts
memory quietly.

`chat_events` stores the full `visible_text` as the assistant message, so a
reply cut at 3 seconds of 20 is remembered in full — and the diary is the
authoritative carrier. Build a ledger that maps character offsets to audio time
using Soniox's `return_timestamps`, and cross-references playback position, so
the runtime knows exactly which prefix was audible.

For persistence, prefer **provenance over rewriting**: keep the generated
message and attach a `spoken_through` marker, so context assembly and memory
maintenance can tell what reached the room from what the model merely produced.
This matches how the codebase already treats derived records — every one carries
its own provenance — and avoids restructuring the agent's storage path for a
channel-specific concern.

### 3.5 Steering instead of queueing

An utterance arriving during `THINKING` should amend the turn in flight, not
spawn a second one. `InboxKind.STEER` already exists and no channel uses it.
Minimum viable version: cancel the in-flight turn and re-dispatch the merged
transcript; the real version feeds the amendment into the running turn.

**Acceptance:** speech onset → last audio ≤ 250 ms; backchannels interrupt in
under 5% of cases; after any interruption, stored assistant text equals audible
text; invariants 1–3 covered by unit tests against the pure floor.

## Stage 4 — Know who is in the room

**User-felt goal:** it knows there is more than one of us, it does not answer the
television, and it does not wake the house at 3 a.m.

### 4.1 Speaker separation and binding

Turn on `enable_speaker_diarization` and carry `speaker` and `confidence`
through to message metadata. Then build the binding layer, because Soniox gives
separation, not identity:

- Session-local labels (`"1"`, `"2"`, …, up to 15) reset on every reconnect —
  including the 300-minute stream cap and every network blip.
- Bind a label to a persistent `user_id` conversationally (the agent asks, or a
  person self-identifies), persist it in contacts, and re-establish cheaply
  after a reconnect.
- Treat a label as a hint with confidence. Require corroboration before writing
  identity into a relationship card; until bound, keep the persisted id as-is
  but carry the label in metadata so attribution can be repaired retroactively.

Design for being wrong: the docs are explicit that endpoint detection reduces
diarization accuracy, and we are not giving up endpointing. So voice attribution
will be noisier than Feishu's, and the correct response is to make errors cheap
and repairable rather than to pretend they will not happen. Noisy separation is
still categorically better than collapsing a household into `local_voice`.

### 4.2 Attention

Three tiers, cheapest first:

1. **Open-conversation window.** After a dispatched turn, stay addressable for
   20–30 s. This covers nearly all real conversation and needs nothing new.
2. **Name in transcript.** Outside the window, require the agent's name. Since
   we are already transcribing, this needs no wake-word engine — but it does
   need the agent's name and its contacts' names in the Soniox `context.terms`
   so they are recognised in the first place.
3. **`decide_participation`** for the ambiguous remainder. It already exists for
   group chat, and a room with a microphone is a group chat with a lossy
   transcript.

A true local wake-word engine only becomes necessary if we want the STT session
*closed* while waiting, which is a cost question, not an attention question —
see next.

### 4.3 Session lifecycle for cost and privacy

Soniox bills real-time STT by stream duration, not by speech: "You are charged
for the full stream duration, not just the audio processed." An always-open
session is roughly $86/month per device whether or not anyone is home, against
about $0.70 per *generated* hour for TTS.

So gating frames while holding the socket open saves bandwidth and privacy but
not money. The lever is closing the STT session after the room has been quiet
and reopening on local VAD. That is a simple heuristic — no wake-word model
needed — and it also stops the room's audio leaving the house by default, which
is what local-first should mean on a device with a microphone.

Worth confirming against a real invoice before sizing the work, but the
documented billing rule is unambiguous.

### 4.4 Proactive output deferred

Scheduled tasks and subconscious deliveries do not speak in the current voice
mode. This keeps the first release turn-based: the microphone listens and the
agent replies only after an explicit user turn. Revisit proactive output as a
separate product decision after the basic conversation loop is stable.

## Stage 5 — Make it fast

**User-felt goal:** the gap after you stop talking stops being noticeable.

By this point the conversation is correct; this stage makes it quick. It comes
last because latency work on a pipeline that still has the wrong turn-taking
semantics optimises the wrong thing.

- **Preemptive generation.** Soniox streams non-final tokens throughout the
  utterance. Start context assembly and the LLM call on the evolving transcript
  and hold TTS until the turn is confirmed; cancel and re-run if the final
  transcript diverges. This removes the assembly and TTFT terms from the
  perceived gap, and it is what pays back the aggregation window from 2.1.
- **Instant acknowledgement.** What the user feels is time-to-first-*audio*. If
  first audio has not happened by roughly 400 ms, emit a short locally-decided
  filler. With audio tags it sounds like a person thinking, not a chime. Use
  sparingly — a filler on every turn becomes a tic.
- **Warm pipes.** Open the output device once per process instead of per
  playback. Hold one TTS connection across turns and open a stream per segment.
- **A voice profile for the agent loop.** Cap `max_agent_loops` far below 50,
  emit spoken progress on long tool runs, and consider a non-reasoning fast path
  for conversational turns.
- **Endpoint settings as policy.** With local VAD in the loop, revisit
  `max_endpoint_delay_ms` (the Soniox + LiveKit reference agent runs 1000 ms) and
  make sensitivity adapt to the shape of the partial transcript.
- **A voice of its own.** Optionally clone a voice so the agent is a specific
  person rather than a fixed catalogue entry such as Daniel. This belongs to identity, not
  latency, but it is the last thing standing between the current output and
  something that sounds like someone.

**Acceptance:** end-of-speech → first audio p50 ≤ 1.2 s, p95 ≤ 2.2 s.

## 9. How we will know: the field-test script

Numbers from logs cannot tell us whether it feels smooth. Run the same fixed
scenario list in a real room before and after every stage, and record it:

1. Short question, immediate answer.
2. Long question with a two-second thinking pause in the middle.
3. Question ending in a trailing conjunction ("我想问一下，然后……").
4. Interrupt the agent three seconds into a long answer.
5. Say "嗯" and "对" while the agent is talking — it must not stop.
6. Correct yourself mid-request ("订周一……不对，周二").
7. Two people talking to each other, neither addressing the agent.
8. Two people alternately addressing the agent.
9. Television or music playing throughout.
10. Ask for something that requires a long tool call.
11. Ask for a URL or a file path.
12. Ask something whose honest answer is a list of six items.
13. Speak Chinese with an English product name embedded.
14. Unplug the network mid-reply; restore it.
15. A scheduled task due at 03:00.
16. Speak from across the room, off-axis from the microphone.

Scenarios 4, 5, 7 and 9 are the ones that distinguish a voice interface from a
demo. Each stage's acceptance criteria should be read against this list, not
against unit tests alone.

## 10. Decisions to make before stage 3

- **Echo cancellation path.** Recommendation: a USB conference speakerphone or
  mic array with on-device AEC. Rationale: it is the cheapest route to barge-in,
  it removes a signal-processing dependency from the codebase, and the clean
  16 kHz mono capture it provides also retires the resampler problem. Software
  AEC remains the fallback for hardware we do not control. This decision gates
  stage 3, so make it during stage 1.
- **Whether voice ships as one channel or two profiles.** A headset (near-field,
  single user, push-to-talk plausible) and a room device (far-field, multi-user,
  always-on) want different defaults for endpointing, attention and diarization.
  Recommendation: one implementation, two config presets, decided now so the
  config surface in §11 is shaped for it rather than retrofitted.
- **Whether the STT session closes when idle.** Turns on the billing question in
  4.3. Recommendation: confirm against an invoice during stage 1, so stage 4 can
  be scoped.

## 11. Config surface

The stages above add real user-facing choices, and the current flat schema has
nowhere to put them. Proposed grouping, so the shape is agreed before the
settings arrive piecemeal:

- `audio` — device selection plus `echo_cancellation: device | software | none`.
- `turn` — endpoint sensitivity, latency level, max endpoint delay, aggregation
  grace window, max utterance length.
- `interruption` — enabled, minimum speech duration, minimum words, backchannel
  handling.
- `attention` — mode (always / open window / name required), window length, wake
  terms.
- `presence` — idle timeout before closing the STT session.
- `proactive` — reserved for a future opt-in output mode.
- `speakers` — diarization on/off, what to do with an unbound speaker.

Keep defaults good enough that an untouched config gives a good room experience;
these knobs are for tuning a specific room, not for making the thing work.

## 12. Module shape

The current package puts orchestration, device I/O and vendor adapters in three
files with the policy spread across all of them. The stages above add policy
that needs to be testable without hardware, so the seams should move:

- `floor.py` — the pure state machine (§3.2). No I/O.
- `aggregator.py` — utterance aggregation (§2.1).
- `speech_text.py` — sanitizer, language selection, audio tags (§1.2, §1.3).
- `spoken_ledger.py` — character timestamps ↔ playback position ↔ truncation
  (§3.4).
- `attention.py` — addressed-to-me decision (§4.2).
- `speakers.py` — diarization label ↔ `user_id` binding (§4.1).
- `notices.py` — the audible state catalogue with cooldowns and local cache
  (§2.2).
- `audio.py` — device I/O and the AEC hook.
- `soniox.py` — adapters, kept deliberately dumb, ideally on the async SDK.
- `runtime.py` — thin wiring, which is what it should have been.

The async migration (the SDK ships `AsyncSonioxClient` and async STT/TTS
sessions) is not a stage of its own. Fold it into stage 3, where the floor is
being written anyway: doing it then deletes the thread bridge, the join
timeouts, and two bug classes at the same time as the code that depends on them
is rewritten.

## 13. Risks

- **Barge-in tuned too permissively** makes the agent unable to complete a
  sentence — worse than no barge-in. Mitigation: ship it behind a config flag,
  require words not just energy, and measure scenario 5 before enabling by
  default.
- **Diarization noise contaminating relationship memory** is the failure with the
  longest half-life, because the diary is authoritative. Mitigation: provisional
  attribution with corroboration before any card is written, and repairability
  as an explicit requirement rather than an afterthought.
- **The aggregation window makes latency worse before stage 5 makes it better.**
  Mitigation: keep the window small initially and accept the trade — answering
  the whole question late beats answering half of it early — then recover the
  time with preemptive generation.
- **AEC hardware that does not behave as advertised.** Mitigation: buy one and
  run the field-test script against it before stage 3 starts, not after.
- **Scope drift into a general voice framework.** LiveKit and Pipecat already
  exist. The reason to build this here is that the agent's identity, memory and
  tool loop are the product; the floor is the minimum needed to serve them.
  Anything beyond the five properties in §1 should be questioned.

## 14. Goal check (GOAL.md mandatory review)

- **Identity impact.** Positive. Stage 1 gives the agent a consistent spoken
  register; stage 5 optionally gives it a voice of its own; stage 4 keeps
  proactive speech under judgement rather than a timer.
- **Multi-user impact.** Stage 4.1 is the direct remedy for the current
  violation of Principle 2, where a physical device collapses every person
  present into `local_voice`.
- **1:1 and group coverage.** Stages 4.1 and 4.2 model a room as a room and
  reuse `decide_participation` rather than forking group logic for audio.
- **Memory/journal perspective.** Stage 3.4 enforces "spoken equals stored";
  stage 4.1 carries transcription and speaker confidence so uncertain input is
  not remembered as certain.
- **Unified memory.** Unchanged. Voice remains one channel writing into the same
  diary stream; no voice-local memory store is introduced.
- **Agent-governed sharing.** Unchanged for content. Quiet hours constrain when
  the agent speaks aloud in a shared physical space, not what it is willing to
  say — though a shared room is itself a disclosure context, which is a further
  reason to pass speaker and room state into the turn.
- **Diary-anchored carrier.** Preserved. `spoken_through` markers and confidence
  are provenance on existing records, not a parallel store.
- **Attribution and continuity.** Stage 4.1 restores who-said-what; stages 2.1
  and 3.5 keep one thought as one turn instead of splitting or duplicating it.

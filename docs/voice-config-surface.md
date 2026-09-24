# Voice config surface: what to expose, what to hide

Status: implemented. `channels.voice` grew from 6 keys to 34 during stages 1–5,
was curated down to 11, and is now 4.

Scope: which of the knobs in `xagent/interfaces/voice/config.py` belong in the
`config.yaml` that `xagent init` / `xagent voice setup` writes, which stay legal
but unwritten, and which should stop being config at all.

## 1. The test

For an agent that perceives and remembers, every config key is a claim that the
agent cannot work something out for itself. Only three kinds of setting survive
that claim:

1. **A credential.** Unobservable from inside the room; it can only be told.
2. **A boundary whose violation cannot be undone.** Waking a household, spending
   money, streaming a room to a vendor. Surface these even when the default is
   good, because the failure is expensive and silent.
3. **A fact that cold start cannot observe.** Something the agent would work out
   after a few minutes of listening, but which must be right on the first turn.

Everything else is one of three other things, none of them config: a property of
the hardware, which can be detected; a preference of a particular person, which
belongs in memory; or an invariant with one correct answer, which is a constant.

Two hard exclusions on top:

- **Never expose a value whose wrong setting corrupts memory or breaks an
  invariant.** The user cannot be expected to know that `return_timestamps` is
  what keeps the diary from claiming the agent said sentences it was cut off
  before reaching.
- **Never expose a value the user cannot measure.** `wake_energy_rms: 450` asks
  someone to type a PCM RMS figure for their living room.

## 2. What the generated config contains

```yaml
channels:
  voice:
    api_key: your_soniox_api_key_here
    languages: [zh, en]         # languages actually spoken here; the first is
                                # also the TTS language when a reply is unclear
    quiet_hours: "22:00-07:00"  # never start talking unprompted inside this
                                # window; "" disables it
    voice: Owen                 # speaking voice, used wherever the agent is heard
```

Why each earns its line:

- **`api_key`** — test 1. Required, no default possible.
- **`quiet_hours`** — test 2, and the only one of these whose wrong value cannot
  be taken back. A physical speaker in a bedroom at 3 a.m. is not recoverable by
  editing the file the next morning. One `"22:00-07:00"` string reads like a
  setting where two integer keys read like an implementation detail.
- **`languages`** — test 3. Soniox accuracy depends on it from the first
  utterance, before there is any conversation to infer it from. A household that
  speaks one language should not pay the accuracy cost of hints for two. One
  question, one answer: splitting it into `language_hints` plus
  `fallback_language` allowed contradictory pairs such as hints `[en]` with
  fallback `zh`.
- **`voice`** — GOAL.md asks for a consistent self, and the name still follows
  the agent wherever it is heard. It lives in the channel block because not
  every agent is heard aloud: an agent that never speaks carries no voice
  setting at all, and the one place a voice is actually used is the place it
  is configured.

`to_public_dict` always writes the curated settings, defaults included —
`audio` appears only once a device has been pinned. Echoing defaults back into
the file is how a curated surface grows again on the next `xagent voice setup`.

## 3. Detected, not configured

**`profile` and `interruptions`.** `detect_audio_topology` (`audio.py`) reads the
devices that were actually selected and answers two questions the old `profile`
boolean conflated:

- **near-field?** Drives diarization, attention window, participation gating,
  barge-in thresholds and Soniox endpointing.
- **echo-managed?** Whether playback is prevented from reaching the microphone.
  This is the precondition for barge-in, and it is a different axis: a
  conference speakerphone is far-field but echo-managed, a laptop mic with
  laptop speakers is neither.

Both are session-level facts, not install-level ones. The same machine moves
between earbuds and a room speaker within a day, so a value written into
`config.yaml` is wrong half the time, while a detection that is wrong once is
corrected by the next session. The decision is logged, and `--profile` and
`--interruptions` override it for a session.

| Behaviour | `headset` | `room` |
| --- | --- | --- |
| `enable_diarization` | false | true |
| `attention.open_window_seconds` | 120 (always addressable) | 25 |
| `attention.use_decide_participation` | false | true |
| `interruption.min_words` | 1 | 2 |
| `interruption.min_speech_ms` | 150 | 250 |
| aggregation grace window | 0.85 | 1.0 |
| Soniox `endpoint_sensitivity` / `max_endpoint_delay_ms` | tighter | vendor preset |

Detection can misjudge echo cancellation, and that failure is loud: the agent
interrupts itself for the rest of the session. `SelfInterruptionGuard`
(`echo_guard.py`) recognises the symptom — the words that interrupt are the
words being spoken — and disables duplex capture after the second such partial.
An automatic decision needs a way to be proven wrong; otherwise it is only a
config problem the user can no longer see.

## 4. Remembered, not configured

**`names`** was a hand-maintained second copy of what the agent already knows.
Contacts carry display names and relationship cards carry a name, so
`context_terms.py` reads them and refreshes the recognizer's context when the
runtime starts. GOAL.md principle 8 makes derived views regenerable projections
rather than parallel sources of truth; a YAML name list was exactly a parallel
source. Wake terms stay limited to the agent's own name, so a mention of
somebody else in the room does not grab the floor.

**`speed`** has no single right answer in a room with more than one listener,
which is the default assumption for a `room` profile. It is a preference about a
person, and the agent has per-person memory. `--speed` remains for a session.

## 5. Constants, not configuration

- **`idle_shutdown_minutes`** — the recognizer socket bills while open (roughly
  \$2.88/day per device, design doc §6a) and streams the room while it does, and
  local energy reopens it, so holding it through silence buys nothing. Closing
  after two quiet minutes is now unconditional. The old default of `0` did not
  merely leave that choice to the user; it left the entire wake-energy gate
  switched off, and with it the `require_recent_speech` guard that depended on
  the controller being enabled.
- **`return_timestamps`** — the spoken ledger's input. False leaves
  `SpokenLedger` with no character timeline, so a reply cut off after three
  seconds is still stored whole and the diary holds sentences the agent never
  said. That is a memory-integrity invariant, not a preference.
- **`aggregate_utterances`** — exists to fix "it answered half my question";
  false reinstates the bug.
- **`presence.wake_energy_rms`**, **`performance.*`** — latency and threshold
  implementation details with one correct answer each. If one turns out to need
  per-device tuning, that is evidence for a third profile, not for seven more
  lines in everyone's config.

## 6. Schema shape

`channels.voice` accepts `api_key`, `languages`, `quiet_hours`, `voice` and
`audio`; anything else is rejected with a close-match hint. `audio` is accepted but not
written: on a fixed appliance the selected device is a stable machine-level fact
that has to survive a restart, and the launcher restarts the channel without
flags. Everywhere a human is at a terminal, `--input-device` / `--output-device`
and `xagent voice --list-devices` are the escape hatch.

There is no advanced tier of YAML keys. Overrides are session-level CLI flags,
so that an override is a decision someone makes now with the device in front of
them, rather than a line that outlives the situation that justified it.

## 7. Goal check (GOAL.md mandatory review)

- **Identity impact.** Positive. `channels.voice.voice` keeps the audible self
  with the only place the agent is heard, so the agent sounds like one entity
  across devices, while an agent without a voice channel carries no voice
  setting at all. Latency tics stop being per-install choices.
- **Multi-user impact.** `enable_diarization` follows the detected profile and
  defaults to on, so speaker separation cannot be switched off by a user who
  does not know principle 2 depends on it.
- **1:1 and group coverage.** `headset` and `room` remain the axis; it is now
  answered by the hardware rather than asked of the user.
- **Memory/journal perspective.** `return_timestamps` and `aggregate_utterances`
  stay off the user surface specifically to protect what gets recorded as said:
  no unspoken text stored as spoken, no half-question stored as a whole turn.
- **Unified-memory impact.** Improved. Removing `names` removes a parallel store
  of who the agent knows.
- **Agent-governed sharing.** `quiet_hours` constrains when the agent speaks
  aloud in a shared physical space, never what it is willing to say. It now
  honours minutes, which it previously accepted and discarded.
- **Diary-anchored carrier.** Unchanged; no setting here adds a parallel store.
- **Attribution and continuity impact.** Names derived from relationship cards
  improve recognition at the source, and diarization stays on by default so
  who-said-what survives into the diary. Fixing `require_recent_speech` is a
  continuity fix too: the agent no longer speaks into a room it has not heard
  from in days.

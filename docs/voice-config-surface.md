# Voice config surface: what to expose, what to hide

Status: implemented (Tier 1 surface, profile presets, setup preservation). Written after stages 1–5 landed and
`channels.voice` grew from 6 keys to 34.

Scope: which of the knobs in `xagent/interfaces/voice/config.py` belong in the
`config.yaml` that `xagent init` / `xagent voice setup` writes, which stay legal
but undocumented, and which should stop being config at all.

## 1. Where the surface stands

`VoiceChannelConfig` accepts 34 settings across six nested models. The paths a
user can actually reach them by:

| Path | What it exposes |
| --- | --- |
| Generated `config.yaml` | nothing — `_config_yaml` writes `channels.api` only |
| `xagent voice setup` (CLI + web wizard) | `enabled`, `api_key` |
| `VOICE_CONFIG_EXAMPLE` (shown only in a migration error) | `api_key`, `voice`, `language_hints`, `fallback_language`, `speed`, `context`, `audio` |
| Hand-written YAML | all 34 |

So 32 of 34 settings are discoverable only by reading the source, and
`model_config = ConfigDict(extra="forbid")` means a guessed key name takes the
channel down with a pydantic dump. A configuration surface nobody can find is
not a feature; it is a support channel.

Two defects follow from the same gap and should be fixed with whatever comes out
of this note:

- **Setup silently erases hand-written tuning.** `_voice_channel_config`
  (`cli/setup.py:542`) and `prepare_voice_preset_update`
  (`cli/config_editor.py:507`) rebuild the block from a `preserved_keys`
  whitelist of six names. Re-running `xagent voice setup` to rotate an API key
  therefore deletes `attention`, `presence`, `proactive`, `performance`,
  `interruption`, `enable_interruptions`, `enable_diarization`,
  `aggregate_utterances`, `return_timestamps` and `room_name`. The user gets no
  warning and the symptom appears later as "it started answering the TV again".
  Invert it: preserve the whole mapping, rewrite `api_key` only.
- **`extra="forbid"` has no recovery path.** A typo should name the nearest legal
  key, not print a validation tree.

## 2. Three tests for "does this belong in the default file"

1. **Only the user knows the answer.** Room acoustics, who lives there, which
   languages get spoken, when the household sleeps, which USB device to use. No
   default can be right, so ask.
2. **A wrong default costs something the user cannot undo.** Money on an
   always-open STT socket, a speaker talking at 3 a.m., the wrong person written
   into the diary. Surface these even when the default is good, because the
   failure is expensive and silent.
3. **Could a good default ever be worse for a real user?** If no, it is not
   config — it is a constant we have not admitted to yet. If the honest answer is
   "it depends, together with four other knobs", it is not a knob either; it is a
   profile.

Two hard exclusions on top:

- **Never expose a value whose wrong setting corrupts memory or breaks an
  invariant.** The user cannot be expected to know that `return_timestamps` is
  what keeps the diary from claiming the agent said sentences it was cut off
  before reaching.
- **Never expose a value the user cannot measure.** `wake_energy_rms: 450` asks
  someone to type a PCM RMS figure for their living room.

## 3. Tier 1 — write these into the generated config, with comments

Eleven lines, in the style of the existing `agent:` block, which already ships a
curated subset with inline comments rather than every `AgentConfig` constant.

```yaml
channels:
  voice:
    api_key: your_soniox_api_key_here
    profile: room              # room = far-field, several people; headset = near-field, one person
    voice: Owen                # Soniox TTS voice
    speed: 1.0                 # 0.7-1.3; prefer leaving this at 1.0
    language_hints: [zh, en]   # languages actually spoken here
    fallback_language: zh      # TTS language when the reply language is unclear
    names: []                  # people, places, product names to recognise correctly
    interruptions: false       # let people cut a reply off; needs an echo-cancelling speakerphone
    quiet_hours: "22:00-07:00" # never start talking unprompted inside this window; "" disables
    idle_shutdown_minutes: 0   # close the mic session after N quiet minutes; 0 = always listening
    audio:
      input: auto
      output: auto
```

Why each one earns its line:

- **`api_key`** — required, no default possible.
- **`profile`** — new, and the main proposal in this note; see §4.
- **`voice`, `speed`, `language_hints`, `fallback_language`** — this is the
  agent's spoken self. GOAL.md asks for "a consistent self voice"; the voice name
  and the fallback language are the audible half of identity, and a household
  that speaks one language should not pay the accuracy cost of hints for two.
  Keep `speed`; comment against touching it, because `reduce_silence` is the
  right lever for tempo and raising `speed` just sounds rushed.
- **`names`** — today `context.terms`. Nothing improves perceived intelligence on
  a device more than getting a person's name right on the first try, and this is
  the only knob that does it. `terms` is vendor vocabulary; `names` says what to
  type. Keep `context.general` / `context.text` as the advanced form.
- **`interruptions`** — the feature users ask for first, and the one with a
  hardware precondition. `enable_interruptions: true` opens the mic during
  playback while `echo_cancellation` does nothing (§5), so on laptop speakers the
  agent interrupts itself. The comment is where that precondition gets stated;
  hiding the knob just moves the surprise later.
- **`quiet_hours`** — a physical speaker in a bedroom. `22`/`7` as two integer
  keys reads like an implementation detail; one `"22:00-07:00"` string reads like
  a setting, and per the design doc the current protection is otherwise prose in
  a prompt.
- **`idle_shutdown_minutes`** — today `presence.close_stt_after_idle_seconds`,
  defaulting to 0, i.e. an always-open recognizer. Per §6a of the design doc
  that is roughly \$2.88/day per device whether or not anyone is home, and it is
  also the difference between a microphone that streams the room continuously and
  one that does not. A recurring bill and a privacy posture both belong in the
  visible file. Minutes, not seconds: nobody wants to type `600`.
- **`audio.input` / `audio.output`** — already the most common hand-edit, and
  `--input-device` proves people need it. Keep `auto` as the default so the file
  documents that the override exists.

## 4. `profile` instead of ten correlated knobs

§10 of the implementation plan already decided voice ships as "one
implementation, two config presets". This is where that pays off. A headset and a
room device want opposite values for endpointing aggressiveness, diarization,
attention and barge-in thresholds — and no user can set those ten values
coherently, because they only make sense as a set.

| Setting | `headset` | `room` |
| --- | --- | --- |
| `enable_diarization` | false | true |
| `attention.open_window_seconds` | large (always addressable) | 25 |
| `attention.use_decide_participation` | false | true |
| `interruption.min_words` | 1 | 2 |
| `interruption.min_speech_ms` | 150 | 250 |
| aggregation grace window | short | default |
| Soniox `endpoint_sensitivity` / `max_endpoint_delay_ms` | tighter | vendor preset |

Two things follow. First, `enable_diarization` should not be a casual boolean in
a user's file: switching it off collapses a family into `local_voice` and
violates GOAL.md principle 2, so let the profile own it. Second, when the frozen
`SONIOX_ENDPOINT_*` constants become policy (plan §11's `turn` group), they
should be driven by the profile and **not** promoted to user config. Endpoint
sensitivity cannot be tuned by ear; a user changing it has no way to tell
improvement from regression.

## 5. Tier 3 — stop calling these config

- **`audio.echo_cancellation`** — declared, validated against three values, and
  never read anywhere in the package. It is the precondition for the one Tier 1
  knob with a hardware caveat, so it reads as if enabling barge-in on a laptop
  were safe. Implement it or delete it; a knob that does nothing is worse than no
  knob.
- **`return_timestamps`** — the spoken ledger's input. Setting it false leaves
  `SpokenLedger` with no character timeline, so a reply cut off after three
  seconds is still stored whole and the diary holds sentences the agent never
  said. That is a memory-integrity invariant, not a preference. Hardcode true, or
  derive it from `interruptions`.
- **`aggregate_utterances`** — exists to fix "it answered half my question";
  false reinstates the bug. Fold the window into `profile` and drop the boolean.
- **`presence.wake_energy_rms`** — auto-calibrate from ambient noise over the
  first seconds of capture. Never ask for an RMS integer.
- **`performance.warm_output_device`, `preemptive_min_chars`,
  `ack_cooldown_seconds`, `speak_tool_progress`, `preemptive_generation`,
  `instant_ack`, `ack_delay_ms`** — latency implementation details with one
  correct answer each. Keep them as constants or debug-only overrides. If any
  turns out to need per-device tuning, that is evidence for a third profile, not
  for seven more lines in everyone's config.

## 6. Tier 2 — legal, documented, not written into the file

Everything remaining stays accepted by the schema and gets documented as advanced
tuning: `attention.*`, `interruption.min_*`, `presence.recent_speech_hours`,
`proactive.max_per_hour`, `proactive.require_recent_speech`,
`performance.max_agent_loops`, `context.general`, `context.text`, `room_name`.
These are real tuning for a specific room, which is exactly the plan's own test:
"defaults good enough that an untouched config gives a good room experience;
these knobs are for tuning a specific room, not for making the thing work."

## 7. Schema shape (no legacy paths)

`channels.voice` accepts **only** the Tier 1 keys listed in §3. Nested blocks
such as `context`, `attention`, `performance`, `enable_interruptions`, and
`return_timestamps` are rejected with a close-match hint. Profile-derived
behaviour (diarization, endpointing, barge-in thresholds, participation gate)
is computed internally, not configured separately.

## 8. Goal check (GOAL.md mandatory review)

- **Identity impact.** Positive. `voice`, `speed` and `fallback_language` are the
  agent's audible self and stay visible and stable; latency tics
  (`ack_delay_ms`, filler phrasing) stop being per-install choices, so the agent
  sounds like one entity across devices.
- **Multi-user impact.** `enable_diarization` moves behind `profile` so speaker
  separation cannot be switched off by a user who does not know principle 2
  depends on it; `room` is the default.
- **1:1 and group coverage.** `profile: headset | room` is precisely this axis,
  asked as one answerable question rather than ten unanswerable ones.
- **Memory/journal perspective.** `return_timestamps` and `aggregate_utterances`
  leave the user surface specifically to protect what gets recorded as said: no
  unspoken text stored as spoken, no half-question stored as a whole turn.
- **Unified-memory impact.** None. No voice-local store is proposed.
- **Agent-governed sharing.** `quiet_hours` constrains when the agent speaks
  aloud in a shared physical space, never what it is willing to say.
- **Diary-anchored carrier.** Unchanged; no setting here adds a parallel store.
- **Attribution and continuity impact.** `names` improves name recognition, which
  is attribution quality at the source, and `profile: room` keeps diarization on
  by default so who-said-what survives into the diary. Fixing the `preserved_keys`
  erasure (§1) is itself a continuity fix: tuning should outlive a key rotation.

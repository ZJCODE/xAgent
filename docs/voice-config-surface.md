# Voice configuration and WebSocket sessions

Status: implemented. Voice uses browser or paired-device WebSocket sessions.
Host voice is no longer a managed channel. No replacement native audio client
is included in this phase.

## Shared settings

Configure voice in **Agent → Preferences → Voice** or `xagent voice setup`.
The existing YAML location stays compatible:

```yaml
channels:
  voice:
    api_key: your_soniox_api_key_here
    voice: Daniel
    interruptions: false
    # speed: 1.25
```

Soniox credentials, speaking voice, speed (0.7–1.3), and interruption preference
are shared across browser and device sessions. The default speed 1.0 is omitted
when saving. Recognition uses internal Chinese and English hints.
Keys are never returned by the settings API. PATCH submits only edited fields.
Settings changed on a running Agent require an Agent restart; offline changes
apply on its next start. Legacy `languages`, `enabled`, and `audio` values are
ignored when loading and removed the next time voice settings are saved. Even
an unavailable or malformed retired host audio selection cannot block WS voice.

There are no host microphone/speaker selectors or host device inventory endpoint.
Capture and playback hardware belong to the browser or paired device. The server
no longer requires sounddevice or PortAudio.

## Connections and lifecycle

```text
Browser microphone/speaker ── WS ──┐
                                  ├─ VoiceRuntime → one Agent
Paired voice device ───────── WS ──┘
```

Channels manages API and Weixin. Agent Runtime and its API must be running for
voice connections; starting Host voice is never required. The Voice page keeps
its existing Start/Restart Agent preparation flow. Voice navigation depends on
valid shared voice configuration, independently of host audio hardware.

Browser `/ws/voice/{session_id}` uses a short-lived, single-use management ticket.
Paired devices use `/ws/devices/{device_id}/voice` and their device credentials.
Each accepted connection creates a VoiceRuntime and cleans up its capture,
playback, and runtime resources when disconnected. API disconnect closes its
voice sessions. Protocol 2, Opus/PCM contracts, bounded audio queues, playback
progress/finished acknowledgements, and reconnect behavior are unchanged.
See [device execution](device-execution.md#device-protocol).

Browser Hold to talk and Continuous conversation share the same transport.
Continuous mode starts explicitly, supports mute, pauses on page hide, and
requires Continue conversation on return. Leaving the page ends the session.
A browser is not a background room microphone. Browser conversations bypass room
participation gating; paired room devices retain attention, diarization, and
idle recognition sleep. Devices supply session profile and echo handling;
interruption policy remains separate, with the existing echo safeguard.

Host capture factory, local hardware I/O, channel controls, restart restoration,
and CLI hardware enumeration are removed. Old `--channels voice`,
`channel voice connect|disconnect`, and native voice command attempts provide
migration instructions. Retired voice channel management/setup routes return
410; configure shared settings through `/api/agent/voice` instead.

## Delivery and compatibility

The `voice` conversation/delivery channel remains valid in historical messages,
contacts, tasks, and the unified diary. Only the managed host transport is retired.

Voice delivery selects a live WS session by explicit device ID, unique room, or
unique matching session user. A missing explicit device never falls through to
another room or browser. Ambiguous or absent destinations are unavailable;
there is no local speaker or sole-online-device fallback. Browser sessions stay
session-specific and room devices keep their persistent device identity.

Future unattended host audio can be an ordinary paired WS client running on the
Agent computer. It must implement the same audio and playback confirmation
contract; it does not require reinstating a managed Host voice channel.

## Goal check

- **Identity:** all endpoints use the same Agent and shared speaking voice.
- **Multi-user:** speaker attribution and identity boundaries remain in VoiceRuntime.
- **1:1 and groups:** browser direct conversation and paired room policies remain distinct.
- **Memory/journal perspective:** first-person records and confirmed played speech are preserved.
- **Unified memory:** endpoints share the existing Agent memory stream.
- **Agent-governed sharing:** existing sharing decisions remain; destination matching prevents accidental rerouting.
- **Diary carrier:** the diary remains authoritative; no new memory store is introduced.
- **Attribution and continuity:** existing voice records remain valid, devices retain identity, and browser sessions keep explicit lifetimes. Unattended host capture is intentionally removed in this phase.

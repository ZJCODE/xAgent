# Runtime validation — 2026-10-10

The real 24-hour soak was replaced, at the user's request, with a bounded accelerated scenario.

| Check | Result |
| --- | --- |
| Complete Python regression | 1,208 tests passed in 24.1 seconds |
| Frontend type check and production build | Passed |
| Source compilation and whitespace checks | Passed |
| Accelerated scheduler time | 24 virtual hours in 28.27 seconds |
| Four-channel formal Agent turns | 768; maximum concurrent turns: 1 |
| Input/reconnect cycles | 192 |
| Recurring task deliveries | 24 confirmed receipts |
| Core restarts | 2; no ordinary conversation replay |
| Persisted history | 1,752 messages; expected count matched |
| Diary input markers | 960 unique; no duplicated or skipped input |
| File descriptors | 9 at start and finish |
| Final diary backlog / pending commit | 0 / none |

Fault regressions include cross-process ownership, duplicate input, queue and execution timeouts, channel-scoped cancellation, voice supplements, failure after tool execution begins, cleanup exceptions, task execution/delivery/archive recovery, explicit task retry, diary generation and cursor failures, file conflicts and cancellation while waiting for a file lock. Shutdown with an unresponsive writer retains ownership until that writer ends. Offline Web reads and passive notifications do not bootstrap an Agent.

The Web UI was inspected in light and dark themes, at desktop and 390-pixel mobile width, including running/offline channel views, settings and task review. These previews used isolated synthetic data.

The suite exits successfully but still emits an unclosed-event-loop `ResourceWarning` in the SDK test process. The accelerated run uses deterministic model/channel doubles and virtual scheduler time; it does not establish real 24-hour uptime or exercise production devices and external bot services.

Reproduce with the commands in [runtime-core.md](runtime-core.md). The scenario is implemented in [runtime_soak.py](../scripts/runtime_soak.py).

## Background maintenance simplification — 2026-11-17

Full offline regression: `.venv/bin/python -m unittest discover -s tests -q` — 1350 tests passed, including local socket, device voice and stop/restart integration tests. New behavior coverage includes coalesced maintenance wakeups, age-based small diary batches after restart, draining committed backlog, periodic requests across midnight, independent reflection/observability, strict participation JSON, a single context snapshot interface, proactive playback preflight under the playback lock, and uncertain delivery without automatic retries. Real provider/model behavior was not evaluated in this run.

## One continuing individual across experiences — 2026-11-17

Full offline regression: 1356 tests passed. The final reflection wording adjustment (record intended sharing without claiming unconfirmed delivery) was followed by 129 targeted tests, all passing. Attribution tests exercise stored direct/group replies through prompt history, diary input and working-summary input; preserve unknown recipients; distinguish a human whose id is `assistant` from the Agent; and keep recent-person context independent of the bounded delivery address list.

Real model review used the configured `gpt-6-luna` with synthetic experiences and temporary local delivery only. Seven new cases covered one-person language scope, several people's distinct requests, identical names across channels, an unreachable person, group followed by private disclosure, inward reflection without a recipient, and an ambiguous older note. Sampled notes kept identities, private/group settings and uncertainty distinct. The diary/projection correction case updated Alice's changed preference while preserving Bob's separate card and diary sources. Two additional sharing cases produced messages to the intended Alice: a new finding during her explicitly welcomed night shift, and a new deployment blocker despite an unanswered earlier update. In the final sharing samples, the note was either absent or expressed an intention rather than claiming prior delivery.

These are reviewed model samples, not guarantees about every generation. The automatic checks establish valid results, routing and required silence; semantic attribution and disclosure were reviewed from the generated notes, messages, diary and cards. No new runtime state, model pass, identity registry or diary schema is introduced.

Follow-up for repeated inward notes: the reflection contract now starts from null choices and writes a note only for a new understanding, observation or change of intention relative to the recent diary. Rephrasing a pending task, waiting state, reflection-only capability limit or reason for silence does not create another diary entry. Reflection tool restrictions are explicitly scoped to the current turn. Final targeted regression: 155 tests passed. Real `gpt-6-luna` review returned null for both fields in three consecutive unchanged LAN-device scenarios, recorded a new conclusion from newly supplied idempotency test results, and still sent new findings in the two local sharing scenarios. The semantic novelty judgment remains in the same model call; no similarity score, cooldown state or second model pass was added.

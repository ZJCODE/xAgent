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

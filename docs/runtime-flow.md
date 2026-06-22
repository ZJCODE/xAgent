# xAgent Runtime Flow

1. A user-facing channel receives input.
2. The channel normalizes the input into domain messages and attachment metadata.
3. `bootstrap.container` provides the configured agent runtime and storage services.
4. `application.AgentService` builds instructions and turn context.
5. The LLM infrastructure streams model events back to the application layer.
6. Tool calls are dispatched through `tools.executor` and `tools.registry`.
7. Message and memory updates are persisted through storage ports.
8. Delivery output is adapted back to the active channel.
9. Scheduled work uses `application.task_service` and the same delivery context.

The current implementation keeps the single-turn model/tool loop in `AgentService`. Split it only when the code has a concrete second caller or a real testing boundary that justifies the extra module.

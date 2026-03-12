# Best Practices

This guide summarizes practical recommendations for building stable, maintainable xAgent applications.

## 1) Start Simple, Then Scale

- Begin with one `Agent` and one interface (CLI or HTTP).
- Add tools only when you have clear use cases.
- Introduce memory and workflows after your baseline flow is stable.

## 2) Keep Sessions Explicit

- Always pass stable `user_id` and `session_id`.
- Use one session per conversation context (e.g., `work`, `personal`, `debug`).
- Avoid reusing one session for unrelated topics.

## 3) Prefer Configuration for Deployments

- Use `xagent-cli --init` to scaffold config and toolkit.
- Keep runtime settings in `config/agent.yaml`.
- Avoid hardcoding model and server settings in application code for production.

See: [Configuration Reference](configuration_reference.md)

## 4) Tool Design Guidelines

- Keep tools focused: one tool, one responsibility.
- Write clear docstrings (purpose + parameter meaning).
- Use async tools for I/O calls (HTTP, DB, network).
- Add timeout/retry logic inside tools for external dependencies.

## 5) Memory Usage

- Enable memory only for scenarios that need continuity.
- Define memory thresholds to control storage frequency.
- Separate memory collections by product or tenant boundaries.

See: [Memory System](memory.md)

## 6) Workflow Pattern Selection

- Default to `run_auto` for complex tasks or unknown complexity.
- Use `run_sequential` for strict pipelines.
- Use `run_parallel` for independent perspectives.
- Use `run_graph` when dependencies are explicit and stable.

See: [Multi-Agent Workflows](workflows.md)

## 7) Production Readiness Checklist

- Set required env vars (`OPENAI_API_KEY`, optional `REDIS_URL`).
- Add health checks and basic monitoring.
- Use bounded retries and request timeouts.
- Validate model output format when integrating with downstream systems.
- Keep examples and config in version control.

## 8) Documentation Hygiene

- Keep `README` short and task-oriented.
- Put parameter-level details in reference docs.
- Prefer one canonical place for each technical topic.
- Update docs with every behavior or config change.

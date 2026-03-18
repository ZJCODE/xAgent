# Best Practices

This guide summarizes practical recommendations for building stable, maintainable xAgent applications.

## 1) Start Simple, Then Scale

- Begin with one `Agent` and one interface (CLI or HTTP).
- Add tools only when you have clear use cases.
- Introduce memory only after your baseline flow is stable.

## 2) Keep Speaker Identity Stable

- Keep `user_id` stable and human-readable, because it is part of the model-visible stream.
- Remember the agent uses one continuous message stream, not separate conversation buckets.

## 3) Prefer Configuration for Deployments

- Use `xagent-cli --init` to scaffold config and toolkit.
- Keep runtime settings in `config/agent.yaml`.
- Avoid hardcoding model and server settings in application code for production.

See: [Configuration Reference](configuration_reference.md)

## 4) Tool Design Guidelines

- Keep tools focused: one tool, one responsibility.
- Write clear docstrings (purpose + parameter meaning).
- Use async tools for I/O calls (HTTP, DB, network).
- Add timeout and retry logic inside tools for external dependencies.

## 5) Memory Usage

- Enable memory only for scenarios that need continuity.
- Define memory thresholds and batch intervals to control storage frequency.
- Separate memory collections by product or tenant boundaries when needed.

See: [Memory System](memory.md)

## 6) Production Readiness Checklist

- Set required env vars (`OPENAI_API_KEY`, optional `REDIS_URL`).
- Add health checks and basic monitoring.
- Use bounded retries and request timeouts.
- Validate model output format when integrating with downstream systems.
- Keep examples and config in version control.

## 7) Documentation Hygiene

- Keep `README` short and task-oriented.
- Put parameter-level details in reference docs.
- Prefer one canonical place for each technical topic.
- Update docs with every behavior or config change.

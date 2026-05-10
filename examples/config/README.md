# Config Examples

These examples are grouped by scenario instead of by implementation detail.

- [`config.yaml`](./config.yaml): smallest local-first server config
- [`structured_output/`](./structured_output): directory-based YAML output schema examples

Run examples from the repository root:

```bash
# Minimal local server
xagent-server --dir examples/config

# Structured output config example
xagent-server --dir examples/config/structured_output/weather
```

Guidelines for adding new config examples:

- Add a new config only when it demonstrates a distinct runtime capability
- Prefer the built-in local workspace layout in examples
- Keep each runnable config in a directory named by scenario with a `config.yaml` file

# Config Examples

These examples are grouped by scenario instead of by implementation detail.

- [`agent.yaml`](./agent.yaml): smallest local-first server config
- [`toolkit_agent.yaml`](./toolkit_agent.yaml): config that uses custom tools from [`../toolkit`](../toolkit/README.md)
- [`structured_output/`](./structured_output): YAML-defined output schemas

Run examples from the repository root:

```bash
# Minimal local server
xagent-server --config examples/config/agent.yaml

# Server with custom toolkit tools
xagent-server --config examples/config/toolkit_agent.yaml --toolkit_path examples/toolkit
```

Guidelines for adding new config examples:

- Add a new config only when it demonstrates a distinct runtime capability
- Prefer the built-in local workspace layout in examples
- Avoid hidden dependencies: if a config needs `--toolkit_path` or an MCP server, say so in a nearby README

# Examples

This folder is organized by how people usually learn the project:

- [`demo/`](./demo/README.md): curated Python examples for the main runtime APIs
- [`config/`](./config/README.md): YAML-first examples for `xagent-server` and `xagent-cli`
- [`toolkit/`](./toolkit/README.md): local custom tools and standalone MCP examples

Best-practice rules used in this directory:

- Keep examples local-first unless cloud behavior is the thing being demonstrated
- Prefer one high-signal example per capability over many overlapping variants
- Make starter examples runnable from the repository root
- Keep generated files such as `__pycache__` and logs out of versioned example content

Typical starting points:

```bash
# Python API
python examples/demo/basic_chat.py

# Config-driven server
xagent-server --config examples/config/agent.yaml

# Config-driven server with custom toolkit tools
xagent-server --config examples/config/toolkit_agent.yaml --toolkit_path examples/toolkit
```

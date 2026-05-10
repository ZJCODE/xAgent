# Examples

This folder is organized by how people usually learn the project:

- [`demo/`](./demo/README.md): curated Python examples for the main runtime APIs
- [`config/`](./config/README.md): YAML-first examples for `xagent-server` and `xagent`

Best-practice rules used in this directory:

- Keep examples local-first and focused on built-in behavior unless an extension point is the thing being demonstrated
- Prefer one high-signal example per capability over many overlapping variants
- Make starter examples runnable from the repository root
- Keep generated files such as `__pycache__` and logs out of versioned example content

Typical starting points:

```bash
# Python API
python examples/demo/basic_chat.py

# Config-driven server
xagent-server --dir examples/config

# Config-driven CLI
xagent --dir examples/config --ask "Hello"
```

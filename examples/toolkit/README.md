# Toolkit Examples

This directory shows the local toolkit extension mechanism:

- `tools.py` + `__init__.py`: local Python tools loaded with `--toolkit_path`

## Local Toolkit

Load these tools with:

```bash
xagent-server --config examples/config/toolkit_agent.yaml --toolkit_path examples/toolkit
```

The toolkit loader reads `TOOLKIT_REGISTRY` from [`__init__.py`](./__init__.py), so keep exported tools explicit and easy to scan.

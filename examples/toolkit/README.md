# Toolkit Examples

This directory shows two different extension mechanisms:

- `tools.py` + `__init__.py`: local Python tools loaded with `--toolkit_path`
- [`mcp_demo/`](./mcp_demo/README.md): a standalone MCP server and client example

## Local Toolkit

Load these tools with:

```bash
xagent-server --config examples/config/toolkit_agent.yaml --toolkit_path examples/toolkit
```

The toolkit loader reads `TOOLKIT_REGISTRY` from [`__init__.py`](./__init__.py), so keep exported tools explicit and easy to scan.

## MCP Demo

The MCP demo is separate from `--toolkit_path`.

It starts a standalone MCP server that can be used by:

- [`examples/demo/mcp_integration.py`](../demo/mcp_integration.py)
- any other xAgent config or code path that points at `http://localhost:8001/mcp/`

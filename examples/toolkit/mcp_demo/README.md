# MCP Demo

This folder contains a minimal standalone MCP setup:

- `server.py`: starts an MCP server on `http://localhost:8001/mcp/`
- `client.py`: connects to that server and exercises tools, resources, and prompts

Run from the repository root:

```bash
python examples/toolkit/mcp_demo/server.py --port 8001
python examples/toolkit/mcp_demo/client.py
```

The Python demo [`examples/demo/mcp_integration.py`](../demo/mcp_integration.py) expects the same default server URL.

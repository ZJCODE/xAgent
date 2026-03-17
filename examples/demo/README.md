# Demo Examples

This directory is intentionally curated.

The examples here are the smallest set that still covers xAgent's core capabilities:

- `basic_chat.py`: local-first Python API usage with multi-turn conversations
- `stream_chat.py`: streaming responses from the Python API
- `custom_tools.py`: registering sync and async local tools
- `structured_output.py`: typed outputs with Pydantic models
- `memory.py`: enabling long-term memory in normal agent chats
- `http_server_with_custom_agent.py`: running `AgentHTTPServer` with a pre-configured local agent
- `mcp_integration.py`: combining local tools with MCP tools when an MCP server is available

What is intentionally not in this folder anymore:

- Cloud-only persistence demos that duplicated local behavior
- Test-like or outdated scripts with broken imports or `sys.path` hacks

Run these examples after installing the package or from an editable checkout:

```bash
pip install -e .
```

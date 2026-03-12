# Demo Examples

This directory is intentionally curated.

The examples here are the smallest set that still covers xAgent's core capabilities:

- `basic_chat.py`: local-first Python API usage with multi-turn sessions
- `stream_chat.py`: streaming responses from the Python API
- `custom_tools.py`: registering sync and async local tools
- `structured_output.py`: typed outputs with Pydantic models
- `sub_agents.py`: composing specialist agents under one coordinator
- `memory.py`: enabling long-term memory in normal agent chats
- `meta_memory_demo.py`: extracting higher-level meta memories from local memory
- `http_server_with_custom_agent.py`: running `AgentHTTPServer` with a pre-configured local agent
- `hybrid_quick_start.py`: one high-signal multi-agent workflow example
- `mcp_integration.py`: combining local tools with MCP tools when an MCP server is available

What is intentionally not in this folder anymore:

- Cloud-only persistence demos that duplicated local behavior
- Multiple workflow files that repeated the same orchestration ideas
- Test-like or outdated scripts with broken imports or `sys.path` hacks

Run these examples after installing the package or from an editable checkout:

```bash
pip install -e .
```

For cloud deployment, Redis-backed persistence, and advanced workflow DSL details, use the docs in `docs/` instead of adding more demo variants here.

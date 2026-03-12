# xAgent Documentation

This directory contains the complete technical documentation for xAgent.

## Read by Goal

### I want to run xAgent quickly
1. [Getting Started](getting_started.md)
2. [Best Practices](best_practices.md)

### I want to build with Python API
1. [Getting Started](getting_started.md)
2. [API Reference](api_reference.md)

### I want to deploy HTTP service
1. [Getting Started](getting_started.md)
2. [Configuration Reference](configuration_reference.md)
3. [Docker Deployment](../deploy/docker/README.md)

### I want memory and long-term context
1. [Memory System](memory.md)
2. [Message Storage Inheritance](message_storage_inheritance.md)
3. [Redis Cluster Support](redis_cluster_support.md)

### I want multi-agent orchestration
1. [Multi-Agent Workflows](workflows.md)
2. [Workflow DSL](workflow_dsl.md)

## Document Map

- [Getting Started](getting_started.md) — Fastest path from install to usable app
- [Best Practices](best_practices.md) — Recommended patterns for production and maintainability
- [Configuration Reference](configuration_reference.md) — Full YAML config fields and examples
- [API Reference](api_reference.md) — Full Python API details
- [Memory System](memory.md) — Long-term memory architecture and usage
- [Multi-Agent Workflows](workflows.md) — Workflow patterns and orchestration
- [Workflow DSL](workflow_dsl.md) — DSL syntax for agent dependencies
- [HTTP Server Agent Passing](http_server_agent_passing.md) — Pass a prebuilt `Agent` into HTTP server
- [Message Storage Inheritance](message_storage_inheritance.md) — Build custom storage backends
- [Redis Cluster Support](redis_cluster_support.md) — Cluster mode setup and guidance

## Suggested Reading Order

For most users:

1. [Getting Started](getting_started.md)
2. [Best Practices](best_practices.md)
3. One deep-dive reference based on your use case

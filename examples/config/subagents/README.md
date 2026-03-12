## Sub-Agent Config Example

Start the specialist agents first, then the coordinator.

Run from the repository root:

```bash
mkdir -p logs

xagent-server --config examples/config/subagents/agents/research_agent.yaml > logs/research_agent.log 2>&1 &
xagent-server --config examples/config/subagents/agents/writer_agent.yaml > logs/writer_agent.log 2>&1 &
xagent-server --config examples/config/subagents/agents/image_analysis_agent.yaml > logs/image_analysis_agent.log 2>&1 &
xagent-server --config examples/config/subagents/agents/planner_agent.yaml > logs/planner_agent.log 2>&1 &
xagent-server --config examples/config/subagents/coordinator.yaml > logs/coordinator_agent.log 2>&1 &
```

Quick health checks:

```bash
curl http://localhost:8010/health
curl http://localhost:8011/health
curl http://localhost:8012/health
curl http://localhost:8013/health
curl http://localhost:8014/health
```

Cleanup:

```bash
pkill -f "examples/config/subagents/agents/research_agent.yaml"
pkill -f "examples/config/subagents/agents/writer_agent.yaml"
pkill -f "examples/config/subagents/agents/image_analysis_agent.yaml"
pkill -f "examples/config/subagents/agents/planner_agent.yaml"
pkill -f "examples/config/subagents/coordinator.yaml"
```

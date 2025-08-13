### Start Sub-Agents Example

run the following commands to start the sub-agents example:

```bash
xagent-server --config config/sub_agents_example/subs/research_agent.yaml > logs/research_agent.log 2>&1 &
xagent-server --config config/sub_agents_example/subs/write_agent.yaml > logs/write_agent.log 2>&1 &
xagent-server --config config/sub_agents_example/agent.yaml --toolkit_path toolkit > logs/agent.log 2>&1 &
```

kill the sub-agents example:

```bash
pkill -f "xagent-server --config config/sub_agents_example/subs/research_agent.yaml"
pkill -f "xagent-server --config config/sub_agents_example/subs/write_agent.yaml"
pkill -f "xagent-server --config config/sub_agents_example/agent.yaml"
``` 

auto delete the logs:

```bash
rm -rf logs/research_agent.log logs/write_agent.log logs/agent.log
```

you can interact with the agents using the xagent-web interface:

```bash
xagent-web --agent-server http://localhost:8010
```
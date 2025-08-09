from xagent.core.server import HTTPAgentServer

# Create and run the HTTP Agent Server
server = HTTPAgentServer("config/agent.yaml")

# Run the server
server.run(host="0.0.0.0", port=8010)
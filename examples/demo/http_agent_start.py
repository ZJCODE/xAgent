from xagent.core.server import AgentHTTPServer

# Create and run the HTTP Agent Server
server = AgentHTTPServer("config/agent.yaml")

# Run the server
server.run(host="0.0.0.0", port=8010)
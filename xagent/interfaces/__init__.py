__all__ = ["AgentHTTPServer", "AgentCLI"]


def __getattr__(name: str):
	if name == "AgentHTTPServer":
		from .server import AgentHTTPServer
		return AgentHTTPServer
	if name == "AgentCLI":
		from .cli import AgentCLI

		return AgentCLI
	raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

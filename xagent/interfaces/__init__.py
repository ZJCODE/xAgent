from .server import AgentHTTPServer

__all__ = ["AgentHTTPServer", "AgentCLI"]


def __getattr__(name: str):
	if name == "AgentCLI":
		from .cli import AgentCLI

		return AgentCLI
	raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

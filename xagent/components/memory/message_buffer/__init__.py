"""Message buffer implementations for xAgent memory system."""

import importlib

__all__ = [
    "MessageBufferBase",
    "MessageBufferLocal",
    "MessageBufferRedis",
]

_EXPORTS = {
    "MessageBufferBase": (".base_message_buffer", "MessageBufferBase"),
    "MessageBufferLocal": (".local_message_buffer", "MessageBufferLocal"),
    "MessageBufferRedis": (".redis_message_buffer", "MessageBufferRedis"),
}


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_name, attr_name = _EXPORTS[name]
    try:
        module = importlib.import_module(module_name, __name__)
    except ModuleNotFoundError as exc:
        if (exc.name or "").split(".")[0] == "redis":
            raise ImportError(
                "MessageBufferRedis requires the optional cloud dependencies. "
                "Install them with `pip install myxagent[cloud]`."
            ) from exc
        raise

    value = getattr(module, attr_name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(__all__)

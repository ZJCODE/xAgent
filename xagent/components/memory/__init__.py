import importlib

__all__ = ["MemoryStorageBase", "MemoryStorageLocal", "MemoryStorageCloud"]

_EXPORTS = {
    "MemoryStorageBase": (".base_memory", "MemoryStorageBase"),
    "MemoryStorageLocal": (".local_memory", "MemoryStorageLocal"),
    "MemoryStorageCloud": (".cloud_memory", "MemoryStorageCloud"),
}


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_name, attr_name = _EXPORTS[name]
    try:
        module = importlib.import_module(module_name, __name__)
    except ModuleNotFoundError as exc:
        if (exc.name or "").split(".")[0] == "upstash_vector":
            raise ImportError(
                "MemoryStorageCloud requires the optional cloud dependencies. "
                "Install them with `pip install myxagent[cloud]`."
            ) from exc
        if (exc.name or "").split(".")[0] == "redis":
            raise ImportError(
                "Redis-backed memory buffers require the optional cloud dependencies. "
                "Install them with `pip install myxagent[cloud]`."
            ) from exc
        raise

    value = getattr(module, attr_name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(__all__)

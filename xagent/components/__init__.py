import importlib

__all__ = [
    "MessageStorageBase",
    "MessageStorageLocal",
    "MessageStorageCloud",
    "MemoryStorageBase",
    "MemoryStorageLocal",
    "MemoryStorageCloud",
]

_EXPORTS = {
    "MessageStorageBase": (".message", "MessageStorageBase"),
    "MessageStorageLocal": (".message", "MessageStorageLocal"),
    "MessageStorageCloud": (".message", "MessageStorageCloud"),
    "MemoryStorageBase": (".memory", "MemoryStorageBase"),
    "MemoryStorageLocal": (".memory", "MemoryStorageLocal"),
    "MemoryStorageCloud": (".memory", "MemoryStorageCloud"),
}


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_name, attr_name = _EXPORTS[name]
    try:
        module = importlib.import_module(module_name, __name__)
    except ModuleNotFoundError as exc:
        package = (exc.name or "").split(".")[0]
        if package == "redis":
            raise ImportError(
                "Cloud components require the optional cloud dependencies. "
                "Install them with `pip install myxagent[cloud]`."
            ) from exc
        if package == "upstash_vector":
            raise ImportError(
                "Cloud components require the optional cloud dependencies. "
                "Install them with `pip install myxagent[cloud]`."
            ) from exc
        raise

    value = getattr(module, attr_name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(__all__)

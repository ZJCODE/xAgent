import importlib

__all__ = [
    "MessageStorageBase",
    "MessageStorageLocal",
    "MessageStorageCloud",
]

_EXPORTS = {
    "MessageStorageBase": (".base_messages", "MessageStorageBase"),
    "MessageStorageLocal": (".local_messages", "MessageStorageLocal"),
    "MessageStorageCloud": (".cloud_messages", "MessageStorageCloud"),
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
                "MessageStorageCloud requires the optional cloud dependencies. "
                "Install them with `pip install myxagent[cloud]`."
            ) from exc
        raise

    value = getattr(module, attr_name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(__all__)

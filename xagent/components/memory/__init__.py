import importlib

__all__ = [
    "MemoryStorageBase",
    "MemoryStorageLocal",
    "MemoryStorageCloud",
    "VectorStoreBase",
    "VectorDoc",
    "VectorStoreLocal",
    "VectorStoreUpstash",
]

_EXPORTS = {
    "MemoryStorageBase": (".base_memory", "MemoryStorageBase"),
    "MemoryStorageLocal": (".local_memory", "MemoryStorageLocal"),
    "MemoryStorageCloud": (".cloud_memory", "MemoryStorageCloud"),
    "VectorStoreBase": (".vector.base_vector_store", "VectorStoreBase"),
    "VectorDoc": (".vector.base_vector_store", "VectorDoc"),
    "VectorStoreLocal": (".vector.local_vector_store", "VectorStoreLocal"),
    "VectorStoreUpstash": (".vector.cloud_vector_store", "VectorStoreUpstash"),
}


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_name, attr_name = _EXPORTS[name]
    try:
        module = importlib.import_module(module_name, __name__)
    except ModuleNotFoundError as exc:
        package = (exc.name or "").split(".")[0]
        if package == "upstash_vector":
            raise ImportError(
                f"{attr_name} requires the optional cloud dependencies. "
                "Install them with `pip install myxagent[cloud]`."
            ) from exc
        if package == "chromadb":
            raise ImportError(
                f"{attr_name} requires ChromaDB. "
                "Install it with `pip install myxagent` (included by default)."
            ) from exc
        raise

    value = getattr(module, attr_name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(__all__)

"""Vector store implementations for xAgent memory system."""

import importlib

__all__ = [
    "VectorStoreBase",
    "VectorDoc",
    "VectorStoreLocal",
    "VectorStoreUpstash",
]

_EXPORTS = {
    "VectorStoreBase": (".base_vector_store", "VectorStoreBase"),
    "VectorDoc": (".base_vector_store", "VectorDoc"),
    "VectorStoreLocal": (".local_vector_store", "VectorStoreLocal"),
    "VectorStoreUpstash": (".upstach_vector_store", "VectorStoreUpstash"),
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
                "VectorStoreUpstash requires the optional cloud dependencies. "
                "Install them with `pip install myxagent[cloud]`."
            ) from exc
        raise

    value = getattr(module, attr_name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(__all__)

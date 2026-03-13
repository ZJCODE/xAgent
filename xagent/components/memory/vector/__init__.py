import importlib

__all__ = [
    "VectorDoc",
    "VectorStoreBase",
    "VectorStoreLocal",
    "VectorStoreUpstash",
]

_EXPORTS = {
    "VectorDoc": (".base_vector_store", "VectorDoc"),
    "VectorStoreBase": (".base_vector_store", "VectorStoreBase"),
    "VectorStoreLocal": (".local_vector_store", "VectorStoreLocal"),
    "VectorStoreUpstash": (".cloud_vector_store", "VectorStoreUpstash"),
}


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_name, attr_name = _EXPORTS[name]
    module = importlib.import_module(module_name, __name__)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(__all__)
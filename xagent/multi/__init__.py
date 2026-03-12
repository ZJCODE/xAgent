"""Multi-agent capabilities for xAgent."""

import importlib

__all__ = [
    "Workflow",
]

_EXPORTS = {
    "Workflow": (".workflow", "Workflow"),
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

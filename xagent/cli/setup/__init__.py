"""CLI setup wizard."""

from . import wizard as _wizard

globals().update({
    name: value
    for name, value in vars(_wizard).items()
    if not name.startswith("__")
})

__all__ = [
    name
    for name in globals()
    if not name.startswith("__") and name != "_wizard"
]

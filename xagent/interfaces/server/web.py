"""Legacy import path for SPA route registration (moved to clients.web)."""

from ..clients.web.spa import register_spa_routes

__all__ = ["register_spa_routes"]

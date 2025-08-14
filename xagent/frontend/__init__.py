"""
Frontend components for xAgent.

This module provides web interfaces for interacting with xAgent:
- Streamlit chat application for conversational interface
- Configuration UI for visual agent setup and management
"""

from .app import main as chat_app_main
from .launcher import main as web_launcher_main
from .config_ui import main as config_ui_main
from .config_launcher import main as config_launcher_main

__all__ = [
    "chat_app_main",
    "web_launcher_main", 
    "config_ui_main",
    "config_launcher_main"
]

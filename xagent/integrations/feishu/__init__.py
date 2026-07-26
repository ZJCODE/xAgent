"""Feishu (Lark) bot integration for xAgent.

Bridges Feishu inbound messages to the single runtime using the
official ``lark_oapi.channel.FeishuChannel`` WebSocket long-connection
layer. No public webhook, no reverse proxy, no extra HTTP hop required.

Adapters are constructed only by the runtime composition root.
"""

from .config import FeishuAdapterConfig
from .adapter import FeishuAdapter

__all__ = ["FeishuAdapter", "FeishuAdapterConfig"]

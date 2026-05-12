"""Feishu (Lark) bot integration for xAgent.

Bridges Feishu inbound messages to an in-process ``Agent`` using the
official ``lark_oapi.channel.FeishuChannel`` WebSocket long-connection
layer. No public webhook, no reverse proxy, no extra HTTP hop required.

Quick start:

    from xagent.integrations.feishu import FeishuAdapter, FeishuAdapterConfig
    from xagent.interfaces.base import BaseAgentRunner

    runner = BaseAgentRunner(config_dir="~/.xagent")
    cfg = FeishuAdapterConfig.from_file("~/.xagent/feishu/feishu.yaml")
    adapter = FeishuAdapter(agent=runner.agent, config=cfg)
    asyncio.run(adapter.run())
"""

from .config import FeishuAdapterConfig
from .adapter import FeishuAdapter

__all__ = ["FeishuAdapter", "FeishuAdapterConfig"]

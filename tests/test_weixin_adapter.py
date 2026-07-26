import asyncio
import tempfile
import unittest
from pathlib import Path

from xagent.core.runtime import Delivery
from xagent.integrations.weixin.adapter import WeixinAdapter
from xagent.integrations.weixin.config import WeixinAdapterConfig
from xagent.integrations.weixin.state import WeixinCredentials, WeixinStateStore


class _FakeAgent:
    supports_vision = True

    def __init__(self, reply="agent reply"):
        self.reply = reply
        self.chat_calls = []
        self.maintenance_count = 0
        self.workspace_dir = None

    async def chat_events(self, **kwargs):
        self.chat_calls.append(kwargs)
        yield {"type": "message_done", "message_id": "m1", "phase": "final", "content": self.reply}
        yield {"type": "done"}

    async def run_memory_maintenance(self, trigger: str = "unknown"):
        self.maintenance_count += 1


class _FakeWeixinClient:
    def __init__(self):
        self.sent_text = []
        self.sent_items = []
        self.typing = []

    def with_credentials(self, credentials):
        self.credentials = credentials
        return self

    async def aclose(self):
        pass

    async def send_text_message(self, **kwargs):
        self.sent_text.append(kwargs)
        return {}

    async def send_message_item(self, **kwargs):
        self.sent_items.append(kwargs)
        return {}

    async def get_config(self, **kwargs):
        return {"typing_ticket": "ticket"}

    async def send_typing(self, **kwargs):
        self.typing.append(kwargs)
        return {}


class WeixinAdapterTests(unittest.TestCase):
    def _adapter(self, tmpdir, *, owner_only=True, allow_users=(), reply="agent reply", text_max_chars=2000):
        state = WeixinStateStore(tmpdir)
        credentials = WeixinCredentials(
            token="token",
            base_url="https://example.test",
            account_id="bot@im.bot",
            user_id="owner@im.wechat",
        )
        state.save_credentials(credentials)
        agent = _FakeAgent(reply=reply)
        agent.workspace_dir = Path(tmpdir) / "workspace"
        agent.workspace_dir.mkdir(parents=True, exist_ok=True)
        client = _FakeWeixinClient()
        config = WeixinAdapterConfig(
            account_id="bot@im.bot",
            owner_user_id="owner@im.wechat",
            owner_only=owner_only,
            allow_users=list(allow_users),
            send_typing=False,
            media_enabled=False,
            text_max_chars=text_max_chars,
            send_chunk_delay_seconds=0,
        )
        adapter = WeixinAdapter(agent=agent, config=config, runtime_dir=tmpdir, state_store=state, client=client)
        adapter._credentials = credentials
        adapter._context_tokens = state.load_context_tokens(credentials.account_id)
        return adapter, agent, client, state

    def test_owner_direct_message_routes_to_agent_and_replies(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter, agent, client, state = self._adapter(tmpdir)
            message = {
                "message_id": 101,
                "from_user_id": "owner@im.wechat",
                "to_user_id": "bot@im.bot",
                "message_type": 1,
                "context_token": "ctx-owner",
                "item_list": [{"type": 1, "text_item": {"text": "hello"}}],
            }

            asyncio.run(adapter._process_message(message))

            self.assertEqual(agent.chat_calls[0]["user_message"], "hello")
            self.assertEqual(agent.chat_calls[0]["user_id"], "owner@im.wechat")
            self.assertEqual(client.sent_text[0]["to_user_id"], "owner@im.wechat")
            self.assertEqual(client.sent_text[0]["context_token"], "ctx-owner")
            self.assertEqual(state.load_context_tokens("bot@im.bot"), {"owner@im.wechat": "ctx-owner"})

    def test_non_owner_direct_message_is_ignored_by_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter, agent, client, _state = self._adapter(tmpdir)
            message = {
                "message_id": 102,
                "from_user_id": "stranger@im.wechat",
                "to_user_id": "bot@im.bot",
                "message_type": 1,
                "context_token": "ctx-stranger",
                "item_list": [{"type": 1, "text_item": {"text": "hello"}}],
            }

            asyncio.run(adapter._process_message(message))

            self.assertEqual(agent.chat_calls, [])
            self.assertEqual(client.sent_text, [])

    def test_group_message_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter, agent, client, _state = self._adapter(tmpdir)
            message = {
                "message_id": 103,
                "from_user_id": "owner@im.wechat",
                "to_user_id": "bot@im.bot",
                "group_id": "room@chatroom",
                "message_type": 1,
                "context_token": "ctx-owner",
                "item_list": [{"type": 1, "text_item": {"text": "hello"}}],
            }

            asyncio.run(adapter._process_message(message))

            self.assertEqual(agent.chat_calls, [])
            self.assertEqual(client.sent_text, [])

    def test_reply_text_is_split_in_order(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter, _agent, client, _state = self._adapter(
                tmpdir,
                reply="first line\nsecond line\nthird line",
                text_max_chars=12,
            )
            message = {
                "message_id": "m-split",
                "from_user_id": "owner@im.wechat",
                "to_user_id": "bot@im.bot",
                "message_type": 1,
                "context_token": "ctx-owner",
                "item_list": [{"type": 1, "text_item": {"text": "go"}}],
            }

            asyncio.run(adapter._process_message(message))

            self.assertGreater(len(client.sent_text), 1)
            self.assertEqual([item["text"] for item in client.sent_text], ["first line", "second line", "third line"])

    def test_runtime_delivery_requires_cached_context_and_sends(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                adapter, _agent, client, _state = self._adapter(tmpdir)
                adapter._context_tokens["owner@im.wechat"] = "ctx-owner"
                delivery = Delivery.create(
                    event_id="event-1",
                    channel="weixin",
                    target={"user_id": "owner@im.wechat"},
                    payload={"content": "scheduled hello"},
                )
                await adapter.send(delivery)
                return client.sent_text

        sent_text = asyncio.run(run_test())

        self.assertEqual(sent_text[0]["to_user_id"], "owner@im.wechat")
        self.assertEqual(sent_text[0]["context_token"], "ctx-owner")
        self.assertEqual(sent_text[0]["text"], "scheduled hello")

    def test_runtime_delivery_uses_stable_delivery_id(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                adapter, _agent, client, _state = self._adapter(tmpdir)
                adapter._context_tokens["owner@im.wechat"] = "ctx-owner"
                delivery = Delivery.create(
                    event_id="event-2",
                    channel="weixin",
                    target={"user_id": "owner@im.wechat"},
                    payload={"content": "subconscious hello"},
                )
                await adapter.send(delivery)
                return delivery, client.sent_text

        delivery, sent_text = asyncio.run(run_test())

        self.assertEqual(sent_text[0]["to_user_id"], "owner@im.wechat")
        self.assertEqual(sent_text[0]["context_token"], "ctx-owner")
        self.assertEqual(sent_text[0]["text"], "subconscious hello")
        self.assertIn(delivery.delivery_id, sent_text[0]["client_id"])

    def test_runtime_mode_persists_reply_instead_of_sending_directly(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                adapter, _agent, client, _state = self._adapter(tmpdir)
                queued = []

                async def sink(delivery):
                    queued.append(delivery)

                adapter.delivery_sink = sink
                message = {
                    "message_id": 101,
                    "from_user_id": "owner@im.wechat",
                    "to_user_id": "bot@im.bot",
                    "message_type": 1,
                    "context_token": "ctx-owner",
                    "item_list": [{"type": 1, "text_item": {"text": "hello"}}],
                }
                await adapter._process_message(message)
                return queued, client.sent_text

        queued, sent_text = asyncio.run(run_test())

        self.assertEqual(sent_text, [])
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0].event_id, "weixin:101")
        self.assertEqual(queued[0].payload["content"], "agent reply")


if __name__ == "__main__":
    unittest.main()

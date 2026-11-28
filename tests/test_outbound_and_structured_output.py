"""Tests for core outbound codec and native structured response_format plumbing."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from xagent.core.agent import Agent
from xagent.core.config import ReplyType
from xagent.core.handlers.model import ModelClient
from xagent.core.journal import JournalLLMService
from xagent.core.outbound import (
    normalize_outbound_reply,
    strip_diary_headings,
    strip_leaked_transcript_markers,
)
from xagent.core.providers import (
    MODEL_API_ANTHROPIC_MESSAGES,
    MODEL_API_OPENAI_CHAT_COMPLETIONS,
    MODEL_API_OPENAI_RESPONSES,
    PROVIDER_DEEPSEEK,
    PROVIDER_OPENAI,
)
from xagent.core.runtime.subconscious import ContactEntry, SubconsciousLoop
import tests.test_agent_chat_flow as chat_test


class OutboundCodecTests(unittest.IsolatedAsyncioTestCase):
    def test_strip_leaked_transcript_markers_removes_leading_headers_only(self):
        raw = "[speaker=ME][timestamp=2026-06-08 10:00][channel=weixin]\n好的，我记下了 [1]。"
        self.assertEqual(strip_leaked_transcript_markers(raw), "好的，我记下了 [1]。")

    def test_strip_diary_headings_removes_accidental_date_headers(self):
        raw = "# 2026-06-09\n## 2026-06-09 14:30\n\n今天和 Alice 聊了数据库备份。"
        self.assertEqual(strip_diary_headings(raw), "今天和 Alice 聊了数据库备份。")

    def test_normalize_outbound_reply_lifts_workspace_files_and_keeps_external_links(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            (root / "charts").mkdir()
            (root / "charts" / "plot.png").write_bytes(b"\x89PNG\r\n\x1a\npng")
            (root / "report.pdf").write_bytes(b"%PDF-1.4")

            delivered: set[str] = set()
            text, attachments = normalize_outbound_reply(
                "[speaker=ME][timestamp=2026-06-08 10:00]\n"
                "这是分析结果：\n\n![趋势图](charts/plot.png)\n\n"
                "完整报告见 [PDF](report.pdf)，参考 [文档](https://example.com/docs) 与 [不存在](missing.txt)。",
                workspace_dir=root,
                channel="api",
                delivered_paths=delivered,
            )

            self.assertNotIn("[speaker=ME]", text)
            self.assertNotIn("charts/plot.png", text)
            self.assertNotIn("(report.pdf)", text)
            self.assertIn("[文档](https://example.com/docs)", text)
            self.assertIn("[不存在](missing.txt)", text)
            self.assertEqual([a["path"] for a in attachments], ["charts/plot.png", "report.pdf"])
            self.assertEqual(attachments[0]["kind"], "image")
            self.assertEqual(attachments[0]["caption"], "趋势图")
            self.assertEqual(attachments[1]["kind"], "file")

            # Already-delivered paths are not re-emitted on repeat links.
            text2, attachments2 = normalize_outbound_reply(
                "再看一眼 ![趋势图](charts/plot.png)",
                workspace_dir=root,
                channel="api",
                delivered_paths=delivered,
            )
            self.assertEqual(text2, "再看一眼")
            self.assertEqual(attachments2, [])

    def test_normalize_outbound_reply_skips_attachments_on_voice(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            (root / "note.txt").write_text("hi", encoding="utf-8")
            text, attachments = normalize_outbound_reply(
                "See [note](note.txt)",
                workspace_dir=root,
                channel="voice",
            )
            self.assertEqual(text, "See [note](note.txt)")
            self.assertEqual(attachments, [])

    async def test_agent_turn_automatically_extracts_markdown_workspace_attachments(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir).resolve()
            (workspace / "out.txt").write_text("summary data", encoding="utf-8")
            storage = chat_test.InMemoryMessageStorage()
            model_client = chat_test.CapturingModelClient([
                (ReplyType.SIMPLE_REPLY, "已整理好，请查收 [汇总](out.txt)"),
            ])
            agent = chat_test.AgentChatFlowTests()._build_agent(
                storage=storage,
                model_client=model_client,
            )
            agent.workspace_dir = str(workspace)
            events = [
                ev async for ev in Agent.chat_events(
                    agent, user_message="发我汇总文件", user_id="alice", channel="api",
                )
            ]
            done = next(ev for ev in events if ev.get("type") == "message_done" and ev.get("phase") == "final")
            self.assertEqual(done["content"], "已整理好，请查收")
            self.assertEqual(len(done["attachments"]), 1)
            self.assertEqual(done["attachments"][0]["path"], "out.txt")

    async def test_journal_format_diary_entry_strips_leading_markdown_date_headings(self):
        service = JournalLLMService(client=object())
        service._call_text = AsyncMock(return_value="## 2026-06-09 18:00\n今天帮 Bob 排查了网关超时。")
        entry = await service.format_diary_entry(
            [{"role": "user", "sender_id": "bob", "content": "网关超时了"}],
            journal_date="2026-06-09",
        )
        self.assertEqual(entry, "今天帮 Bob 排查了网关超时。")


class StructuredResponseFormatTests(unittest.IsolatedAsyncioTestCase):
    def test_provider_param_builders_map_response_format_cleanly(self):
        schema_fmt = SubconsciousLoop._build_response_format([
            ContactEntry(channel="api", user_id="alice", target={"user_id": "alice"}, last_seen=""),
        ])
        self.assertEqual(
            schema_fmt["json_schema"]["schema"]["properties"]["message"]["anyOf"][1]["properties"]["recipient"]["enum"],
            ["api:alice"],
        )

        empty_fmt = SubconsciousLoop._build_response_format([])
        self.assertEqual(empty_fmt["json_schema"]["schema"]["properties"]["message"], {"type": "null"})

        oai_chat = ModelClient(
            client=object(), model="gpt-4.1", model_api=MODEL_API_OPENAI_CHAT_COMPLETIONS, provider_name=PROVIDER_OPENAI,
        )
        chat_params = oai_chat._build_create_params(
            [{"role": "user", "content": "hi"}], None, None, False, response_format=schema_fmt,
        )
        self.assertEqual(chat_params["response_format"], schema_fmt)

        ds_chat = ModelClient(
            client=object(), model="deepseek-chat", model_api=MODEL_API_OPENAI_CHAT_COMPLETIONS, provider_name=PROVIDER_DEEPSEEK,
        )
        ds_params = ds_chat._build_create_params(
            [{"role": "user", "content": "hi"}], None, None, False, response_format=schema_fmt,
        )
        self.assertEqual(ds_params["response_format"], {"type": "json_object"})

        oai_resp = ModelClient(
            client=object(), model="gpt-5", model_api=MODEL_API_OPENAI_RESPONSES, provider_name=PROVIDER_OPENAI,
        )
        resp_params = oai_resp._build_responses_create_params(
            [{"role": "user", "content": "hi"}], None, None, False, response_format=schema_fmt,
        )
        self.assertEqual(resp_params["text"]["format"]["type"], "json_schema")
        self.assertEqual(resp_params["text"]["format"]["name"], "subconscious_reflection")

        ant = ModelClient(
            client=object(), model="claude-sonnet-4-5", model_api=MODEL_API_ANTHROPIC_MESSAGES, provider_name="anthropic",
        )
        ant_params = ant._build_anthropic_create_params(
            [{"role": "user", "content": "hi"}], None, None, False, response_format=schema_fmt,
        )
        self.assertEqual(ant_params["output_config"]["format"]["type"], "json_schema")

    async def test_create_response_falls_back_when_endpoint_rejects_response_format(self):
        seen_kwargs = []

        class RejectSchemaError(Exception):
            status_code = 400

        async def create(**kwargs):
            seen_kwargs.append(kwargs)
            if "response_format" in kwargs:
                raise RejectSchemaError("response_format json_schema is not supported by this model")
            return SimpleNamespace(
                id="resp-1",
                choices=[SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content='{"should_reply": true, "reason": "ok"}', tool_calls=None, refusal=None),
                )],
                usage=None,
            )

        fake_sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        mc = ModelClient(
            client=fake_sdk,
            model="gpt-4.1",
            model_api=MODEL_API_OPENAI_CHAT_COMPLETIONS,
            provider_name=PROVIDER_OPENAI,
        )
        schema_fmt = {
            "type": "json_schema",
            "json_schema": {"name": "test", "strict": True, "schema": {"type": "object"}},
        }
        reply_type, payload = await mc.call(
            [{"role": "user", "content": "ping"}], None, response_format=schema_fmt,
        )
        self.assertEqual(reply_type, ReplyType.SIMPLE_REPLY)
        self.assertEqual(json.loads(payload)["should_reply"], True)
        self.assertEqual(len(seen_kwargs), 2)
        self.assertIn("response_format", seen_kwargs[0])
        self.assertNotIn("response_format", seen_kwargs[1])

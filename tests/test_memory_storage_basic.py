import asyncio
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from xagent.components.memory.basic_memory import MemoryStorageBasic
from xagent.components.message.local_messages import MessageStorageLocal
from xagent.schemas import Message, RoleType


class FakeJournalLLMService:
    def __init__(self, *args, **kwargs):
        self.rewrite_calls = []
        self.keyword_calls = []
        self.rewrite_responses = []
        self.keyword_responses = []

    async def rewrite_daily_journal(
        self,
        existing_journal: str,
        new_transcript: str,
        journal_date: str,
    ) -> str:
        self.rewrite_calls.append(
            {
                "existing_journal": existing_journal,
                "new_transcript": new_transcript,
                "journal_date": journal_date,
            }
        )
        if self.rewrite_responses:
            return self.rewrite_responses.pop(0)
        return existing_journal or new_transcript

    async def extract_query_keywords(self, query: str, max_keywords: int = 5):
        self.keyword_calls.append({"query": query, "max_keywords": max_keywords})
        if self.keyword_responses:
            return self.keyword_responses.pop(0)
        return [query]


class MemoryStorageBasicTests(unittest.IsolatedAsyncioTestCase):
    def make_storage(
        self,
        db_path: Path,
        threshold: int = 1,
        interval_seconds: int = 0,
        max_batch_messages: int = 40,
    ):
        message_storage = MessageStorageLocal(path=str(db_path))
        with patch("xagent.components.memory.basic_memory.JournalLLMService", FakeJournalLLMService):
            storage = MemoryStorageBasic(
                path=str(db_path),
                memory_threshold=threshold,
                memory_interval_seconds=interval_seconds,
                max_batch_messages=max_batch_messages,
                message_storage=message_storage,
            )
        return storage, message_storage

    @staticmethod
    def make_message(content: str, role: RoleType, sender_id: str, timestamp: float) -> Message:
        return Message(
            role=role,
            sender_id=sender_id,
            content=content,
            timestamp=timestamp,
        )

    @staticmethod
    def local_timestamp(year: int, month: int, day: int, hour: int, minute: int = 0) -> float:
        return datetime(year, month, day, hour, minute).timestamp()

    def fetch_rows(self, db_path: Path, sql: str, params=()):
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(sql, params).fetchall()

    async def test_same_day_flush_rewrites_single_row(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "messages.sqlite3"
            storage, message_storage = self.make_storage(db_path, threshold=1, interval_seconds=0)
            storage.llm_service.rewrite_responses.extend(
                [
                    "上午确定了发布计划。",
                    "上午确定了发布计划，下午补充了评审安排。",
                ]
            )

            first = self.make_message(
                "上午我们确定了发布计划。",
                role=RoleType.USER,
                sender_id="alice",
                timestamp=self.local_timestamp(2026, 3, 18, 9),
            )
            second = self.make_message(
                "下午补充了评审安排。",
                role=RoleType.USER,
                sender_id="alice",
                timestamp=self.local_timestamp(2026, 3, 18, 15),
            )

            await message_storage.add_messages(first)
            await storage.add("agent:test", [first.to_model_input()])

            await message_storage.add_messages(second)
            await storage.add("agent:test", [second.to_model_input()])

            rows = self.fetch_rows(
                db_path,
                """
                SELECT journal_date, content
                FROM journals
                WHERE memory_key = ?
                """,
                ("agent:test",),
            )
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["journal_date"], "2026-03-18")
            self.assertEqual(rows[0]["content"], "上午确定了发布计划，下午补充了评审安排。")
            self.assertEqual(len(storage.llm_service.rewrite_calls), 2)

    async def test_explicit_memory_trigger_bypasses_threshold(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "messages.sqlite3"
            storage, message_storage = self.make_storage(db_path, threshold=5, interval_seconds=300)
            storage.llm_service.rewrite_responses.append("alice 喜欢乌龙茶。")

            message = self.make_message(
                "记住这个：我喜欢乌龙茶。",
                role=RoleType.USER,
                sender_id="alice",
                timestamp=self.local_timestamp(2026, 3, 18, 10),
            )
            await message_storage.add_messages(message)
            await storage.add("agent:test", [message.to_model_input()])

            rows = self.fetch_rows(
                db_path,
                "SELECT content FROM journals WHERE memory_key = ?",
                ("agent:test",),
            )
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["content"], "alice 喜欢乌龙茶。")
            self.assertEqual(len(storage.llm_service.rewrite_calls), 1)

    async def test_last_processed_message_id_persists_across_restart(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "messages.sqlite3"
            storage, message_storage = self.make_storage(db_path, threshold=1, interval_seconds=0)
            storage.llm_service.rewrite_responses.append("alice 完成了第一次记录。")

            message = self.make_message(
                "今天完成了第一次记录。",
                role=RoleType.USER,
                sender_id="alice",
                timestamp=self.local_timestamp(2026, 3, 18, 11),
            )
            await message_storage.add_messages(message)
            await storage.add("agent:test", [message.to_model_input()])

            restarted_storage, _ = self.make_storage(db_path, threshold=1, interval_seconds=0)
            await restarted_storage.add(
                "agent:test",
                [{"role": "user", "content": "不会触发，因为没有新消息"}],
            )

            state_rows = self.fetch_rows(
                db_path,
                """
                SELECT last_processed_message_id
                FROM journal_state
                WHERE memory_key = ?
                """,
                ("agent:test",),
            )
            self.assertEqual(len(restarted_storage.llm_service.rewrite_calls), 0)
            self.assertEqual(int(state_rows[0]["last_processed_message_id"]), 1)

    async def test_cross_day_batch_writes_two_journals(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "messages.sqlite3"
            storage, message_storage = self.make_storage(db_path, threshold=2, interval_seconds=0)
            storage.llm_service.rewrite_responses.extend(
                [
                    "3月18日整理了路线图。",
                    "3月19日更新了评审材料。",
                ]
            )

            first = self.make_message(
                "今天整理了路线图。",
                role=RoleType.USER,
                sender_id="alice",
                timestamp=self.local_timestamp(2026, 3, 18, 22),
            )
            second = self.make_message(
                "第二天更新了评审材料。",
                role=RoleType.USER,
                sender_id="alice",
                timestamp=self.local_timestamp(2026, 3, 19, 9),
            )
            await message_storage.add_messages([first, second])
            await storage.add("agent:test", [second.to_model_input()])

            rows = self.fetch_rows(
                db_path,
                """
                SELECT journal_date, content
                FROM journals
                WHERE memory_key = ?
                ORDER BY journal_date ASC
                """,
                ("agent:test",),
            )
            self.assertEqual(
                [(row["journal_date"], row["content"]) for row in rows],
                [
                    ("2026-03-18", "3月18日整理了路线图。"),
                    ("2026-03-19", "3月19日更新了评审材料。"),
                ],
            )

    async def test_retrieve_supports_date_keyword_and_like_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "messages.sqlite3"
            storage, _ = self.make_storage(db_path, threshold=1, interval_seconds=0)

            await asyncio.to_thread(
                storage._upsert_journal_sync,
                "agent:test",
                "2026-03-18",
                "今天讨论了东京行程和乌龙茶偏好。",
            )
            await asyncio.to_thread(
                storage._upsert_journal_sync,
                "agent:test",
                "2026-03-19",
                "下午做了复盘，并更新了发布计划。",
            )

            storage.llm_service.keyword_responses.extend(
                [
                    ["东京", "乌龙茶"],
                    ["复盘"],
                    ["东京"],
                ]
            )

            keyword_results = await storage.retrieve(
                "agent:test",
                query="东京和乌龙茶",
                limit=5,
            )
            date_only_results = await storage.retrieve(
                "agent:test",
                journal_date="2026-03-19",
                limit=5,
            )
            like_results = await storage.retrieve(
                "agent:test",
                query="复盘",
                limit=5,
            )
            filtered_results = await storage.retrieve(
                "agent:test",
                query="东京",
                journal_date="2026-03-18",
                limit=5,
            )

            self.assertEqual(keyword_results[0]["metadata"]["journal_date"], "2026-03-18")
            self.assertEqual(
                keyword_results[0]["metadata"]["matched_keywords"],
                ["东京", "乌龙茶"],
            )
            self.assertEqual(len(date_only_results), 1)
            self.assertEqual(date_only_results[0]["metadata"]["journal_date"], "2026-03-19")
            self.assertEqual(like_results[0]["metadata"]["journal_date"], "2026-03-19")
            self.assertIn("复盘", like_results[0]["metadata"]["matched_keywords"])
            self.assertEqual(len(filtered_results), 1)
            self.assertEqual(filtered_results[0]["metadata"]["journal_date"], "2026-03-18")

    async def test_journal_fts_trigger_tracks_updates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "messages.sqlite3"
            storage, _ = self.make_storage(db_path, threshold=1, interval_seconds=0)

            await asyncio.to_thread(
                storage._upsert_journal_sync,
                "agent:test",
                "2026-03-18",
                "我喜欢乌龙茶。",
            )
            await asyncio.to_thread(
                storage._upsert_journal_sync,
                "agent:test",
                "2026-03-18",
                "我喜欢铁观音。",
            )

            with sqlite3.connect(db_path) as conn:
                old_rows = conn.execute(
                    "SELECT rowid FROM journal_fts WHERE journal_fts MATCH ?",
                    ("乌龙茶",),
                ).fetchall()
                new_rows = conn.execute(
                    "SELECT rowid FROM journal_fts WHERE journal_fts MATCH ?",
                    ("铁观音",),
                ).fetchall()

            self.assertEqual(old_rows, [])
            self.assertEqual(len(new_rows), 1)

    async def test_sender_aliases_are_stable_for_technical_ids_and_keep_readable_ids(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "messages.sqlite3"
            storage, _ = self.make_storage(db_path, threshold=1, interval_seconds=0)

            first_batch = storage._format_messages_for_storage(
                memory_key="agent:test",
                journal_date="2026-03-18",
                messages=[
                    {
                        "role": "user",
                        "type": "message",
                        "sender_id": "alice",
                        "content": "我来负责路线图。",
                        "timestamp": self.local_timestamp(2026, 3, 18, 9),
                    },
                    {
                        "role": "user",
                        "type": "message",
                        "sender_id": "550e8400-e29b-41d4-a716-446655440000",
                        "content": "我负责评审。",
                        "timestamp": self.local_timestamp(2026, 3, 18, 10),
                    },
                    {
                        "role": "user",
                        "type": "message",
                        "sender_id": "b7f4c2d9-91cb-47f2-a57d-7d6c84b91b6a",
                        "content": "我补充一下测试安排。",
                        "timestamp": self.local_timestamp(2026, 3, 18, 11),
                    },
                ],
            )
            second_batch = storage._format_messages_for_storage(
                memory_key="agent:test",
                journal_date="2026-03-18",
                messages=[
                    {
                        "role": "user",
                        "type": "message",
                        "sender_id": "b7f4c2d9-91cb-47f2-a57d-7d6c84b91b6a",
                        "content": "我继续跟进测试安排。",
                        "timestamp": self.local_timestamp(2026, 3, 18, 15),
                    },
                    {
                        "role": "user",
                        "type": "message",
                        "sender_id": "550e8400-e29b-41d4-a716-446655440000",
                        "content": "评审我这边继续推进。",
                        "timestamp": self.local_timestamp(2026, 3, 18, 16),
                    },
                ],
            )

            self.assertIn("User alice:", first_batch)
            self.assertIn("User 用户A:", first_batch)
            self.assertIn("User 用户B:", first_batch)
            self.assertIn("User 用户B:", second_batch)
            self.assertIn("User 用户A:", second_batch)


if __name__ == "__main__":
    unittest.main()

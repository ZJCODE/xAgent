import unittest

from xagent.core.handlers.memory import MemoryManager


class _FakeMemoryStorage:
    def __init__(self):
        self.calls = []
        self.by_date = {}
        self.search_results = []

    async def retrieve(self, memory_key: str, query: str = "", limit: int = 5, journal_date=None):
        self.calls.append(
            {
                "memory_key": memory_key,
                "query": query,
                "limit": limit,
                "journal_date": journal_date,
            }
        )
        if journal_date is not None:
            return list(self.by_date.get(journal_date, []))
        return list(self.search_results)


class _FakeMessageStorage:
    path = "/tmp/fake.sqlite3"


class MemoryManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_retrieve_memories_always_includes_recent_two_days(self):
        storage = _FakeMemoryStorage()
        manager = MemoryManager(memory_storage=storage, message_storage=_FakeMessageStorage())
        recent_dates = manager._retrieve_recent_day_memories.__func__.__globals__["datetime"].now().astimezone().date()
        today = recent_dates.strftime("%Y-%m-%d")
        yesterday = (recent_dates - manager._retrieve_recent_day_memories.__func__.__globals__["timedelta"](days=1)).strftime("%Y-%m-%d")

        storage.by_date[today] = [
            {
                "id": "today-id",
                "content": "今天的日记",
                "metadata": {"journal_date": today},
            }
        ]
        storage.by_date[yesterday] = [
            {
                "id": "yesterday-id",
                "content": "昨天的日记",
                "metadata": {"journal_date": yesterday},
            }
        ]
        storage.search_results = [
            {
                "id": "search-id",
                "content": "关键词命中的更早日记",
                "metadata": {"journal_date": "2026-03-01"},
            }
        ]

        results = await manager.retrieve_memories(
            memory_key="agent:test",
            query="路线图",
            limit=5,
        )

        self.assertEqual([item["id"] for item in results], ["today-id", "yesterday-id", "search-id"])
        self.assertEqual(storage.calls[0]["journal_date"], today)
        self.assertEqual(storage.calls[1]["journal_date"], yesterday)
        self.assertEqual(storage.calls[2]["query"], "路线图")

    async def test_retrieve_memories_deduplicates_recent_day_and_search_results(self):
        storage = _FakeMemoryStorage()
        manager = MemoryManager(memory_storage=storage, message_storage=_FakeMessageStorage())
        current_date = manager._retrieve_recent_day_memories.__func__.__globals__["datetime"].now().astimezone().date()
        today = current_date.strftime("%Y-%m-%d")

        storage.by_date[today] = [
            {
                "id": "today-id",
                "content": "今天的日记",
                "metadata": {"journal_date": today},
            }
        ]
        storage.search_results = [
            {
                "id": "today-id",
                "content": "今天的日记",
                "metadata": {"journal_date": today},
            },
            {
                "id": "other-id",
                "content": "别的日记",
                "metadata": {"journal_date": "2026-03-01"},
            },
        ]

        results = await manager.retrieve_memories(
            memory_key="agent:test",
            query="今天",
            limit=5,
        )

        self.assertEqual([item["id"] for item in results], ["today-id", "other-id"])

    async def test_explicit_journal_date_bypasses_recent_day_injection(self):
        storage = _FakeMemoryStorage()
        manager = MemoryManager(memory_storage=storage, message_storage=_FakeMessageStorage())
        storage.by_date["2026-03-18"] = [
            {
                "id": "date-id",
                "content": "指定日期日记",
                "metadata": {"journal_date": "2026-03-18"},
            }
        ]

        results = await manager.retrieve_memories(
            memory_key="agent:test",
            query="",
            limit=5,
            journal_date="2026-03-18",
        )

        self.assertEqual([item["id"] for item in results], ["date-id"])
        self.assertEqual(len(storage.calls), 1)
        self.assertEqual(storage.calls[0]["journal_date"], "2026-03-18")


if __name__ == "__main__":
    unittest.main()

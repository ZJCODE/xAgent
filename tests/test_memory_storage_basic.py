import unittest
from unittest.mock import patch

from xagent.components.memory.basic_memory import MemoryStorageBasic
from xagent.components.memory.vector.base_vector_store import VectorDoc
from xagent.schemas import Message, RoleType
from xagent.schemas.memory import MemoryExtraction, MemoryPiece, MemoryType


class FakeLLMService:
    def __init__(self, *args, **kwargs):
        self.calls = []
        self.responses = []

    async def extract_memories_from_content(self, content: str) -> MemoryExtraction:
        self.calls.append(content)
        if self.responses:
            return self.responses.pop(0)
        return MemoryExtraction(memories=[])


class FakeVectorStore:
    def __init__(self):
        self.upserts = []
        self.query_results = []
        self.deleted_ids = []
        self.deleted_filters = []

    async def upsert(self, ids, documents, metadatas):
        self.upserts.append({
            "ids": ids,
            "documents": documents,
            "metadatas": metadatas,
        })

    async def query(self, query_texts=None, n_results=5, meta_filter=None):
        if self.query_results:
            return self.query_results.pop(0)
        return []

    async def delete(self, ids):
        self.deleted_ids.append(ids)

    async def delete_by_filter(self, meta_filter):
        self.deleted_filters.append(meta_filter)


class FakeMessageStorage:
    def __init__(self):
        self.conversations = {}

    def append(self, conversation_id: str, message: Message) -> None:
        self.conversations.setdefault(conversation_id, []).append(message)

    async def get_message_count(self, conversation_id: str) -> int:
        return len(self.conversations.get(conversation_id, []))

    async def get_messages(self, conversation_id: str, count: int = 20):
        return list(self.conversations.get(conversation_id, []))[-count:]


class MemoryStorageBasicTests(unittest.IsolatedAsyncioTestCase):
    def make_storage(self, threshold: int = 2):
        vector_store = FakeVectorStore()
        message_storage = FakeMessageStorage()

        with patch("xagent.components.memory.basic_memory.MemoryLLMService", FakeLLMService):
            storage = MemoryStorageBasic(
                memory_threshold=threshold,
                message_storage=message_storage,
                vector_store=vector_store,
            )

        return storage, message_storage, vector_store

    async def test_explicit_memory_trigger_bypasses_threshold(self):
        storage, message_storage, vector_store = self.make_storage(threshold=3)
        storage.llm_service.responses.append(
            MemoryExtraction(
                memories=[
                    MemoryPiece(
                        content="alice 喜欢乌龙茶。",
                        type=MemoryType.PROFILE,
                    )
                ]
            )
        )

        user_message = Message.create(
            content="记住这个：我喜欢乌龙茶。",
            role=RoleType.USER,
            sender_id="alice",
        )
        message_storage.append("team", user_message)

        await storage.add(
            memory_key="agent:test",
            conversation_id="team",
            messages=[user_message.to_model_input()],
        )

        self.assertEqual(len(storage.llm_service.calls), 1)
        self.assertEqual(len(vector_store.upserts), 1)
        self.assertEqual(vector_store.upserts[0]["documents"], ["alice 喜欢乌龙茶。"])

    async def test_assistant_messages_do_not_advance_threshold(self):
        storage, message_storage, vector_store = self.make_storage(threshold=2)
        storage.llm_service.responses.append(
            MemoryExtraction(
                memories=[
                    MemoryPiece(
                        content="alice 正在准备季度复盘。",
                        type=MemoryType.EPISODIC,
                    )
                ]
            )
        )

        first_user = Message.create(
            content="我在准备季度复盘。",
            role=RoleType.USER,
            sender_id="alice",
        )
        assistant = Message.create(
            content="明白了，我可以帮你整理结构。",
            role=RoleType.ASSISTANT,
            sender_id="agent:test",
        )
        second_user = Message.create(
            content="我想先梳理项目里程碑。",
            role=RoleType.USER,
            sender_id="alice",
        )

        message_storage.append("team", first_user)
        await storage.add("agent:test", "team", [first_user.to_model_input()])
        self.assertEqual(len(storage.llm_service.calls), 0)

        message_storage.append("team", assistant)
        await storage.add("agent:test", "team", [assistant.to_model_input()])
        self.assertEqual(len(storage.llm_service.calls), 0)

        message_storage.append("team", second_user)
        await storage.add("agent:test", "team", [second_user.to_model_input()])

        self.assertEqual(len(storage.llm_service.calls), 1)
        self.assertIn("User alice: 我在准备季度复盘。", storage.llm_service.calls[0])
        self.assertIn("Assistant agent:test: 明白了，我可以帮你整理结构。", storage.llm_service.calls[0])
        self.assertIn("User alice: 我想先梳理项目里程碑。", storage.llm_service.calls[0])
        self.assertEqual(len(vector_store.upserts), 1)

    async def test_extraction_reads_only_unprocessed_transcript_segment(self):
        storage, message_storage, vector_store = self.make_storage(threshold=2)
        storage.llm_service.responses.extend(
            [
                MemoryExtraction(
                    memories=[
                        MemoryPiece(content="alice 喜欢复盘。", type=MemoryType.PROFILE)
                    ]
                ),
                MemoryExtraction(
                    memories=[
                        MemoryPiece(content="alice 本周要提交路线图。", type=MemoryType.EPISODIC)
                    ]
                ),
            ]
        )

        first = Message.create("我很重视复盘。", role=RoleType.USER, sender_id="alice")
        second = Message.create("我喜欢在周五做总结。", role=RoleType.USER, sender_id="alice")
        third = Message.create("这周我要提交路线图。", role=RoleType.USER, sender_id="alice")
        fourth = Message.create("我还要准备评审材料。", role=RoleType.USER, sender_id="alice")

        for message in [first, second]:
            message_storage.append("team", message)
            await storage.add("agent:test", "team", [message.to_model_input()])

        for message in [third, fourth]:
            message_storage.append("team", message)
            await storage.add("agent:test", "team", [message.to_model_input()])

        self.assertEqual(len(storage.llm_service.calls), 2)
        self.assertIn("我很重视复盘。", storage.llm_service.calls[0])
        self.assertIn("我喜欢在周五做总结。", storage.llm_service.calls[0])
        self.assertNotIn("这周我要提交路线图。", storage.llm_service.calls[0])

        self.assertIn("这周我要提交路线图。", storage.llm_service.calls[1])
        self.assertIn("我还要准备评审材料。", storage.llm_service.calls[1])
        self.assertNotIn("我很重视复盘。", storage.llm_service.calls[1])
        self.assertEqual(len(vector_store.upserts), 2)

    async def test_duplicate_store_is_skipped_and_retrieve_filters_low_scores(self):
        storage, _, vector_store = self.make_storage(threshold=1)
        storage.llm_service.responses.extend(
            [
                MemoryExtraction(
                    memories=[
                        MemoryPiece(content="alice 喜欢乌龙茶。", type=MemoryType.PROFILE)
                    ]
                ),
                MemoryExtraction(
                    memories=[
                        MemoryPiece(content="alice 喜欢乌龙茶。", type=MemoryType.PROFILE)
                    ]
                ),
            ]
        )

        vector_store.query_results.extend(
            [
                [],
                [
                    VectorDoc(
                        id="existing-profile",
                        document="alice 喜欢乌龙茶。",
                        metadata={"memory_type": "profile"},
                        score=0.99,
                    )
                ],
                [
                    VectorDoc(
                        id="episodic-1",
                        document="alice 上周完成了发布复盘。",
                        metadata={"memory_type": "episodic", "created_timestamp": 1},
                        score=0.91,
                    ),
                    VectorDoc(
                        id="profile-1",
                        document="alice 喜欢乌龙茶。",
                        metadata={"memory_type": "profile", "created_timestamp": 2},
                        score=0.72,
                    ),
                    VectorDoc(
                        id="low-score",
                        document="无关记忆。",
                        metadata={"memory_type": "profile", "created_timestamp": 3},
                        score=0.1,
                    ),
                ],
            ]
        )

        await storage.store("agent:test", "User alice: 我喜欢乌龙茶。")
        await storage.store("agent:test", "User alice: 我还是喜欢乌龙茶。")
        results = await storage.retrieve("agent:test", "alice 喜欢什么茶？", limit=3)

        self.assertEqual(len(vector_store.upserts), 1)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["metadata"]["memory_type"], "profile")
        self.assertEqual(results[1]["metadata"]["memory_type"], "episodic")
        self.assertEqual(results[0]["content"], "alice 喜欢乌龙茶。")


if __name__ == "__main__":
    unittest.main()

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
        self.messages = []

    def append(self, message: Message) -> None:
        self.messages.append(message)

    async def get_message_count(self) -> int:
        return len(self.messages)

    async def get_messages(self, count: int = 20):
        return list(self.messages)[-count:]


class MemoryStorageBasicTests(unittest.IsolatedAsyncioTestCase):
    def make_storage(
        self,
        threshold: int = 2,
        interval_seconds: int = 300,
        max_batch_messages: int = 40,
    ):
        vector_store = FakeVectorStore()
        message_storage = FakeMessageStorage()

        with patch("xagent.components.memory.basic_memory.MemoryLLMService", FakeLLMService):
            storage = MemoryStorageBasic(
                memory_threshold=threshold,
                memory_interval_seconds=interval_seconds,
                max_batch_messages=max_batch_messages,
                message_storage=message_storage,
                vector_store=vector_store,
            )

        return storage, message_storage, vector_store

    async def test_explicit_memory_trigger_bypasses_periodic_threshold(self):
        storage, message_storage, vector_store = self.make_storage(threshold=5)
        storage.llm_service.responses.append(
            MemoryExtraction(
                memories=[
                    MemoryPiece(
                        content="alice 喜欢乌龙茶。",
                        type=MemoryType.SEMANTIC,
                    )
                ]
            )
        )

        user_message = Message.create(
            content="记住这个：我喜欢乌龙茶。",
            role=RoleType.USER,
            sender_id="alice",
        )
        message_storage.append(user_message)

        await storage.add(
            memory_key="agent:test",
            messages=[user_message.to_model_input()],
        )

        self.assertEqual(len(storage.llm_service.calls), 1)
        self.assertEqual(len(vector_store.upserts), 1)
        self.assertEqual(vector_store.upserts[0]["documents"], ["alice 喜欢乌龙茶。"])

    async def test_periodic_extraction_waits_for_interval_after_first_batch(self):
        storage, message_storage, _ = self.make_storage(threshold=2, interval_seconds=300)
        storage.llm_service.responses.extend(
            [
                MemoryExtraction(
                    memories=[
                        MemoryPiece(
                            content="alice 负责季度复盘。",
                            type=MemoryType.SEMANTIC,
                        )
                    ]
                ),
                MemoryExtraction(
                    memories=[
                        MemoryPiece(
                            content="alice 本周要提交路线图。",
                            type=MemoryType.EPISODIC,
                        )
                    ]
                ),
            ]
        )

        first_user = Message.create("我在准备季度复盘。", role=RoleType.USER, sender_id="alice")
        assistant = Message.create("明白了，我可以帮你整理结构。", role=RoleType.ASSISTANT, sender_id="agent:test")
        second_user = Message.create("我负责这次季度复盘。", role=RoleType.USER, sender_id="alice")

        for message in [first_user, assistant, second_user]:
            message_storage.append(message)

        await storage.add("agent:test", [second_user.to_model_input()])
        self.assertEqual(len(storage.llm_service.calls), 1)

        assistant_follow_up = Message.create("我建议先列里程碑。", role=RoleType.ASSISTANT, sender_id="agent:test")
        third_user = Message.create("这周我要提交路线图。", role=RoleType.USER, sender_id="alice")
        for message in [assistant_follow_up, third_user]:
            message_storage.append(message)

        await storage.add("agent:test", [third_user.to_model_input()])
        self.assertEqual(len(storage.llm_service.calls), 1)

        storage._stream_states["agent:test"].last_extracted_at -= 301
        fourth_user = Message.create("我还要准备评审材料。", role=RoleType.USER, sender_id="alice")
        message_storage.append(fourth_user)

        await storage.add("agent:test", [fourth_user.to_model_input()])
        self.assertEqual(len(storage.llm_service.calls), 2)
        self.assertIn("这周我要提交路线图。", storage.llm_service.calls[1])
        self.assertIn("我还要准备评审材料。", storage.llm_service.calls[1])

    async def test_extraction_reads_unprocessed_batches_only(self):
        storage, message_storage, vector_store = self.make_storage(
            threshold=2,
            interval_seconds=0,
            max_batch_messages=2,
        )
        storage.llm_service.responses.extend(
            [
                MemoryExtraction(
                    memories=[
                        MemoryPiece(content="alice 喜欢复盘。", type=MemoryType.SEMANTIC)
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
            message_storage.append(message)
            await storage.add("agent:test", [message.to_model_input()])

        for message in [third, fourth]:
            message_storage.append(message)
            await storage.add("agent:test", [message.to_model_input()])

        self.assertEqual(len(storage.llm_service.calls), 2)
        self.assertIn("我很重视复盘。", storage.llm_service.calls[0])
        self.assertIn("我喜欢在周五做总结。", storage.llm_service.calls[0])
        self.assertNotIn("这周我要提交路线图。", storage.llm_service.calls[0])

        self.assertIn("这周我要提交路线图。", storage.llm_service.calls[1])
        self.assertIn("我还要准备评审材料。", storage.llm_service.calls[1])
        self.assertNotIn("我很重视复盘。", storage.llm_service.calls[1])
        self.assertEqual(len(vector_store.upserts), 2)

    async def test_retrieve_orders_memory_types_and_filters_low_scores(self):
        storage, _, vector_store = self.make_storage(threshold=1)
        vector_store.query_results.append(
            [
                VectorDoc(
                    id="semantic-1",
                    document="alice 是销售负责人。",
                    metadata={
                        "memory_type": "semantic",
                        "created_timestamp": 2,
                    },
                    score=0.71,
                ),
                VectorDoc(
                    id="social-1",
                    document="alice 和 bob 在 Phoenix 项目群里共享项目上下文。",
                    metadata={
                        "memory_type": "social",
                        "created_timestamp": 3,
                    },
                    score=0.66,
                ),
                VectorDoc(
                    id="episodic-1",
                    document="alice 下周要去东京出差。",
                    metadata={
                        "memory_type": "episodic",
                        "created_timestamp": 5,
                    },
                    score=0.88,
                ),
                VectorDoc(
                    id="self-1",
                    document="对 alice 回答时应更简洁。",
                    metadata={
                        "memory_type": "self",
                        "created_timestamp": 4,
                    },
                    score=0.95,
                ),
                VectorDoc(
                    id="low-score",
                    document="无关记忆。",
                    metadata={
                        "memory_type": "semantic",
                        "created_timestamp": 6,
                    },
                    score=0.1,
                ),
            ]
        )

        results = await storage.retrieve(
            "agent:test",
            "alice 的上下文是什么？",
            limit=4,
        )

        self.assertEqual(len(results), 4)
        self.assertEqual(results[0]["metadata"]["memory_type"], "semantic")
        self.assertEqual(results[1]["metadata"]["memory_type"], "social")
        self.assertEqual(results[2]["metadata"]["memory_type"], "episodic")
        self.assertEqual(results[3]["metadata"]["memory_type"], "self")
        self.assertTrue(all("无关记忆" not in item["content"] for item in results))

    async def test_same_content_is_deduplicated_across_the_agent_memory_pool(self):
        storage, _, vector_store = self.make_storage(threshold=1)
        storage.llm_service.responses.extend(
            [
                MemoryExtraction(
                    memories=[
                        MemoryPiece(
                            content="项目 Phoenix 需要严格控制共享范围。",
                            type=MemoryType.SOCIAL,
                        )
                    ]
                ),
                MemoryExtraction(
                    memories=[
                        MemoryPiece(
                            content="项目 Phoenix 需要严格控制共享范围。",
                            type=MemoryType.SOCIAL,
                        )
                    ]
                ),
            ]
        )

        vector_store.query_results.extend(
            [
                [],
                [
                    VectorDoc(
                        id="existing-social",
                        document="项目 Phoenix 需要严格控制共享范围。",
                        metadata={
                            "memory_type": "social",
                        },
                        score=0.99,
                    )
                ],
            ]
        )

        await storage.store("agent:test", "User alice: 项目 Phoenix 需要严格控制共享范围。")
        await storage.store("agent:test", "User bob: 项目 Phoenix 需要严格控制共享范围。")

        self.assertEqual(len(vector_store.upserts), 1)


if __name__ == "__main__":
    unittest.main()

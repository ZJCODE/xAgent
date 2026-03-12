import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from xagent.core.agent import Agent
from xagent.multi.workflow import (
    AgentDependency,
    AgentSpec,
    AgentsList,
    DependenciesSpec,
    GraphWorkflow,
    Workflow,
    WorkflowPatternType,
    WorkflowResult,
)


class InMemoryMessageStorage:
    def __init__(self):
        self._messages = {}

    async def add_messages(self, user_id, session_id, messages, **kwargs):
        bucket = self._messages.setdefault((user_id, session_id), [])
        if isinstance(messages, list):
            bucket.extend(messages)
        else:
            bucket.append(messages)

    async def get_messages(self, user_id, session_id, count=20):
        return list(self._messages.get((user_id, session_id), []))[-count:]

    async def clear_history(self, user_id, session_id):
        self._messages.pop((user_id, session_id), None)

    async def pop_message(self, user_id, session_id):
        bucket = self._messages.get((user_id, session_id), [])
        return bucket.pop() if bucket else None


class TrackingMemoryStorage:
    def __init__(self, preprocess_decider=None, fail_add_times=0):
        self.retrieve_calls = []
        self.add_calls = 0
        self.fail_add_times = fail_add_times
        self.llm_service = SimpleNamespace(
            should_preprocess_query=preprocess_decider
            or (lambda query, pre_chat=None: False)
        )

    async def add(self, user_id, messages):
        self.add_calls += 1
        if self.add_calls <= self.fail_add_times:
            raise RuntimeError("temporary add failure")
        return None

    async def store(self, user_id, content):
        return ""

    async def retrieve(self, user_id, query, limit=5, query_context=None, enable_query_process=False):
        self.retrieve_calls.append(
            {
                "user_id": user_id,
                "query": query,
                "enable_query_process": enable_query_process,
                "query_context": query_context,
            }
        )
        return []

    async def extract_meta(self, user_id, days=1):
        return []

    async def clear(self, user_id):
        return None

    async def delete(self, memory_ids):
        return None


class DummyResponsesAPI:
    def __init__(self, create_responses):
        self._create_responses = list(create_responses)

    async def create(self, **kwargs):
        if not self._create_responses:
            raise AssertionError("No fake create response configured")
        response = self._create_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def parse(self, **kwargs):
        raise AssertionError("Structured output is not expected in this test")


def make_stub_agent(name: str) -> Agent:
    return Agent(
        name=name,
        client=SimpleNamespace(responses=DummyResponsesAPI([])),
        message_storage=InMemoryMessageStorage(),
        memory_storage=TrackingMemoryStorage(),
    )


class MemoryRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_memory_query_preprocess_uses_heuristics(self):
        memory_storage = TrackingMemoryStorage(
            preprocess_decider=lambda query, pre_chat=None: "tomorrow" in query.lower()
        )
        agent = Agent(
            name="MemoryAgent",
            client=SimpleNamespace(
                responses=DummyResponsesAPI(
                    [
                        SimpleNamespace(output_text="first", output=[]),
                        SimpleNamespace(output_text="second", output=[]),
                    ]
                )
            ),
            message_storage=InMemoryMessageStorage(),
            memory_storage=memory_storage,
        )

        await agent.chat(
            user_message="What about tomorrow?",
            user_id="user-1",
            session_id="session-1",
            enable_memory=True,
        )
        if agent._background_tasks:
            await asyncio.gather(*list(agent._background_tasks))
        self.assertTrue(memory_storage.retrieve_calls[-1]["enable_query_process"])

        await agent.chat(
            user_message="Tell me about databases",
            user_id="user-1",
            session_id="session-2",
            enable_memory=True,
        )
        if agent._background_tasks:
            await asyncio.gather(*list(agent._background_tasks))
        self.assertFalse(memory_storage.retrieve_calls[-1]["enable_query_process"])

    async def test_background_memory_add_retries_before_failing(self):
        memory_storage = TrackingMemoryStorage(fail_add_times=1)
        agent = Agent(
            name="MemoryAgent",
            client=SimpleNamespace(responses=DummyResponsesAPI([])),
            message_storage=InMemoryMessageStorage(),
            memory_storage=memory_storage,
        )

        agent._schedule_memory_add(
            user_id="user-1",
            messages=[{"role": "user", "content": "hello"}],
            description="retry-test",
        )
        await asyncio.gather(*list(agent._background_tasks))

        self.assertEqual(memory_storage.add_calls, 2)

    def test_cloud_memory_defaults_to_redis_buffer_when_optional_deps_exist(self):
        try:
            from xagent.components.memory.cloud_memory import MemoryStorageCloud
        except ImportError:
            self.skipTest("Optional cloud dependencies are not available")

        class FakeRedisBuffer:
            def __init__(self, max_messages=100):
                self.max_messages = max_messages

        class FakeVectorStore:
            async def upsert(self, ids, documents, metadatas):
                return None

            async def query(self, query_texts=None, n_results=5, meta_filter=None, keywords_filter=None):
                return []

            async def delete(self, ids):
                return None

            async def delete_by_filter(self, meta_filter):
                return None

        class FakeMemoryLLMService:
            def should_preprocess_query(self, query, pre_chat=None):
                return False

        with patch("xagent.components.memory.cloud_memory.MessageBufferRedis", FakeRedisBuffer):
            with patch("xagent.components.memory.basic_memory.MemoryLLMService", FakeMemoryLLMService):
                memory_storage = MemoryStorageCloud(vector_store=FakeVectorStore())

        self.assertIsInstance(memory_storage.message_buffer, FakeRedisBuffer)


class WorkflowValidationTests(unittest.TestCase):
    def test_graph_workflow_rejects_cycles(self):
        agent_a = make_stub_agent("A")
        agent_b = make_stub_agent("B")

        with self.assertRaises(ValueError):
            GraphWorkflow([agent_a, agent_b], {"A": ["B"], "B": ["A"]})

    def test_graph_workflow_rejects_unknown_dependencies(self):
        agent_a = make_stub_agent("A")
        agent_b = make_stub_agent("B")

        with self.assertRaises(ValueError):
            GraphWorkflow([agent_a, agent_b], {"B": ["missing"]})


class WorkflowAutoTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_auto_assembles_metadata_from_generated_specs(self):
        workflow = Workflow(name="auto-test")

        agents_list = AgentsList(
            agents=[
                AgentSpec(name="researcher", system_prompt="Research specialist"),
                AgentSpec(name="writer", system_prompt="Writing specialist"),
            ],
            reasoning="Two agents are enough for this task.",
        )
        dependencies_spec = DependenciesSpec(
            agent_dependencies=[
                AgentDependency(agent_name="researcher", depends_on=[]),
                AgentDependency(agent_name="writer", depends_on=["researcher"]),
            ],
            explanation="writer depends on researcher",
        )
        final_result = WorkflowResult(
            result="done",
            execution_time=0.3,
            pattern=WorkflowPatternType.GRAPH,
            metadata={"final_agents": ["writer"]},
        )

        workflow.run_parallel = AsyncMock(
            side_effect=[
                SimpleNamespace(result=agents_list, execution_time=0.1),
                SimpleNamespace(result=dependencies_spec, execution_time=0.2),
            ]
        )
        workflow.run_graph = AsyncMock(return_value=final_result)

        class FakeAgent:
            def __init__(self, name, system_prompt, **kwargs):
                self.name = name
                self.system_prompt = system_prompt

        with patch("xagent.multi.workflow.Agent", FakeAgent):
            result = await workflow.run_auto(task="produce a short report")

        self.assertEqual(result.result, "done")
        self.assertTrue(result.metadata["auto_workflow"])
        self.assertEqual(result.metadata["agent_count"], 2)
        self.assertEqual(
            result.metadata["generated_dependencies"],
            {"writer": ["researcher"]},
        )

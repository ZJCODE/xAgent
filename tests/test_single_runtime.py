from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx

from xagent.components.message.sqlite_messages import MessageStorage
from xagent.core.runtime import (
    AgentEvent,
    AgentRuntime,
    ChannelManager,
    Delivery,
    DeliveryDispatcher,
    ManagedChannel,
    RuntimeBacklogFull,
    RuntimeControlServer,
    RuntimeAgentProxy,
    RuntimeLease,
    RuntimeStateStore,
    RuntimeTaskScheduler,
    RuntimeTaskStore,
)
from xagent.schemas import Message, RoleType
from xagent.schemas.message import ImageContent
from xagent.core.runtime.scheduling import normalize_schedule
from xagent.core.runtime.application import RuntimeFactory
from xagent.settings import XAgentSettings
from xagent.core.runtime.types import (
    DELIVERY_STATUS_BLOCKED,
    DELIVERY_STATUS_DELIVERED,
    DELIVERY_STATUS_SENDING,
    DELIVERY_STATUS_UNKNOWN,
    EVENT_STATUS_COMPLETED,
    EVENT_STATUS_NEEDS_REVIEW,
    EVENT_STATUS_PENDING,
    EVENT_STATUS_PROCESSING,
)
from xagent.tools.shell_tool import _read_stream_limited, create_workspace_run_command_tool


class _FakeAgent:
    def __init__(self) -> None:
        self.seen: list[str] = []
        self.tool_executor = SimpleNamespace(before_execute=None)

    async def chat_events(self, **kwargs):
        content = kwargs["user_message"]
        self.seen.append(content)
        await asyncio.sleep(0)
        yield {
            "type": "message_done",
            "phase": "final",
            "content": f"reply:{content}",
        }
        yield {"type": "done"}


class RuntimeStateStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_desktop_message_archive_is_filtered_paginated_and_sanitized(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite3"
            store = RuntimeStateStore(path)
            messages = MessageStorage(str(path))
            await messages.add_messages(
                [
                    Message(
                        role=RoleType.USER,
                        sender_id="alice",
                        source="web",
                        content="Budget is exactly 100% today",
                        images=[
                            ImageContent(
                                format="png",
                                source="data:image/png;base64,secret",
                            )
                        ],
                        metadata={
                            "safe": "visible",
                            "count": 2,
                            "nested": {"secret": True},
                            "long": "x" * 501,
                        },
                    ),
                    Message(
                        role=RoleType.ASSISTANT,
                        source="api",
                        content="Acknowledged",
                    ),
                ]
            )

            first_page = await store.list_messages(limit=1)
            match = await store.list_messages(query="100%", role="user", source="web")

            self.assertEqual(first_page["total"], 2)
            self.assertTrue(first_page["has_more"])
            self.assertEqual(first_page["messages"][0]["content"], "Acknowledged")
            self.assertEqual(match["total"], 1)
            self.assertEqual(match["messages"][0]["source"], "web")
            self.assertEqual(match["messages"][0]["images"], [{"format": "png"}])
            self.assertEqual(
                match["messages"][0]["metadata"],
                {"safe": "visible", "count": 2},
            )

    async def test_desktop_overview_uses_small_aggregates_not_full_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite3"
            store = RuntimeStateStore(path)
            messages = MessageStorage(str(path))
            await messages.add_messages(Message(content="hello", source="web"))
            await store.resolve_person("web", "alice")
            event = AgentEvent.create(
                kind="chat",
                source="web",
                speaker_id="alice",
                content="recent event",
            )
            sequence, _ = await store.enqueue_event(event)

            overview = await store.overview()

            self.assertEqual(overview["counts"]["messages"], 1)
            self.assertEqual(overview["counts"]["people"], 2)
            self.assertEqual(overview["counts"]["events"]["pending"], 1)
            self.assertEqual(overview["recent_events"][0]["sequence"], sequence)
            self.assertEqual(overview["recent_events"][0]["content"], "recent event")

    async def test_events_are_claimed_in_persisted_sequence_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RuntimeStateStore(Path(tmp) / "state.sqlite3")
            first = AgentEvent.create(kind="chat", source="api", speaker_id="a", content="one")
            second = AgentEvent.create(kind="chat", source="feishu", speaker_id="b", content="two")
            first_sequence, _ = await store.enqueue_event(first)
            second_sequence, _ = await store.enqueue_event(second)

            claimed_first = await store.claim_next_event()
            claimed_second = await store.claim_next_event()

            self.assertLess(first_sequence, second_sequence)
            self.assertEqual(claimed_first.event.event_id, first.event_id)
            self.assertEqual(claimed_second.event.event_id, second.event_id)

    async def test_duplicate_event_id_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RuntimeStateStore(Path(tmp) / "state.sqlite3")
            event = AgentEvent.create(kind="chat", source="api", speaker_id="a", content="one")
            first = await store.enqueue_event(event)
            second = await store.enqueue_event(event)
            self.assertEqual(first[0], second[0])
            self.assertTrue(first[1])
            self.assertFalse(second[1])

    async def test_duplicate_event_id_rejects_a_different_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RuntimeStateStore(Path(tmp) / "state.sqlite3")
            original = AgentEvent.create(
                event_id="same",
                kind="chat",
                source="api",
                speaker_id="a",
                content="one",
            )
            await store.enqueue_event(original)

            with self.assertRaisesRegex(ValueError, "event_id collision"):
                await store.enqueue_event(
                    AgentEvent.create(
                        event_id="same",
                        kind="chat",
                        source="api",
                        speaker_id="a",
                        content="different",
                        timestamp=original.timestamp,
                    )
                )

    async def test_source_backlog_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RuntimeStateStore(Path(tmp) / "state.sqlite3", max_pending_per_source=1)
            await store.enqueue_event(
                AgentEvent.create(kind="chat", source="api", speaker_id="a", content="one")
            )
            with self.assertRaises(RuntimeBacklogFull):
                await store.enqueue_event(
                    AgentEvent.create(kind="chat", source="api", speaker_id="b", content="two")
                )

    async def test_recovery_requeues_safe_work_and_quarantines_side_effects(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RuntimeStateStore(Path(tmp) / "state.sqlite3")
            safe = AgentEvent.create(kind="chat", source="api", speaker_id="a", content="safe")
            unsafe = AgentEvent.create(kind="chat", source="api", speaker_id="b", content="unsafe")
            await store.enqueue_event(safe)
            await store.enqueue_event(unsafe)
            await store.claim_next_event()
            await store.claim_next_event()
            await store.mark_side_effect_started(unsafe.event_id)

            retryable, needs_review = await store.recover_interrupted()

            self.assertEqual((retryable, needs_review), (1, 1))
            self.assertEqual((await store.get_event(safe.event_id)).status, EVENT_STATUS_PENDING)
            self.assertEqual(
                (await store.get_event(unsafe.event_id)).status,
                EVENT_STATUS_NEEDS_REVIEW,
            )

    async def test_unknown_schema_version_is_rejected_without_rewrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite3"
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "CREATE TABLE runtime_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO runtime_meta(key, value) VALUES('schema_version', '999')"
                )
                connection.commit()
            with self.assertRaisesRegex(RuntimeError, "Unsupported runtime schema version"):
                RuntimeStateStore(path)

    async def test_unknown_runtime_structure_is_rejected_without_rewrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite3"
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "CREATE TABLE runtime_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO runtime_meta(key, value) VALUES('schema_version', '1')"
                )
                connection.execute("CREATE TABLE deliveries(wrong_column TEXT)")
                connection.commit()
            with self.assertRaisesRegex(RuntimeError, "Unsupported .* schema"):
                RuntimeStateStore(path)
            with sqlite3.connect(path) as connection:
                columns = [
                    row[1] for row in connection.execute("PRAGMA table_info(deliveries)")
                ]
            self.assertEqual(columns, ["wrong_column"])

    async def test_malformed_schema_metadata_fails_cleanly_without_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite3"
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE runtime_meta(wrong_column TEXT)")
                connection.commit()
            with self.assertRaisesRegex(RuntimeError, "Unsupported runtime schema structure"):
                RuntimeStateStore(path)
            with sqlite3.connect(path) as connection:
                columns = [
                    row[1] for row in connection.execute("PRAGMA table_info(runtime_meta)")
                ]
            self.assertEqual(columns, ["wrong_column"])

    async def test_interrupted_send_is_never_automatically_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite3"
            state = RuntimeStateStore(path)
            delivery = Delivery.create(
                event_id="event",
                channel="api",
                target={"user_id": "alice"},
                payload={"content": "hello"},
            )
            await state.add_delivery(delivery)
            await state.update_delivery(
                delivery.delivery_id,
                status=DELIVERY_STATUS_SENDING,
                increment_attempts=True,
            )

            reopened = RuntimeStateStore(path)
            recovered = (await reopened.list_deliveries())[0]

            self.assertEqual(recovered.status, DELIVERY_STATUS_UNKNOWN)
            self.assertEqual(recovered.attempts, 1)

    def test_delivery_rejects_control_surfaces_as_channels(self):
        for invalid in ("web", "cli", "scheduler", "internal"):
            with self.subTest(channel=invalid), self.assertRaisesRegex(
                ValueError,
                "delivery channel",
            ):
                Delivery.create(
                    event_id="event",
                    channel=invalid,
                    target={},
                    payload={"content": "hello"},
                )


class AgentRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_submitters_share_one_fifo_actor(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = _FakeAgent()
            store = RuntimeStateStore(Path(tmp) / "state.sqlite3")
            runtime = AgentRuntime(agent, store)
            await runtime.start()
            events = [
                AgentEvent.create(
                    kind="chat",
                    source=channel,
                    speaker_id=f"person-{index}",
                    content=f"message-{index}",
                )
                for index, channel in enumerate(("api", "feishu", "weixin"))
            ]
            try:
                results = await asyncio.gather(
                    *(runtime.submit_and_wait(event) for event in events)
                )
            finally:
                await runtime.stop()

            persisted = await store.list_events()
            self.assertEqual(
                agent.seen,
                [stored.event.content for stored in persisted],
            )
            self.assertTrue(all(result["kind"] == "chat" for result in results))
            for event in events:
                self.assertEqual(
                    (await store.get_event(event.event_id)).status,
                    EVENT_STATUS_COMPLETED,
                )

    async def test_stream_exposes_model_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = RuntimeStateStore(Path(tmp) / "state.sqlite3")
            runtime = AgentRuntime(
                _FakeAgent(),
                state,
            )
            event = AgentEvent.create(
                kind="chat",
                source="api",
                speaker_id="person",
                content="hello",
            )
            await runtime.start()
            try:
                items = [item async for item in runtime.stream(event)]
            finally:
                await runtime.stop()
            self.assertEqual(items[-1], {"type": "done"})
            self.assertEqual(items[0]["content"], "reply:hello")
            self.assertTrue((await state.get_event(event.event_id)).side_effect_started)

    async def test_store_marks_claimed_event_processing(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RuntimeStateStore(Path(tmp) / "state.sqlite3")
            event = AgentEvent.create(
                kind="chat",
                source="api",
                speaker_id="person",
                content="hello",
            )
            await store.enqueue_event(event)
            claimed = await store.claim_next_event()
            self.assertEqual(claimed.status, EVENT_STATUS_PROCESSING)


class ChannelManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_channel_failure_does_not_cancel_another(self):
        persisted: list[tuple[str, bool]] = []
        healthy_stopped = asyncio.Event()

        async def persist(name: str, enabled: bool) -> None:
            persisted.append((name, enabled))

        async def failing_factory() -> ManagedChannel:
            async def run() -> None:
                raise RuntimeError("broken transport")

            return ManagedChannel(run=run, stop=lambda: None)

        async def healthy_factory() -> ManagedChannel:
            async def run() -> None:
                await healthy_stopped.wait()

            async def stop() -> None:
                healthy_stopped.set()

            return ManagedChannel(run=run, stop=stop)

        manager = ChannelManager(
            persist_enabled=persist,
            restart_delay_seconds=0.01,
        )
        manager.register("broken", failing_factory)
        manager.register("healthy", healthy_factory)
        await manager.start("broken")
        await manager.start("healthy")
        await asyncio.sleep(0.03)
        snapshot = {item["name"]: item for item in manager.snapshot()}
        self.assertEqual(snapshot["healthy"]["state"], "running")
        self.assertIn(snapshot["broken"]["state"], {"starting", "degraded"})
        await manager.stop_all()
        self.assertEqual(persisted, [("broken", True), ("healthy", True)])

    async def test_channel_stop_is_persistent_but_runtime_shutdown_is_not(self):
        persisted: list[tuple[str, bool]] = []
        stopped = asyncio.Event()

        async def factory() -> ManagedChannel:
            async def run() -> None:
                await stopped.wait()

            async def stop() -> None:
                stopped.set()

            return ManagedChannel(run=run, stop=stop)

        manager = ChannelManager(
            persist_enabled=lambda name, enabled: persisted.append((name, enabled))
        )
        manager.register("api", factory)
        await manager.start("api")
        await manager.stop("api")
        self.assertEqual(persisted, [("api", True), ("api", False)])

    async def test_channel_factory_reads_credentials_when_hot_started(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            initial = XAgentSettings.model_validate(
                {
                    "schema_version": 2,
                    "provider": {
                        "name": "openai",
                        "model": "test-model",
                        "api_key": "key",
                    },
                    "channels": {"feishu": {"enabled": False}},
                }
            )
            initial.write_atomic(root / "config.yaml")
            manager = ChannelManager()
            state = SimpleNamespace(add_delivery=lambda delivery: None)
            RuntimeFactory(root)._register_channels(
                manager,
                initial,
                SimpleNamespace(),
                state,
            )

            updated_data = initial.model_dump(mode="python")
            updated_data["channels"]["feishu"].update(
                {"app_id": "cli_hot_app", "app_secret": "hot-secret"}
            )
            XAgentSettings.model_validate(updated_data).write_atomic(
                root / "config.yaml"
            )

            captured: dict[str, object] = {}

            class FakeAdapter:
                def __init__(self, agent, config, **kwargs):
                    captured["config"] = config

                async def run(self):
                    return None

                async def stop(self):
                    return None

                async def send(self, delivery):
                    return None

            from unittest.mock import patch

            with patch(
                "xagent.integrations.feishu.FeishuAdapter",
                FakeAdapter,
            ):
                managed = manager._slots["feishu"].factory()

            self.assertIsNotNone(managed)
            self.assertEqual(captured["config"].app_id, "cli_hot_app")
            self.assertEqual(captured["config"].app_secret, "hot-secret")

    async def test_disabled_delivery_stays_blocked_until_manual_retry(self):
        sent: list[str] = []
        stop = asyncio.Event()

        async def factory() -> ManagedChannel:
            async def run() -> None:
                await stop.wait()

            async def send(delivery: Delivery) -> None:
                sent.append(delivery.delivery_id)

            return ManagedChannel(run=run, stop=stop.set, send=send)

        with tempfile.TemporaryDirectory() as tmp:
            state = RuntimeStateStore(Path(tmp) / "state.sqlite3")
            manager = ChannelManager(restart_delay_seconds=0.01)
            manager.register("api", factory, enabled=False)
            dispatcher = DeliveryDispatcher(state, manager, poll_seconds=0.01)
            delivery = Delivery.create(
                event_id="event",
                channel="api",
                target={"user_id": "alice"},
                payload={"content": "later"},
            )
            await state.add_delivery(delivery)
            await dispatcher.start()
            await asyncio.sleep(0.04)
            blocked = (await state.list_deliveries())[0]
            self.assertEqual(blocked.status, DELIVERY_STATUS_BLOCKED)
            self.assertEqual(sent, [])

            await manager.start("api")
            await asyncio.sleep(0.02)
            await state.retry_blocked_delivery(delivery.delivery_id)
            await asyncio.sleep(0.04)
            delivered = (await state.list_deliveries())[0]
            self.assertEqual(delivered.status, DELIVERY_STATUS_DELIVERED)
            self.assertEqual(sent, [delivery.delivery_id])
            await dispatcher.stop()
            await manager.stop_all()

    async def test_reply_delivery_waits_for_source_event_commit(self):
        sent: list[str] = []
        stop = asyncio.Event()

        async def factory() -> ManagedChannel:
            async def send(delivery: Delivery) -> None:
                sent.append(delivery.delivery_id)

            return ManagedChannel(run=stop.wait, stop=stop.set, send=send)

        with tempfile.TemporaryDirectory() as tmp:
            state = RuntimeStateStore(Path(tmp) / "state.sqlite3")
            event = AgentEvent.create(
                event_id="source-event",
                kind="chat",
                source="feishu",
                speaker_id="person",
                content="hello",
            )
            await state.enqueue_event(event)
            await state.claim_next_event()
            delivery = Delivery.create(
                delivery_id="stable-reply",
                event_id=event.event_id,
                channel="feishu",
                target={"chat_id": "room"},
                payload={"content": "reply"},
            )
            await state.add_delivery(delivery)
            await state.add_delivery(delivery)

            manager = ChannelManager(restart_delay_seconds=0.01)
            manager.register("feishu", factory, enabled=True)
            await manager.start_enabled()
            dispatcher = DeliveryDispatcher(state, manager, poll_seconds=0.01)
            await dispatcher.start()
            await asyncio.sleep(0.04)
            self.assertEqual(sent, [])

            await state.complete_event(event.event_id, {"events": []})
            await asyncio.sleep(0.04)
            self.assertEqual(sent, [delivery.delivery_id])
            self.assertEqual(len(await state.list_deliveries()), 1)
            await dispatcher.stop()
            await manager.stop_all()


class RuntimeSchedulingTests(unittest.IsolatedAsyncioTestCase):
    def test_only_supported_schedule_shapes_are_accepted(self):
        future = (datetime.now().astimezone() + timedelta(hours=1)).isoformat()
        once, once_at = normalize_schedule({"kind": "once", "run_at": future})
        daily, daily_at = normalize_schedule({"kind": "daily", "local_time": "09:30"})
        weekly, weekly_at = normalize_schedule(
            {"kind": "weekly", "weekday": 2, "local_time": "10:00"}
        )
        interval, interval_at = normalize_schedule(
            {"kind": "interval", "interval_seconds": 60, "duration_seconds": 3600}
        )
        self.assertEqual(once["kind"], "once")
        self.assertGreater(once_at, time.time())
        self.assertGreater(daily_at, time.time())
        self.assertGreater(weekly_at, time.time())
        self.assertGreater(interval["end_timestamp"], interval_at)
        with self.assertRaisesRegex(ValueError, "requires end_at or duration_seconds"):
            normalize_schedule({"kind": "interval", "interval_seconds": 60})

    def test_resuming_interval_keeps_its_original_end(self):
        start = time.time()
        schedule, first_run = normalize_schedule(
            {
                "kind": "interval",
                "interval_seconds": 60,
                "duration_seconds": 3600,
            },
            after=start,
        )
        resumed, resumed_run = normalize_schedule(schedule, after=start + 600)
        self.assertEqual(resumed["end_timestamp"], schedule["end_timestamp"])
        self.assertEqual(resumed_run, start + 660)
        self.assertEqual(first_run, start + 60)

    async def test_due_agent_task_with_destination_creates_durable_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = RuntimeStateStore(Path(tmp) / "state.sqlite3")
            runtime = AgentRuntime(_FakeAgent(), state)
            store = RuntimeTaskStore(state)
            scheduler = RuntimeTaskScheduler(store, runtime, state)
            await runtime.start()
            task = await store.create(
                instruction="remember",
                schedule={
                    "kind": "once",
                    "run_at": (
                        datetime.now().astimezone() + timedelta(milliseconds=80)
                    ).isoformat(),
                },
                destination={
                    "channel": "api",
                    "target": {"user_id": "alice"},
                },
                created_source="web",
                created_by="owner",
            )
            await scheduler.start()
            await asyncio.sleep(0.7)
            await scheduler.stop()
            await runtime.stop()
            tasks = await store.list()
            deliveries = await state.list_deliveries()
            self.assertEqual(tasks[0].task_id, task.task_id)
            self.assertEqual(tasks[0].status, "completed")
            self.assertIn("remember", deliveries[0].payload["content"])
            self.assertEqual(deliveries[0].channel, "api")

    async def test_task_without_destination_stays_on_the_agent_timeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = RuntimeStateStore(Path(tmp) / "state.sqlite3")
            runtime = AgentRuntime(_FakeAgent(), state)
            store = RuntimeTaskStore(state)
            scheduler = RuntimeTaskScheduler(store, runtime, state)
            await runtime.start()
            task = await store.create(
                instruction="review today",
                schedule={
                    "kind": "once",
                    "run_at": (
                        datetime.now().astimezone() + timedelta(milliseconds=80)
                    ).isoformat(),
                },
                destination=None,
                created_source="web",
                created_by="owner",
            )
            await scheduler.start()
            await asyncio.sleep(0.7)
            await scheduler.stop()
            await runtime.stop()

            tasks = await store.list()
            events = await state.list_events()
            deliveries = await state.list_deliveries()
            self.assertEqual(tasks[0].task_id, task.task_id)
            self.assertEqual(tasks[0].status, "completed")
            self.assertEqual(events[0].event.source, "scheduler")
            self.assertEqual(deliveries, [])

    async def test_proxy_preserves_explicit_group_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = RuntimeStateStore(Path(tmp) / "state.sqlite3")
            runtime = AgentRuntime(_FakeAgent(), state)
            proxy = RuntimeAgentProxy(runtime)
            await runtime.start()
            try:
                events = [
                    event
                    async for event in proxy.chat_events(
                        user_message="hello group",
                        user_id="feishu-account",
                        source="feishu",
                        conversation_id="feishu:oc_room",
                        audience_ids=("feishu-room:oc_room",),
                    )
                ]
            finally:
                await runtime.stop()

            stored = (await state.list_events())[0].event
            self.assertEqual(stored.conversation_id, "feishu:oc_room")
            self.assertEqual(stored.audience_ids, ("feishu-room:oc_room",))
            self.assertEqual(events[-1]["type"], "done")

    async def test_interrupted_running_task_is_failed_not_replayed(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = RuntimeStateStore(Path(tmp) / "state.sqlite3")
            store = RuntimeTaskStore(state)
            task = await store.create(
                instruction="only once",
                schedule={
                    "kind": "once",
                    "run_at": (
                        datetime.now().astimezone() + timedelta(milliseconds=30)
                    ).isoformat(),
                },
                destination={
                    "channel": "api",
                    "target": {"user_id": "alice"},
                },
                created_source="web",
                created_by="owner",
            )
            await asyncio.sleep(0.05)
            claimed = await store.claim_due()
            self.assertEqual(claimed.task_id, task.task_id)

            self.assertEqual(await store.recover_interrupted(), 1)
            recovered = (await store.list())[0]

            self.assertEqual(recovered.status, "failed")
            self.assertIn("interrupted", recovered.error)


class ShellBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_shell_only_runs_read_only_commands_inside_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "note.txt").write_text("hello", encoding="utf-8")
            tool = create_workspace_run_command_tool(str(workspace))

            allowed = await tool("wc -c note.txt")
            write_attempt = await tool("rm note.txt")
            escape_attempt = await tool("head -1 /etc/passwd")

            self.assertEqual(allowed["return_code"], 0)
            self.assertIn("5", allowed["stdout"])
            self.assertEqual(write_attempt["return_code"], -1)
            self.assertIn("read-only allowlist", write_attempt["stderr"])
            self.assertEqual(escape_attempt["return_code"], -1)
            self.assertIn("outside the workspace", escape_attempt["stderr"])
            self.assertTrue((workspace / "note.txt").is_file())

    async def test_shell_stream_storage_is_bounded(self):
        stream = asyncio.StreamReader()
        stream.feed_data(b"x" * 100_000)
        stream.feed_eof()

        result = await _read_stream_limited(stream, 1024)

        self.assertEqual(result.count("x"), 1024)
        self.assertTrue(result.endswith("[truncated]"))


class RuntimeControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_desktop_read_routes_expose_messages_memory_and_overview(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = RuntimeStateStore(root / "state.sqlite3")
            messages = MessageStorage(str(root / "state.sqlite3"))
            await messages.add_messages(
                Message(
                    role=RoleType.USER,
                    source="web",
                    content="hello desktop",
                )
            )
            diary = root / "memory" / "daily" / "2026-07-26.md"
            diary.parent.mkdir(parents=True)
            diary.write_text(
                "# A good day\n\nThe desktop project became simpler.",
                encoding="utf-8",
            )
            runtime = AgentRuntime(_FakeAgent(), state)
            control = RuntimeControlServer(
                runtime=runtime,
                channels=ChannelManager(),
                state=state,
                tasks=RuntimeTaskStore(state),
                runtime_dir=root,
            )
            headers = {"Authorization": f"Bearer {control.token}"}
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=control.app),
                base_url="http://runtime",
                headers=headers,
            ) as client:
                archive = await client.get("/v1/messages", params={"q": "desktop"})
                memory = await client.get(
                    "/v1/memory",
                    params={"scope": "daily", "q": "simpler"},
                )
                document = await client.get(
                    "/v1/memory/file",
                    params={"path": "daily/2026-07-26.md"},
                )
                overview = await client.get("/v1/overview")
                traversal = await client.get(
                    "/v1/memory/file",
                    params={"path": "../identity.md"},
                )

            self.assertEqual(archive.status_code, 200)
            self.assertEqual(archive.json()["messages"][0]["content"], "hello desktop")
            self.assertEqual(memory.status_code, 200)
            self.assertEqual(memory.json()["entries"][0]["title"], "A good day")
            self.assertEqual(document.status_code, 200)
            self.assertIn("desktop project", document.json()["content"])
            self.assertEqual(overview.status_code, 200)
            self.assertEqual(overview.json()["counts"]["memory_files"], 1)
            self.assertEqual(traversal.status_code, 400)

    async def test_control_routes_require_token_and_submit_through_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = RuntimeStateStore(Path(tmp) / "state.sqlite3")
            runtime = AgentRuntime(_FakeAgent(), state)
            channels = ChannelManager()
            control = RuntimeControlServer(
                runtime=runtime,
                channels=channels,
                state=state,
                tasks=RuntimeTaskStore(state),
                runtime_dir=tmp,
            )
            transport = httpx.ASGITransport(app=control.app)
            await runtime.start()
            try:
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://runtime",
                ) as client:
                    denied = await client.get("/v1/runtime")
                    accepted = await client.get(
                        "/v1/runtime",
                        headers={"Authorization": f"Bearer {control.token}"},
                    )
                    submitted = await client.post(
                        "/v1/events",
                        headers={"Authorization": f"Bearer {control.token}"},
                        json={
                            "kind": "chat",
                            "source": "cli",
                            "speaker_id": "person",
                            "content": "hello",
                        },
                    )
            finally:
                await runtime.stop()
            self.assertEqual(denied.status_code, 401)
            self.assertEqual(accepted.status_code, 200)
            self.assertEqual(submitted.status_code, 200)
            self.assertEqual(
                submitted.json()["result"]["events"][0]["content"],
                "reply:hello",
            )
            stored = (await state.list_events())[0].event
            self.assertEqual(stored.source, "cli")
            self.assertEqual(stored.speaker_id, "owner")
            self.assertEqual(stored.audience_ids, ("owner",))

    async def test_runtime_lease_rejects_a_second_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime.lock"
            first = RuntimeLease(path)
            second = RuntimeLease(path)
            first.acquire()
            try:
                with self.assertRaisesRegex(RuntimeError, "already owns"):
                    second.acquire()
            finally:
                first.release()

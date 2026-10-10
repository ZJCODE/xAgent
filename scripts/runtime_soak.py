"""Accelerated 24-hour scenario with deterministic models, channels and scheduler time.

Run from the checkout: python scripts/runtime_soak.py --root /tmp/xagent-soak
The default scenario advances 24 virtual scheduler hours in less than two minutes.
No model provider or external bot is contacted; elapsed-time stability is not inferred.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import resource
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml
from xagent.core.handlers.model import ModelStreamEvent
from xagent.core.runtime.client import RuntimeClient
from xagent.core.runtime.host import RuntimeHost
from xagent.core.runtime.tasks import enqueue_scheduled_task, AsyncTaskScheduler
from xagent.interfaces.base import BaseAgentRunner
# Import the subclass before temporarily replacing its bootstrap dependency.
from xagent.interfaces.server.admin_service import AdminService


class Model:
    def __init__(self):
        self.calls = 0
        self.active = 0
        self.max_active = 0

    async def model_turn_events(self, **kwargs):
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.005)
            yield ModelStreamEvent(type="delta", delta="I received this message.")
        finally:
            self.active -= 1


class Journal:
    async def format_diary_entry(self, messages, journal_date, existing_today=""):
        identifiers = [str(row.get("content", "")) for row in messages if not row.get("already_journaled")]
        return "I experienced these events: " + " | ".join(identifiers)

    async def update_relationship_cards(self, **kwargs):
        return []

    async def distill_notes(self, **kwargs):
        return []

    async def generate_summary(self, source_content, period_type, period_label):
        return f"I reviewed my {period_type} experiences for {period_label}."


class Channel:
    def __init__(self, host, name):
        self.host, self.name = host, name
        self.agent = host.agent
        self.stopping = asyncio.Event()

    async def run(self):
        self.host._channel_connection(self.name, "connected")
        await self.stopping.wait()

    async def stop(self):
        self.stopping.set()

    def _can_handle_scheduled_task(self, task):
        return task.delivery_channel == self.name

    async def execute_scheduled_task(self, task):
        return await self.host.api.tasks.execute(task)

    async def deliver_scheduled_task(self, task, result, run_id):
        # API's durable session-history acknowledgement also gives the fake
        # channels a deterministic, inspectable delivery receipt.
        return await self.host.api.tasks.deliver(task, result, run_id)


class Host(RuntimeHost):
    async def _start_channel(self, channel):
        adapter = Channel(self, channel)
        self.adapters[channel] = adapter
        self._tasks[channel] = asyncio.create_task(self._watch_channel(channel, adapter.run()))
        await asyncio.sleep(0)


def write_report(root, payload):
    temporary = root / ".soak-report.tmp"
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(root / "soak-report.json")


async def soak(args):
    root = args.root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    if (root / "messages").exists():
        raise ValueError("Use a fresh soak directory so counts can be verified.")
    (root / "config.yaml").write_text(yaml.safe_dump({
        "provider": {"name": "openai", "model": "soak-model", "api_key": "test-key"},
        "channels": {"api": {"enabled": False}},
        "agent": {"notes_enabled": False, "subconscious_activity": 0, "diary_write_batch": 16},
        "runtime": {"heartbeat_enabled": True, "heartbeat_interval_seconds": 5},
    }), encoding="utf-8")
    (root / "identity.md").write_text("I am the deterministic soak agent.", encoding="utf-8")
    (root / "soak.pid").write_text(str(os.getpid()), encoding="utf-8")
    model = Model()
    def bootstrap(config_dir):
        runner = BaseAgentRunner(config_dir=config_dir)
        runner.agent.model_client = model
        runner.agent.working_context_compactor = None
        runner.agent.memory_handler.llm_service = Journal()
        runner.agent.memory_handler.relationship_store = None
        return runner
    host = Host(root, channels=["api", "feishu", "weixin", "voice"])
    clock = datetime(2026, 10, 10)
    def scheduler(*positional, **kwargs):
        return AsyncTaskScheduler(*positional, **kwargs, now_provider=lambda: clock)
    started = time.monotonic()
    report = {"state": "starting", "pid": os.getpid(), "started_at": datetime.now().isoformat(),
              "duration_seconds": args.duration_seconds, "cycles": 0, "checks": 0}
    write_report(root, report)
    with patch("xagent.interfaces.base.BaseAgentRunner", side_effect=bootstrap), patch(
        "xagent.core.runtime.AsyncTaskScheduler", side_effect=scheduler
    ):
        running = asyncio.create_task(host.run())
        try:
            while not host.runtime_ready:
                if running.done():
                    await running
                await asyncio.sleep(0.01)
            client = RuntimeClient(root)
            report["initial_fd_count"] = len(list(Path("/dev/fd").iterdir()))
            async def chat(channel, event_id):
                events = [event async for event in client.chat_events(
                    channel=channel, user_id=f"{channel}-user", user_message=event_id,
                    event_id=event_id, request_id=f"request-{event_id}")]
                assert any(event.get("type") == "message_done" for event in events), events
                return events
            host.scheduler.now_provider = lambda: clock
            # One recurring occurrence per virtual hour; dispatch remains real.
            enqueue_scheduled_task(tasks_dir=host.runner.tasks_dir, task_type="message",
                content="hourly reminder", run_at=clock, channel="api", target={"user_id": "api-user"},
                recurrence=[{"kind": "interval", "every_seconds": 3600,
                    "start_at": clock.isoformat(sep=" "),
                    "end_at": (clock + timedelta(hours=args.simulated_hours)).isoformat(sep=" ")}])
            for hour in range(args.simulated_hours):
                if time.monotonic() - started > args.duration_seconds:
                    raise TimeoutError("Accelerated scenario exceeded its wall-clock budget")
                for burst in range(args.bursts_per_hour):
                    cycle = report["cycles"]
                    calls_before = model.calls
                    await asyncio.gather(*(chat(channel, f"{channel}-{hour}-{burst}") for channel in host.channel_status))
                    assert model.calls == calls_before + 4
                    await chat("api", f"api-{hour}-{burst}")
                    assert model.calls == calls_before + 4, "duplicate input executed again"
                    await client.observe(context=f"observation-{cycle}", source="soak", event_id=f"observation-{cycle}")
                    await client.observe(context=f"observation-{cycle}", source="soak", event_id=f"observation-{cycle}")
                    host._channel_connection("feishu", "reconnecting")
                    status = await client.status()
                    assert status["state"] == "degraded"
                    host._channel_connection("feishu", "connected")
                    assert model.max_active == 1, "formal turns overlapped"
                    assert all(adapter.agent is host.agent for adapter in host.adapters.values())
                    assert host.scheduler.is_running
                    report.update(state="running", cycles=cycle+1, checks=report["checks"]+9,
                        virtual_hour=hour, elapsed_seconds=round(time.monotonic()-started, 2), model_calls=model.calls,
                        max_active_turns=model.max_active, max_rss=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                        fd_count=len(list(Path("/dev/fd").iterdir())), last_status=status,
                        updated_at=datetime.now().isoformat())
                    write_report(root, report)
                await host.scheduler.tick()
                await host.agent.memory_handler.run_maintenance(force=True, trigger="simulation-hour")
                assert (await host.agent.memory_handler.get_maintenance_status())["backlog"] == 0
                report["checks"] += 1
                if hour in {7, 15}:
                    calls_before_restart = model.calls
                    await host.stop()
                    await asyncio.wait_for(running, 30)
                    host = Host(root, channels=["api", "feishu", "weixin", "voice"])
                    running = asyncio.create_task(host.run())
                    while not host.runtime_ready:
                        if running.done():
                            await running
                        await asyncio.sleep(0.01)
                    client = RuntimeClient(root)
                    host.scheduler.now_provider = lambda: clock
                    assert model.calls == calls_before_restart, "ordinary input replayed at restart"
                    # Previously completed inputs dedupe after process-state reset.
                    await chat("api", f"api-{hour}-0")
                    assert model.calls == calls_before_restart
                    report["restarts"] = report.get("restarts", 0) + 1
                    report["checks"] += 2
                clock += timedelta(hours=1)
            report["simulated_hours"] = args.simulated_hours
            receipts = [json.loads(path.read_text()) for path in (root / ".runtime" / "task_runs").glob("*.json")]
            report["recurring_deliveries"] = len(receipts)
            assert report["recurring_deliveries"] == args.simulated_hours, report
            assert all(receipt["stage"] == "succeeded" for receipt in receipts), receipts
            report["stored_messages"] = await host.agent.message_storage.get_message_count()
            assert report["stored_messages"] == report["cycles"] * 9 + args.simulated_hours, report
            report["final_fd_count"] = len(list(Path("/dev/fd").iterdir()))
            assert report["final_fd_count"] <= report["initial_fd_count"] + 8, "file handles accumulated"
            diary = "\n".join(path.read_text() for path in (root / "memory" / "daily").rglob("*.md"))
            import re
            markers = re.findall(r"(?:api|voice|weixin|feishu)-\d+-\d+|observation-\d+", diary)
            assert len(markers) == len(set(markers)) == report["cycles"] * 5, "diary duplicated or skipped input"
            report["diary_unique_inputs"] = len(markers)
            report["checks"] += 4
            report["state"] = "passed"
        except BaseException as exc:
            report.update(state="failed", error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            try:
                await host.stop()
                await asyncio.wait_for(running, 30)
                report["journal"] = await host.agent.memory_handler.get_maintenance_status() if host.agent else None
            except BaseException as exc:
                report.update(state="failed", shutdown_error=f"{type(exc).__name__}: {exc}")
                raise
            finally:
                report.update(finished_at=datetime.now().isoformat(), elapsed_seconds=round(time.monotonic()-started, 2))
                write_report(root, report)
                (root / "soak.pid").unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--duration-seconds", type=float, default=90)
    parser.add_argument("--simulated-hours", type=int, default=24)
    parser.add_argument("--bursts-per-hour", type=int, default=8)
    arguments = parser.parse_args()
    if min(arguments.duration_seconds, arguments.simulated_hours, arguments.bursts_per_hour) <= 0:
        parser.error("Scenario limits must be positive")
    async def bounded_scenario():
        await asyncio.wait_for(soak(arguments), timeout=min(115, arguments.duration_seconds + 25))
    asyncio.run(bounded_scenario())

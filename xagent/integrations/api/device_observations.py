"""One runtime-owned worker for silent decisions on live device observations."""

import asyncio
import json

from ...tools.device_tool import create_device_tool


class DeviceObservationWorker:
    def __init__(self, agent, service, logger):
        self.agent = agent
        self.service = service
        self.logger = logger
        self.tool = create_device_tool(service)
        self.task = None
        self.pending = None
        self.generation = 0
        self.last_signature = None
        self.last_timestamps = {}
        self.last_actions = {}
        self.closed = False

    @staticmethod
    def _extract_timestamp(message):
        metadata = getattr(message, "metadata", None) or {}
        for candidate in (metadata.get("observed_at"), metadata.get("device_timestamp"), getattr(message, "timestamp", None)):
            if isinstance(candidate, (int, float)):
                return float(candidate)
        return None

    def submit(self, message):
        if self.closed:
            return
        metadata = getattr(message, "metadata", None) or {}
        device_id = metadata.get("device_id")
        observed_ts = self._extract_timestamp(message)
        if device_id and observed_ts is not None:
            previous_ts = self.last_timestamps.get(device_id)
            if previous_ts is not None and observed_ts < previous_ts:
                return
            self.last_timestamps[device_id] = observed_ts
        signature = (device_id, message.room_name,
                     metadata.get("event_type"), message.content,
                     json.dumps(metadata.get("data"), sort_keys=True, ensure_ascii=False))
        # Repeated samples during a decision are already represented in history.
        if self.task is not None and not self.task.done() and signature == self.last_signature:
            return
        self.last_signature = signature
        self.generation += 1
        self.pending = message
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run())

    async def stop(self):
        self.closed = True
        self.pending = None
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def _run(self):
        while self.pending is not None and not self.closed:
            # User turns own priority. History is rebuilt after they complete.
            if self.agent.inbox.busy or self.agent.inbox.pending_count:
                await asyncio.sleep(0.05)
                continue
            message, self.pending = self.pending, None
            generation = self.generation
            revision = getattr(self.agent, "_action_context_revision", 0)
            try:
                devices = self.service.list_devices()
                if not any(row.get("capabilities") for row in devices["devices"]):
                    continue
                if self.last_actions:
                    devices = {
                        **devices,
                        "devices": [
                            {**row, "last_action_result": self.last_actions[row["device_id"]]}
                            if row.get("device_id") in self.last_actions
                            else row
                            for row in devices["devices"]
                        ],
                    }
                arguments = await asyncio.wait_for(
                    self.agent.decide_device_action(message, devices, self.tool.tool_spec), timeout=60,
                )

                def current():
                    return (not self.closed and generation == self.generation
                            and revision == getattr(self.agent, "_action_context_revision", 0)
                            and not self.agent.inbox.busy)

                if not current():
                    if self.pending is None:
                        self.pending = message
                    continue
                if arguments is None:
                    continue
                tool = create_device_tool(self.service, before_send=current)
                result = await tool(**arguments)
                if result.get("code") == "action_context_changed" and self.pending is None:
                    self.pending = message
                target_device_id = arguments.get("device_id")
                if target_device_id and result.get("code") != "action_context_changed":
                    self.last_actions[target_device_id] = {
                        key: val
                        for key, val in {
                            "capability": arguments.get("capability"),
                            "params": arguments.get("params"),
                            "status": result.get("status"),
                            "command_id": result.get("command_id"),
                            "data": result.get("data"),
                        }.items()
                        if val is not None
                    }
                await self.agent.observe(
                    context=json.dumps({"event_type": "device_action_result", "request": arguments, "result": result}, ensure_ascii=False),
                    source="device_action", event_type="device_action_result",
                    channel="devices", room_name=message.room_name,
                    metadata={"trigger_event_key": message.metadata.get("event_key"),
                              "device_id": arguments.get("device_id")},
                )
            except Exception:
                # A failed/uncertain command is never retried by this worker.
                self.logger.exception("Device observation decision failed")

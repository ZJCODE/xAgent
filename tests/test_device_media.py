"""Device media transport, durable ownership, recovery and attachment use."""

import asyncio
import hashlib
import io
import json
import sqlite3
import tempfile
import threading
import uuid
from pathlib import Path
from contextlib import contextmanager
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, WebSocketDisconnect
from fastapi.testclient import TestClient
from PIL import Image

from xagent.integrations.devices.config import DeviceConfig
from xagent.integrations.devices.media import DeviceMediaStore, MediaError
from xagent.integrations.devices.service import AttachmentReference, CallRequest, DeviceResult, DeviceService
from xagent.interfaces.server.device_routes import register_device_routes
from xagent.schemas.attachment import normalize_attachment
from xagent.tools.device_tool import create_device_tool
from xagent.tools.see_image_tool import create_see_image_tool
from tests.test_device_execution import FakeSocket, manifest


def picture():
    output = io.BytesIO()
    Image.new("RGB", (16, 16), "red").save(output, format="PNG")
    return output.getvalue()


async def chunks(data):
    for offset in range(0, len(data), 7):
        yield data[offset:offset + 7]


def make_service(root, **media):
    service = DeviceService(DeviceConfig(media=media), root / ".devices.json", workspace=root / "workspace")
    for device in ("desk", "robot"):
        if device not in service.credentials.digests:
            service.credentials.add(device, device + "-secret")
    return service


class DeviceMediaTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.service = make_service(self.root)
        self.service.start()
        self.addAsyncCleanup(self.service.stop)
        self.socket = FakeSocket()
        self.connection = await self.service.register(self.socket, manifest())

    async def upload(self, data=b"hello", attachment_id=None, device_id="desk", **overrides):
        return await self.service.upload_media(device_id, "Bearer " + device_id + "-secret", **{
            "attachment_id": attachment_id or str(uuid.uuid4()), "file_name": "note.txt",
            "mime_type": "text/plain", "size_bytes": len(data), "sha256": None,
            "chunks": chunks(data), **overrides,
        })

    def refs(self, metadata):
        return [AttachmentReference(attachment_id=metadata["attachment_id"])]

    def rows(self):
        with sqlite3.connect(self.service.media.database) as db:
            return db.execute("SELECT state, size_bytes FROM attachments").fetchall()

    async def test_all_media_types_round_trip_and_image_is_viewable(self):
        for name, mime, data in (("photo.png", "image/png", picture()), ("voice.wav", "audio/wav", b"RIFFrecording"),
                                 ("clip.mp4", "video/mp4", b"video"), ("note.txt", "text/plain", b"hello")):
            metadata, created = await self.upload(data, file_name=name, mime_type=mime)
            self.assertTrue(created)
            self.assertEqual(metadata["sha256"], hashlib.sha256(data).hexdigest())
            attachments = await self.service.resolve_attachments(self.connection, self.refs(metadata))
            attachment, = attachments
            self.assertEqual((self.root / "workspace" / attachment["path"]).read_bytes(), data)
            self.assertEqual(normalize_attachment(attachment)["source_device_id"], "desk")
            self.assertEqual(attachment["mime_type"], mime)
            if mime.startswith("image/"):
                tool = create_see_image_tool(workspace_dir=str(self.root / "workspace"))
                self.assertEqual((await tool(path=attachment["path"]))["status"], "ok")

    async def test_replay_and_conflicts_do_not_overwrite(self):
        metadata, _ = await self.upload()
        repeat, created = await self.upload(attachment_id=metadata["attachment_id"])
        self.assertFalse(created)
        self.assertEqual(metadata, repeat)
        for overrides in ({"data": b"other"}, {"file_name": "renamed.txt"}, {"mime_type": "application/octet-stream"}):
            with self.assertRaises(MediaError) as caught:
                await self.upload(attachment_id=metadata["attachment_id"], **overrides)
            self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.rows(), [("ready", 5)])
        self.assertFalse(list(self.service.media.state_dir.glob("*.part")))

    async def test_references_survive_restart(self):
        metadata, _ = await self.upload()
        await self.service.stop()
        other = make_service(self.root)
        other.start()
        self.addAsyncCleanup(other.stop)
        connection = await other.register(FakeSocket(), manifest())
        attachments = await other.resolve_attachments(connection, self.refs(metadata))
        self.assertEqual(attachments[0]["source_resource_id"], metadata["attachment_id"])
        replay, created = await other.upload_media("desk", "Bearer desk-secret", attachment_id=metadata["attachment_id"],
            file_name="note.txt", mime_type="text/plain", size_bytes=5, sha256=None, chunks=chunks(b"hello"))
        self.assertFalse(created)
        self.assertEqual(replay, metadata)

    async def test_other_device_agent_and_new_pairing_cannot_reference(self):
        metadata, _ = await self.upload()
        robot = await self.service.register(FakeSocket(), manifest("robot"))
        with self.assertRaises(MediaError):
            await self.service.resolve_attachments(robot, self.refs(metadata))
        other = make_service(self.root / "other")
        other.start()
        self.addAsyncCleanup(other.stop)
        connection = await other.register(FakeSocket(), manifest())
        with self.assertRaises(MediaError):
            await other.resolve_attachments(connection, self.refs(metadata))
        await self.service.revoke("desk")
        self.service.credentials.add("desk", "new-secret")
        connection = await self.service.register(FakeSocket(), manifest())
        with self.assertRaises(MediaError):
            await self.service.resolve_attachments(connection, self.refs(metadata))

    async def test_failed_uploads_release_reservations_and_temporary_files(self):
        for overrides, code in (({"size_bytes": 3}, "length_mismatch"), ({"size_bytes": 10}, "length_mismatch"),
                                ({"sha256": "0" * 64}, "checksum_mismatch")):
            with self.assertRaises(MediaError) as caught:
                await self.upload(**overrides)
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(self.rows(), [])
            self.assertFalse(list(self.service.media.state_dir.glob("*.part")))
        with patch.object(self.service.media, "_append", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await self.upload()
        self.assertEqual(self.rows(), [])

    async def test_image_validation_and_size_limits(self):
        for data, overrides, status in (
            (b"fake", {"file_name": "photo.png", "mime_type": "image/png"}, 415),
            (picture()[:40], {"file_name": "photo.png", "mime_type": "image/png"}, 415),
            (picture(), {"mime_type": "text/plain"}, 415),
            (b"hello", {"size_bytes": 51 * 1024 * 1024}, 413),
            (b"hello", {"mime_type": "image/png", "size_bytes": 11 * 1024 * 1024}, 413),
        ):
            with self.assertRaises(MediaError) as caught:
                await self.upload(data, **overrides)
            self.assertEqual(caught.exception.status, status)
        with patch.object(Image, "MAX_IMAGE_PIXELS", 10):
            with self.assertRaises(MediaError):
                await self.upload(picture(), file_name="p.png", mime_type="image/png")

    async def test_path_is_server_controlled_and_image_kind_uses_content_type(self):
        metadata, _ = await self.upload(picture(), file_name="../../outside.bin", mime_type="image/png")
        attachment, = await self.service.resolve_attachments(self.connection, self.refs(metadata))
        self.assertEqual(metadata["file_name"], "outside.bin")
        self.assertEqual(attachment["kind"], "image")
        self.assertTrue(attachment["path"].endswith("content.png"))
        self.assertFalse((self.root / "outside.bin").exists())

    async def test_symlink_escape_is_rejected_and_recovery_does_not_touch_target(self):
        identifier = str(uuid.uuid4())
        directory = self.root / "workspace/assets/inbound/devices"
        directory.mkdir(parents=True)
        outside = self.root / "outside"
        outside.mkdir()
        (directory / identifier).symlink_to(outside, target_is_directory=True)
        with self.assertRaises(MediaError):
            await self.upload(attachment_id=identifier)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(self.rows(), [])

    async def test_quota_counts_reservations_and_deleted_files_release_space(self):
        self.service.config.media.device_quota_bytes = 5
        metadata, _ = await self.upload()
        with self.assertRaises(MediaError) as caught:
            await self.upload(b"x")
        self.assertEqual(caught.exception.status, 429)
        attachment, = await self.service.resolve_attachments(self.connection, self.refs(metadata))
        (self.root / "workspace" / attachment["path"]).unlink()
        await self.upload()
        self.assertEqual(self.rows(), [("ready", 5)])

    async def test_duplicate_in_progress_does_not_delete_original_reservation(self):
        self.service.config.media.max_device_uploads = 2
        started, release = asyncio.Event(), asyncio.Event()
        async def slow():
            started.set()
            await release.wait()
            yield b"hello"
        identifier = str(uuid.uuid4())
        first = asyncio.create_task(self.upload(attachment_id=identifier, chunks=slow()))
        await started.wait()
        try:
            with self.assertRaises(MediaError) as caught:
                await self.upload(attachment_id=identifier)
            self.assertEqual(caught.exception.code, "upload_in_progress")
            self.assertEqual(self.rows(), [("uploading", 5)])
        finally:
            release.set()
        await first
        self.assertEqual(self.rows(), [("ready", 5)])

    async def test_concurrency_and_quota_are_reserved_before_consuming_body(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def slow():
            started.set()
            await release.wait()
            yield b"hello"
        first = asyncio.create_task(self.upload(chunks=slow()))
        await started.wait()
        try:
            with self.assertRaises(MediaError) as caught:
                await self.upload()
            self.assertEqual(caught.exception.code, "upload_busy")
            self.service.config.media.device_quota_bytes = 5
            self.service.config.media.max_device_uploads = 2
            with self.assertRaises(MediaError) as caught:
                await self.upload(b"x")
            self.assertEqual(caught.exception.code, "media_quota")
        finally:
            release.set()
        await first

    async def test_idle_and_total_timeout_clean_up(self):
        self.service.config.media.idle_timeout_seconds = 0.02
        async def stalled():
            await asyncio.Event().wait()
            yield b"hello"
        with self.assertRaises(MediaError) as caught:
            await self.upload(chunks=stalled())
        self.assertEqual(caught.exception.status, 408)
        self.service.config.media.idle_timeout_seconds = 1
        self.service.config.media.upload_timeout_seconds = 0.025
        async def trickle():
            for _ in range(5):
                await asyncio.sleep(0.01)
                yield b"x"
        with self.assertRaises(MediaError) as caught:
            await self.upload(chunks=trickle())
        self.assertEqual(caught.exception.status, 408)
        self.assertEqual(self.rows(), [])
        self.assertFalse(self.service.uploads)

    async def test_revocation_cancels_upload_without_publishing(self):
        started = asyncio.Event()
        async def stalled():
            started.set()
            yield b"he"
            await asyncio.Event().wait()
        upload = asyncio.create_task(self.upload(chunks=stalled()))
        await started.wait()
        await self.service.revoke("desk")
        with self.assertRaises(MediaError) as caught:
            await upload
        self.assertEqual(caught.exception.status, 401)
        self.assertEqual(self.rows(), [])
        self.assertFalse(self.service.uploads)

    async def test_shutdown_drains_then_cancels_and_reopens(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def stalled():
            started.set()
            await release.wait()
            yield b"hello"
        upload = asyncio.create_task(self.upload(chunks=stalled()))
        await started.wait()
        stopping = asyncio.create_task(self.service.stop(drain_timeout=0.01))
        await stopping
        with self.assertRaises(MediaError) as caught:
            await upload
        self.assertEqual(caught.exception.status, 503)
        self.assertTrue(self.service.resources_closed)
        self.assertEqual(self.rows(), [])
        self.service.start()
        upload = asyncio.create_task(self.upload(chunks=stalled()))
        while not self.service.uploads:
            await asyncio.sleep(0)
        stopping = asyncio.create_task(self.service.stop(drain_timeout=1))
        release.set()
        await upload
        await stopping
        self.assertEqual(self.rows(), [("ready", 5)])

    async def test_client_cancel_waits_for_worker_before_cleanup(self):
        started = asyncio.Event()
        async def stalled():
            started.set()
            await asyncio.Event().wait()
            yield b"hello"
        upload = asyncio.create_task(self.upload(chunks=stalled()))
        await started.wait()
        upload.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await upload
        self.assertEqual(self.rows(), [])
        self.assertFalse(self.service.uploads)

    async def test_slow_disk_does_not_block_commands_and_cancel_waits_for_cleanup(self):
        started, release = threading.Event(), threading.Event()
        append = self.service.media._append
        def stalled(*args):
            started.set()
            release.wait(2)
            return append(*args)
        with patch.object(self.service.media, "_append", side_effect=stalled):
            upload = asyncio.create_task(self.upload())
            try:
                while not started.is_set():
                    await asyncio.sleep(0.001)
                command_task = asyncio.create_task(self.service.call(CallRequest(device_id="desk", capability="custom.execute")))
                command = await self.socket.next_command()
                self.service.resolve(self.connection, DeviceResult(type="result", command_id=command["command_id"], ok=True))
                self.assertEqual((await command_task)["status"], "success")
                upload.cancel()
                await asyncio.sleep(0.01)
                self.assertFalse(upload.done())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await upload
        self.assertEqual(self.rows(), [])
        self.assertFalse(list(self.service.media.state_dir.glob("*.part")))

    async def test_empty_chunks_do_not_reset_idle_deadline(self):
        self.service.config.media.idle_timeout_seconds = 0.02
        async def empty():
            for _ in range(8):
                await asyncio.sleep(0.005)
                yield b""
            yield b"hello"
        with self.assertRaises(MediaError) as caught:
            await self.upload(chunks=empty())
        self.assertEqual(caught.exception.status, 408)

    async def test_commit_failure_never_exposes_a_half_finished_attachment(self):
        identifier = str(uuid.uuid4())
        with patch("xagent.integrations.devices.media.os.replace", side_effect=OSError("rename failed")):
            with self.assertRaises(OSError):
                await self.upload(attachment_id=identifier)
        self.assertEqual(self.rows(), [])
        with self.assertRaises(MediaError):
            await self.service.resolve_attachments(self.connection, [AttachmentReference(attachment_id=identifier)])

    async def test_index_commit_failure_cleans_the_already_moved_file(self):
        connect = self.service.media._connect
        @contextmanager
        def failing_connection():
            with connect() as db:
                class Database:
                    def execute(self, sql, *args):
                        if sql.startswith("UPDATE attachments SET sha256"):
                            raise sqlite3.OperationalError("commit failed")
                        return db.execute(sql, *args)
                yield Database()
        identifier = str(uuid.uuid4())
        with patch.object(self.service.media, "_connect", side_effect=failing_connection):
            with self.assertRaises(sqlite3.OperationalError):
                await self.upload(attachment_id=identifier)
        self.assertEqual(self.rows(), [])
        self.assertFalse((self.root / "workspace/assets/inbound/devices" / identifier / "note.txt").exists())

    async def test_large_stream_uses_chunks_without_whole_file_reads(self):
        block = b"x" * (64 * 1024)
        async def stream():
            for _ in range(32):
                yield block
        with patch.object(Path, "read_bytes", side_effect=AssertionError("whole file read")):
            metadata, _ = await self.upload(size_bytes=32 * len(block), chunks=stream(), mime_type="application/octet-stream")
            attachment, = await self.service.resolve_attachments(self.connection, self.refs(metadata))
            self.assertEqual(attachment["size_bytes"], 2 * 1024 * 1024)

    async def test_reopening_waits_for_recovery_before_reserving_upload(self):
        await self.service.stop()
        recover = self.service.media.recover
        started, release = asyncio.Event(), asyncio.Event()
        async def slow_recovery():
            started.set()
            await release.wait()
            await recover()
        with patch.object(self.service.media, "recover", side_effect=slow_recovery):
            self.service.start()
            await started.wait()
            upload = asyncio.create_task(self.upload())
            try:
                await asyncio.sleep(0.01)
                self.assertFalse(upload.done())
            finally:
                release.set()
            await upload
        self.assertEqual(self.rows(), [("ready", 5)])

    async def test_revocation_after_capture_before_commit_prevents_publication(self):
        started, release = threading.Event(), threading.Event()
        validate = self.service.media._validate_file
        def stalled(upload):
            started.set()
            release.wait(2)
            validate(upload)
        with patch.object(self.service.media, "_validate_file", side_effect=stalled):
            upload = asyncio.create_task(self.upload())
            try:
                while not started.is_set():
                    await asyncio.sleep(0.001)
                revocation = asyncio.create_task(self.service.revoke("desk"))
                await asyncio.sleep(0.01)
            finally:
                release.set()
            await revocation
            with self.assertRaises(MediaError):
                await upload
        self.assertEqual(self.rows(), [])

    async def test_recovery_removes_crashed_upload_and_missing_ready_file(self):
        await self.service.media.prepare()
        identifier = str(uuid.uuid4())
        relative = f"assets/inbound/devices/{identifier}/crashed.txt"
        file = self.root / "workspace" / relative
        file.parent.mkdir(parents=True)
        file.write_bytes(b"crashed")
        with sqlite3.connect(self.service.media.database) as db:
            db.execute("INSERT INTO attachments VALUES (?, 'desk', 'owner', 'crashed.txt', 'text/plain', 7, ?, NULL, 'uploading')", (identifier, relative))
        (self.service.media.state_dir / "crashed.part").write_bytes(b"partial")
        store = DeviceMediaStore(self.root / "workspace", self.root, self.service.config.media)
        await store.prepare()
        self.assertFalse(file.exists())
        self.assertFalse(list(store.state_dir.glob("*.part")))
        self.assertEqual(self.rows(), [])

    async def test_attachment_errors_preserve_device_success_and_data(self):
        task = asyncio.create_task(self.service.call(CallRequest(device_id="desk", capability="custom.execute")))
        command = await self.socket.next_command()
        await self.service.resolve_result(self.connection, DeviceResult(type="result", command_id=command["command_id"], ok=True,
            data={"captured": True}, attachments=[AttachmentReference(attachment_id=uuid.uuid4())]))
        result = await task
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["data"]["captured"])
        self.assertEqual(result["attachment_error"]["code"], "invalid_attachment")

    async def test_attachment_storage_failure_preserves_device_error(self):
        task = asyncio.create_task(self.service.call(CallRequest(device_id="desk", capability="custom.execute")))
        command = await self.socket.next_command()
        with patch.object(self.service, "resolve_attachments", side_effect=sqlite3.OperationalError("storage unavailable")):
            await self.service.resolve_result(self.connection, DeviceResult(type="result", command_id=command["command_id"], ok=False,
                data={"captured": False}, error="Camera failed", attachments=[AttachmentReference(attachment_id=uuid.uuid4())]))
        result = await task
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["message"], "Camera failed")
        self.assertFalse(result["data"]["captured"])
        self.assertEqual(result["attachment_error"]["code"], "invalid_attachment")

    async def test_late_results_do_not_resolve_attachments(self):
        with patch.object(self.service, "resolve_attachments", side_effect=AssertionError("stale result resolved")):
            await self.service.resolve_result(self.connection, DeviceResult(type="result", command_id="expired", ok=True,
                attachments=[AttachmentReference(attachment_id=uuid.uuid4())]))

    async def test_attachment_count_and_total_size_limits(self):
        metadata, _ = await self.upload()
        self.service.config.media.max_message_bytes = 4
        with self.assertRaises(MediaError):
            await self.service.resolve_attachments(self.connection, self.refs(metadata))
        self.service.config.media.max_attachments = 1
        with self.assertRaises(MediaError):
            await self.service.resolve_attachments(self.connection, self.refs(metadata) * 2)

    async def test_per_call_deadline_preserves_existing_default(self):
        self.service.config.timeout_seconds = 0.01
        tool = create_device_tool(self.service)
        task = asyncio.create_task(tool(action="call", device_id="desk", capability="custom.execute", timeout_seconds=1))
        command = await self.socket.next_command()
        await asyncio.sleep(0.03)
        self.service.resolve(self.connection, DeviceResult(type="result", command_id=command["command_id"], ok=True))
        self.assertEqual((await task)["status"], "success")
        for timeout in (True, 0, 121, float("nan")):
            self.assertEqual((await tool(action="call", device_id="desk", capability="custom.execute", timeout_seconds=timeout))["code"], "invalid_arguments")
        self.assertEqual((await tool(action="list", timeout_seconds=1))["code"], "invalid_arguments")


class DeviceMediaHTTPTests(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.service = make_service(self.root)
        self.on_event = AsyncMock()
        app = FastAPI()
        register_device_routes(app, self.service, on_event=self.on_event)
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.client.portal.call(self.service.start)
        self.addCleanup(lambda: self.client.portal.call(self.service.stop))

    def upload(self, data=b"hello", identifier=None, **headers):
        return self.client.put("/api/device-media/desk/" + (identifier or str(uuid.uuid4())), content=data,
            headers={"Authorization": "Bearer desk-secret", "Content-Type": "text/plain", **headers})

    def test_success_replay_and_authentication_before_body(self):
        identifier = str(uuid.uuid4())
        first = self.upload(identifier=identifier)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(self.upload(identifier=identifier).status_code, 200)
        self.assertEqual(self.upload(b"other", identifier=identifier).status_code, 409)
        pairing = self.client.portal.call(self.service.create_pairing)
        self.assertEqual(self.upload(Authorization="Bearer " + pairing["pairing_code"]).status_code, 401)
        self.assertEqual(self.upload(Authorization="Bearer robot-secret").status_code, 401)
        self.assertEqual(self.upload(**{"Content-Encoding": "gzip"}).status_code, 415)
        self.assertEqual(self.upload(**{"Content-Length": "0"}).status_code, 400)
        self.assertEqual(self.upload(**{"Content-Length": "broken"}).status_code, 400)
        self.assertEqual(self.upload(identifier="not-a-uuid").status_code, 422)
        self.assertEqual(self.upload(**{"X-Content-SHA256": "0" * 64}).status_code, 400)
        self.assertEqual(self.upload(**{"Content-Length": str(51 * 1024 * 1024)}).status_code, 413)
        self.assertEqual(first.headers["cache-control"], "no-store")

    def test_missing_length_and_disconnected_body_release_reservations(self):
        async def request(length, messages):
            app = self.client.app
            headers = [(b"authorization", b"Bearer desk-secret"), (b"content-type", b"text/plain")]
            if length is not None:
                headers.append((b"content-length", str(length).encode()))
            scope = {"type": "http", "http_version": "1.1", "method": "PUT", "scheme": "http", "path": "/api/device-media/desk/" + str(uuid.uuid4()),
                "query_string": b"", "headers": headers, "server": ("testserver", 80), "client": ("test", 1), "root_path": ""}
            sent = []
            async def receive():
                return messages.pop(0)
            async def send(message):
                sent.append(message)
            await app(scope, receive, send)
            return sent[0]["status"]
        self.assertEqual(self.client.portal.call(request, None, []), 411)
        self.assertEqual(self.client.portal.call(request, 5, [{"type": "http.request", "body": b"he", "more_body": True}, {"type": "http.disconnect"}]), 400)
        self.assertFalse(list(self.service.media.state_dir.glob("*.part")))

    def test_events_reference_uploaded_files_without_chat_or_reply(self):
        metadata = self.upload().json()
        with self.client.websocket_connect("/ws/devices/desk", headers={"Authorization": "Bearer desk-secret"}) as socket:
            socket.send_json(manifest().model_dump())
            socket.send_json({"type": "event", "event_id": "media-event", "event_type": "recording", "context": "A recording is available.",
                "attachments": [{"attachment_id": metadata["attachment_id"]}]})
            import time
            deadline = time.monotonic() + 2
            while not self.on_event.await_count and time.monotonic() < deadline:
                time.sleep(0.005)
            self.on_event.assert_awaited_once()
            self.assertEqual(self.on_event.await_args.kwargs["attachments"][0]["source_device_id"], "desk")
            socket.send_json({"type": "event", "event_id": "bad", "event_type": "recording", "context": "Bad reference",
                "attachments": [{"attachment_id": str(uuid.uuid4())}]})
            with self.assertRaises(WebSocketDisconnect):
                socket.receive_json()

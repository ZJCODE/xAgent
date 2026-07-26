from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import yaml

from xagent.interfaces.cli.agents import register_agent
from xagent.interfaces.web import WebClientServer


def _write_agent(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 2,
                "provider": {
                    "name": "openai",
                    "api_key": "test-key",
                    "model": "gpt-5.4-mini",
                },
            }
        ),
        encoding="utf-8",
    )
    (path / "identity.md").write_text("# Identity\n\nTest agent.\n", encoding="utf-8")


class WebClientServerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.agent = self.root / "agents" / "agent"
        _write_agent(self.agent)
        register_agent("agent", path=self.agent, make_active=True, root=self.root)
        self.server = WebClientServer(
            host="127.0.0.1",
            port=1415,
            config_dir=str(self.agent),
            initial_agent="agent",
            registry_root=self.root,
        )

    async def client(self):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.server.app),
            base_url="http://web",
        )

    async def test_spa_only_exposes_the_new_management_pages(self):
        async with await self.client() as client:
            for path in (
                "/",
                "/chat",
                "/messages",
                "/memory",
                "/tasks",
                "/channels",
                "/deliveries",
                "/settings",
            ):
                response = await client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn("text/html", response.headers["content-type"])

    async def test_web_health_reports_runtime_separately(self):
        with patch(
            "xagent.interfaces.web.agent_routes.RuntimeClient.status",
            return_value={"running": True},
        ):
            async with await self.client() as client:
                response = await client.get("/api/health")
        self.assertEqual(
            response.json(),
            {"status": "ok", "web": True, "runtime_running": True},
        )

    async def test_delivery_review_is_forwarded_to_loopback_control(self):
        calls: list[tuple[str, str, object]] = []

        def request(method: str, path: str, **kwargs):
            calls.append((method, path, kwargs.get("json")))
            if path.endswith("/retry"):
                return {"delivery": {"delivery_id": "delivery"}}
            return {"deliveries": []}

        with patch(
            "xagent.interfaces.web.proxy.RuntimeClient.status",
            return_value={"running": True},
        ), patch(
            "xagent.interfaces.web.proxy.RuntimeClient.request",
            side_effect=request,
        ):
            async with await self.client() as client:
                self.assertEqual((await client.get("/api/deliveries?status=blocked")).status_code, 200)
                retried = await client.post("/api/deliveries/delivery/retry")
                self.assertEqual(retried.status_code, 200)
                self.assertEqual((await client.get("/api/people")).status_code, 404)
        self.assertIn(("GET", "/v1/deliveries?status=blocked", None), calls)
        self.assertIn(("POST", "/v1/deliveries/delivery/retry", None), calls)

    async def test_web_chat_uses_the_implicit_owner_identity(self):
        payloads: list[dict[str, object]] = []

        def request(method: str, path: str, **kwargs):
            payloads.append(dict(kwargs.get("json") or {}))
            return {
                "result": {
                    "events": [
                        {"type": "message_done", "content": "hello owner"},
                    ]
                }
            }

        with patch(
            "xagent.interfaces.web.proxy.RuntimeClient.status",
            return_value={"running": True},
        ), patch(
            "xagent.interfaces.web.proxy.RuntimeClient.request",
            side_effect=request,
        ):
            async with await self.client() as client:
                accepted = await client.post("/chat", json={"user_message": "hello"})
                rejected = await client.post(
                    "/chat",
                    json={"user_id": "someone-else", "user_message": "hello"},
                )

        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json()["reply"], "hello owner")
        self.assertEqual(rejected.status_code, 422)
        self.assertEqual(payloads[0]["source"], "web")
        self.assertEqual(payloads[0]["speaker_id"], "owner")
        self.assertEqual(payloads[0]["conversation_id"], "web:main")

    async def test_web_task_records_creation_source_separately_from_destination(self):
        payloads: list[dict[str, object]] = []

        def request(method: str, path: str, **kwargs):
            payload = dict(kwargs.get("json") or {})
            payloads.append(payload)
            return {"task": payload}

        with patch(
            "xagent.interfaces.web.proxy.RuntimeClient.status",
            return_value={"running": True},
        ), patch(
            "xagent.interfaces.web.proxy.RuntimeClient.request",
            side_effect=request,
        ):
            async with await self.client() as client:
                response = await client.post(
                    "/api/tasks",
                    json={
                        "instruction": "review today",
                        "schedule": {
                            "kind": "once",
                            "run_at": "2099-01-01T09:00:00+08:00",
                        },
                        "destination": None,
                    },
                )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(payloads[0]["created_source"], "web")
        self.assertEqual(payloads[0]["created_by"], "owner")
        self.assertIsNone(payloads[0]["destination"])

    async def test_settings_api_masks_and_preserves_provider_secret(self):
        async with await self.client() as client:
            loaded = await client.get("/api/settings")
            payload = loaded.json()
            self.assertEqual(payload["settings"]["provider"]["api_key"], "••••••••")
            self.assertNotIn("test-key", loaded.text)

            payload["settings"]["agent"]["max_history"] = 19
            saved = await client.put(
                "/api/settings",
                json={"settings": payload["settings"]},
            )

        self.assertEqual(saved.status_code, 200)
        config = yaml.safe_load((self.agent / "config.yaml").read_text(encoding="utf-8"))
        self.assertEqual(config["provider"]["api_key"], "test-key")
        self.assertEqual(config["agent"]["max_history"], 19)

    async def test_desktop_read_models_are_forwarded_with_bounded_queries(self):
        calls: list[tuple[str, str]] = []

        def request(method: str, path: str, **kwargs):
            calls.append((method, path))
            if path == "/v1/overview":
                return {"runtime": {"running": True}, "counts": {}, "recent_events": []}
            if path.startswith("/v1/messages"):
                return {"messages": [], "total": 0, "has_more": False}
            if path.startswith("/v1/memory/file"):
                return {"path": "daily/a b.md", "content": "# A"}
            return {"entries": [], "total": 0}

        with patch(
            "xagent.interfaces.web.proxy.RuntimeClient.status",
            return_value={"running": True},
        ), patch(
            "xagent.interfaces.web.proxy.RuntimeClient.request",
            side_effect=request,
        ):
            async with await self.client() as client:
                self.assertEqual((await client.get("/api/overview")).status_code, 200)
                self.assertEqual(
                    (
                        await client.get(
                            "/api/messages",
                            params={"q": "one two", "role": "user", "source": "web"},
                        )
                    ).status_code,
                    200,
                )
                self.assertEqual(
                    (
                        await client.get(
                            "/api/memory",
                            params={"scope": "daily", "q": "project"},
                        )
                    ).status_code,
                    200,
                )
                self.assertEqual(
                    (
                        await client.get(
                            "/api/memory/file",
                            params={"path": "daily/a b.md"},
                        )
                    ).status_code,
                    200,
                )

        self.assertIn(("GET", "/v1/overview"), calls)
        self.assertIn(
            (
                "GET",
                "/v1/messages?limit=50&offset=0&q=one+two&role=user&source=web",
            ),
            calls,
        )
        self.assertIn(
            ("GET", "/v1/memory?scope=daily&q=project&limit=200"),
            calls,
        )
        self.assertIn(
            ("GET", "/v1/memory/file?path=daily%2Fa+b.md"),
            calls,
        )

    async def test_runtime_status_and_overview_have_useful_stopped_snapshots(self):
        from xagent.core.runtime import RuntimeUnavailable

        with patch(
            "xagent.interfaces.web.proxy.RuntimeClient.status",
            side_effect=RuntimeUnavailable("not running"),
        ):
            async with await self.client() as client:
                runtime = await client.get("/api/runtime")
                overview = await client.get("/api/overview")

        self.assertEqual(runtime.status_code, 200)
        self.assertFalse(runtime.json()["running"])
        self.assertEqual(overview.status_code, 200)
        self.assertFalse(overview.json()["runtime"]["running"])
        self.assertIsNone(overview.json()["counts"])

    async def test_runtime_lifecycle_is_owned_by_the_web_management_process(self):
        status = {
            "pid": 42,
            "instance_id": "runtime",
            "started_at": 1.0,
            "running": True,
            "channels": [],
        }
        with patch(
            "xagent.interfaces.web.proxy.RuntimeLauncher.start",
            return_value=SimpleNamespace(state="started"),
        ) as start, patch(
            "xagent.interfaces.web.proxy.RuntimeLauncher.status",
            return_value=status,
        ):
            async with await self.client() as client:
                response = await client.post("/api/runtime/start")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["outcome"], "started")
        self.assertEqual(response.json()["runtime"]["pid"], 42)
        start.assert_called_once()

    async def test_web_server_rejects_public_bind_addresses_and_bad_ports(self):
        with self.assertRaisesRegex(ValueError, "loopback"):
            WebClientServer(host="0.0.0.0", port=1415)
        with self.assertRaisesRegex(ValueError, "between 1 and 65535"):
            WebClientServer(host="127.0.0.1", port=0)

    async def test_missing_ui_assets_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            static = Path(temporary)
            with self.assertRaises(FileNotFoundError):
                WebClientServer(host="127.0.0.1", port=1415, static_dir=static)

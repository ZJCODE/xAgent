"""Tests for the web client server."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from xagent.interfaces.clients.web import WebClientServer


class FakeAgent:
    model = "test-model"


class WebClientServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_web_client_serves_spa_shell(self):
        server = WebClientServer(
            host="127.0.0.1",
            port=8011,
            api_url="http://127.0.0.1:8010",
        )
        client = TestClient(server.app)

        for path in ("/", "/memory", "/workspace", "/message", "/agent", "/skills", "/tasks"):
            response = client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertIn("text/html", response.headers.get("content-type", ""))

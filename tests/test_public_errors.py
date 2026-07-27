"""Tests for the public chat error surface."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from xagent.core.errors import (
    ERROR_EMPTY_RESPONSE,
    ERROR_INTERNAL,
    ERROR_MODEL_UNAVAILABLE,
    ERROR_TIMEOUT,
    DEFAULT_MESSAGES,
    build_public_error,
    map_model_error,
    map_model_error_code,
)
from xagent.core.handlers.model import ModelErrorEvent


class PublicErrorSurfaceTests(unittest.TestCase):
    def test_new_error_id_is_embedded_in_message(self):
        with patch("xagent.core.errors.new_error_id", return_value="abcd1234"):
            payload = build_public_error(code=ERROR_TIMEOUT, log=False)

        self.assertEqual(payload["type"], "error")
        self.assertEqual(payload["error_code"], ERROR_TIMEOUT)
        self.assertEqual(payload["error_id"], "abcd1234")
        self.assertEqual(payload["status_code"], 504)
        self.assertEqual(
            payload["error"],
            f"{DEFAULT_MESSAGES[ERROR_TIMEOUT]} (error_id=abcd1234)",
        )

    def test_map_model_error_codes(self):
        self.assertEqual(map_model_error_code("model_call_failed"), ERROR_MODEL_UNAVAILABLE)
        self.assertEqual(map_model_error_code("model_stream_failed"), ERROR_MODEL_UNAVAILABLE)
        self.assertEqual(map_model_error_code("model_stream_error"), ERROR_MODEL_UNAVAILABLE)
        self.assertEqual(map_model_error_code("empty_model_response"), ERROR_EMPTY_RESPONSE)
        self.assertEqual(map_model_error_code("empty_stream_response"), ERROR_EMPTY_RESPONSE)
        self.assertEqual(map_model_error_code("unknown_code"), ERROR_MODEL_UNAVAILABLE)
        self.assertEqual(
            map_model_error(
                ModelErrorEvent(
                    code="empty_model_response",
                    message="empty",
                    details="secret provider body",
                )
            ),
            ERROR_EMPTY_RESPONSE,
        )

    def test_build_public_error_hides_cause_from_payload(self):
        payload = build_public_error(
            code=ERROR_MODEL_UNAVAILABLE,
            cause=ModelErrorEvent(
                code="model_call_failed",
                message="Model call failed.",
                details="provider rejected messages",
            ),
            log=False,
        )

        self.assertNotIn("provider rejected messages", payload["error"])
        self.assertNotIn("details", payload)
        self.assertIn("error_id=", payload["error"])
        self.assertEqual(payload["error_code"], ERROR_MODEL_UNAVAILABLE)

    def test_unknown_code_falls_back_to_internal(self):
        payload = build_public_error(code="not_a_real_code", log=False)
        self.assertEqual(payload["error_code"], ERROR_INTERNAL)
        self.assertEqual(payload["status_code"], 500)

    def test_custom_message_still_appends_error_id(self):
        with patch("xagent.core.errors.new_error_id", return_value="deadbeef"):
            payload = build_public_error(
                code=ERROR_TIMEOUT,
                message="Agent observe timed out.",
                log=False,
            )
        self.assertEqual(payload["error"], "Agent observe timed out. (error_id=deadbeef)")


if __name__ == "__main__":
    unittest.main()

import unittest

from xagent.core.inbox import InboxKind
from xagent.interfaces.channel import ChatTurnRequest


class ChatTurnRequestTests(unittest.TestCase):
    def test_to_chat_kwargs_includes_inbox_kind_and_omits_empties(self):
        request = ChatTurnRequest(
            user_message="hello",
            user_id="alice",
            channel="feishu",
            room_name="group-1",
            channel_instructions="mention with at tags",
        )
        kwargs = request.to_chat_kwargs()
        self.assertEqual(kwargs["user_message"], "hello")
        self.assertEqual(kwargs["user_id"], "alice")
        self.assertEqual(kwargs["channel"], "feishu")
        self.assertEqual(kwargs["inbox_kind"], InboxKind.USER_TURN.value)
        self.assertEqual(kwargs["room_name"], "group-1")
        self.assertEqual(kwargs["channel_instructions"], "mention with at tags")
        self.assertNotIn("attachments", kwargs)
        self.assertNotIn("image_source", kwargs)
        self.assertNotIn("stream", kwargs)
        self.assertNotIn("sender_name", kwargs)

    def test_to_chat_kwargs_includes_sender_name_when_present(self):
        request = ChatTurnRequest(
            user_message="hello",
            user_id="ou_user",
            channel="feishu",
            sender_name="Alice",
        )
        kwargs = request.to_chat_kwargs()
        self.assertEqual(kwargs["sender_name"], "Alice")

    def test_feishu_group_builder_shape_matches_chat_events(self):
        request = ChatTurnRequest(
            user_message="ping",
            user_id="Alice",
            channel="feishu",
            inbox_kind="user_turn",
            room_name="Eng",
            channel_instructions="For mentions, use at tags",
        )
        kwargs = request.to_chat_kwargs()
        self.assertEqual(
            set(kwargs),
            {
                "user_message",
                "user_id",
                "channel",
                "inbox_kind",
                "room_name",
                "channel_instructions",
            },
        )

    def test_scheduled_kind_and_stream_are_passed_through(self):
        request = ChatTurnRequest(
            user_message="run the nightly check",
            user_id="system",
            channel="api",
            inbox_kind=InboxKind.SCHEDULED_TURN.value,
            stream=True,
        )
        kwargs = request.to_chat_kwargs()
        self.assertEqual(kwargs["inbox_kind"], "scheduled_turn")
        self.assertTrue(kwargs["stream"])

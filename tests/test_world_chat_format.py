import unittest

from agents_world.chat_format import format_chat_event, format_join_intro


class WorldChatFormatTests(unittest.TestCase):
    def test_utterance_you_and_other(self):
        self.assertEqual(
            format_chat_event({"kind": "utterance", "actor_id": "human", "text": "hi"}, member_id="human"),
            "you: hi",
        )
        self.assertEqual(
            format_chat_event({"kind": "utterance", "actor_id": "mira", "text": "hey"}, member_id="human"),
            "mira: hey",
        )

    def test_join_leave(self):
        self.assertEqual(
            format_chat_event({"kind": "join", "actor_id": "echo"}, member_id="human"),
            "· echo joined",
        )

    def test_join_intro_skips_history_hint_when_empty(self):
        lines = format_join_intro(
            world_name="plaza",
            present=[{"member_id": "human", "display_name": "human"}],
            member_id="human",
            event_count=0,
            full_history=False,
        )
        self.assertIn("In plaza.", lines[0])
        self.assertEqual(len(lines), 2)

    def test_join_intro_history_hint(self):
        lines = format_join_intro(
            world_name="plaza",
            present=[],
            member_id="human",
            event_count=12,
            full_history=False,
        )
        self.assertIn("12 earlier messages", lines[1])
        self.assertIn("--full-history", lines[1])


if __name__ == "__main__":
    unittest.main()

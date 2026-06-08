import unittest
from datetime import datetime

from xagent.core.formatters import RoomContextEntry, format_room_context


class RoomContextFormatterTests(unittest.TestCase):
    def test_format_room_context_sorts_entries_and_marks_self_as_you(self):
        text = format_room_context(
            "room-1",
            [
                RoomContextEntry(
                    speaker_label="Mono",
                    occurred_at=datetime(2024, 1, 2, 9, 31),
                    text="I can help with that",
                    is_self=True,
                ),
                RoomContextEntry(
                    speaker_label="Alice",
                    occurred_at=datetime(2024, 1, 2, 9, 30),
                    text="Can you review this?",
                ),
            ],
            room_name="Team Sync",
        )

        self.assertEqual(
            text,
            "[room context]\n"
            "room_name: Team Sync\n"
            "room_id: room-1\n\n"
            "Alice 2024-01-02 09:30: Can you review this?\n"
            "ME 2024-01-02 09:31: I can help with that\n"
            "[/room context]",
        )

    def test_format_room_context_collapses_text_and_sanitizes_fields(self):
        text = format_room_context(
            "room]\n42",
            [
                RoomContextEntry(
                    speaker_label="Bob]\n",
                    occurred_at=datetime(2024, 3, 4, 5, 6),
                    text="hello\n  there",
                )
            ],
            room_name="Project]\nAlpha",
        )

        self.assertEqual(
            text,
            "[room context]\n"
            "room_name: Project Alpha\n"
            "room_id: room 42\n\n"
            "Bob 2024-03-04 05:06: hello there\n"
            "[/room context]",
        )

    def test_format_room_context_returns_body_when_room_id_missing(self):
        text = format_room_context(
            "",
            [
                RoomContextEntry(
                    speaker_label="Alice",
                    occurred_at=datetime(2024, 1, 2, 9, 30),
                    text="still useful",
                )
            ],
            room_name="Ignored",
        )

        self.assertEqual(text, "Alice 2024-01-02 09:30: still useful")

    def test_format_room_context_skips_blank_entries(self):
        text = format_room_context(
            "room-1",
            [
                RoomContextEntry(
                    speaker_label="Alice",
                    occurred_at=datetime(2024, 1, 2, 9, 30),
                    text="   ",
                ),
                RoomContextEntry(
                    speaker_label="Bob",
                    occurred_at=datetime(2024, 1, 2, 9, 31),
                    text="ready",
                ),
            ],
        )

        self.assertNotIn("Alice", text)
        self.assertIn("Bob 2024-01-02 09:31: ready", text)


if __name__ == "__main__":
    unittest.main()

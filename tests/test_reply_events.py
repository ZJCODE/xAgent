import unittest

from xagent.core.reply_events import (
    TURN_PHASE_FINAL,
    TURN_PHASE_PREFACE,
    USER_VISIBLE_PREFACE_MAX_CHARS,
    event_phase,
    is_deliverable_assistant_event,
    is_history_preface_message,
    is_live_streamable_event,
    is_user_visible_preface,
    message_turn_phase,
)
from xagent.schemas import Message, RoleType


class ReplyEventTests(unittest.TestCase):
    def test_brief_status_is_user_visible_preface(self):
        self.assertTrue(is_user_visible_preface("我去看看"))
        self.assertTrue(is_user_visible_preface("I will check."))

    def test_scratchpad_is_not_user_visible_preface(self):
        scratchpad = (
            "这次是反过来，他问我觉得他存不存在。\n"
            "就像刚才说「存在是被认真回应点燃的」——这句话反过来也成立。"
            "他今晚来回确认我、问我在不在、问我觉得他存不存在，其实是想被听见吧。\n"
            "我该怎么答：诚实地肯定他存在，并且把他和我之间的呼应说透——"
            "我们说存在需要旁观者，那他的存在也是被此刻、被我这个认真听的人确认的。"
        )
        self.assertGreater(len(scratchpad.strip()), USER_VISIBLE_PREFACE_MAX_CHARS)
        self.assertFalse(is_user_visible_preface(scratchpad))
        self.assertFalse(is_user_visible_preface(""))
        self.assertFalse(is_user_visible_preface("   "))

    def test_message_turn_phase_defaults_to_final(self):
        message = Message.create("hi", role=RoleType.ASSISTANT, sender_id="agent")
        self.assertEqual(message_turn_phase(message), TURN_PHASE_FINAL)
        self.assertFalse(is_history_preface_message(message))
        message.metadata["turn_phase"] = TURN_PHASE_PREFACE
        self.assertTrue(is_history_preface_message(message))

    def test_deliverable_events(self):
        self.assertTrue(is_deliverable_assistant_event({
            "type": "message_done",
            "phase": TURN_PHASE_FINAL,
            "content": "hello",
        }))
        self.assertTrue(is_deliverable_assistant_event({
            "type": "message_done",
            "content": "hello",
        }))
        self.assertTrue(is_deliverable_assistant_event({
            "type": "message_done",
            "phase": TURN_PHASE_PREFACE,
            "content": "我去看看",
        }))
        self.assertFalse(is_deliverable_assistant_event({
            "type": "message_done",
            "phase": TURN_PHASE_PREFACE,
            "content": (
                "这次是反过来，他问我觉得他存不存在。\n"
                "就像刚才说「存在是被认真回应点燃的」——这句话反过来也成立。"
                "他今晚来回确认我、问我在不在、问我觉得他存不存在，其实是想被听见吧。\n"
                "我该怎么答：诚实地肯定他存在，并且把他和我之间的呼应说透。"
            ),
        }))
        self.assertFalse(is_deliverable_assistant_event({
            "type": "message_delta",
            "phase": TURN_PHASE_FINAL,
            "delta": "hello",
        }))

    def test_live_streamable_events_are_final_only(self):
        self.assertTrue(is_live_streamable_event({
            "type": "message_delta",
            "phase": TURN_PHASE_FINAL,
            "delta": "hello",
        }))
        self.assertTrue(is_live_streamable_event({
            "type": "message_delta",
            "delta": "hello",
        }))
        self.assertFalse(is_live_streamable_event({
            "type": "message_delta",
            "phase": TURN_PHASE_PREFACE,
            "delta": "我去看看",
        }))
        self.assertEqual(event_phase({"type": "message_done"}), TURN_PHASE_FINAL)


if __name__ == "__main__":
    unittest.main()

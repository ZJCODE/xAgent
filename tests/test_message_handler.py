import unittest

from xagent.core.handlers.message import MessageHandler


class MessageHandlerJournalFormattingTests(unittest.TestCase):
    def test_format_memories_preserves_multiline_journal_entries(self):
        journal = "\n\n".join(
            [
                "今天发生了什么\n今天主要围绕路线图推进。",
                "有哪些人，他们大致在做什么\nalice 在推进发布节奏。\n用户A 在补充评审意见。",
                "有意思或重要的信息\nalice 说了一句“别拖了”。",
                "简单总结或感受\n整体节奏更清晰了一些。",
            ]
        )

        formatted = MessageHandler._format_memories(
            [
                {
                    "content": journal,
                    "metadata": {"journal_date": "2026-03-18"},
                }
            ]
        )

        self.assertIn("[2026-03-18]\n今天发生了什么", formatted)
        self.assertIn("有哪些人，他们大致在做什么\nalice 在推进发布节奏。", formatted)
        self.assertNotIn("[2026-03-18] 今天发生了什么", formatted)


if __name__ == "__main__":
    unittest.main()

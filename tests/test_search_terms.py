"""Tests for shared search term helpers."""

import unittest

from xagent.utils.search_terms import normalize_terms, score_text


class SearchTermsTests(unittest.TestCase):
    def test_normalize_terms_strips_and_drops_empty(self):
        self.assertEqual(
            normalize_terms([" Jun", " 书 ", "", "  ", "推荐"]),
            ["Jun", "书", "推荐"],
        )

    def test_normalize_terms_does_not_split_phrases(self):
        self.assertEqual(
            normalize_terms(["API documentation", "Jun"]),
            ["API documentation", "Jun"],
        )

    def test_score_text_counts_distinct_hits(self):
        text = "Jun 推荐阅读几本书"
        self.assertEqual(score_text(text, ["Jun", "书", "阅读", "推荐"]), 4)
        self.assertEqual(score_text(text, ["Jun", "喜欢", "爱好", "兴趣"]), 1)


if __name__ == "__main__":
    unittest.main()

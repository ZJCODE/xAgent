"""Configuration constants for the minimal long-term memory pipeline."""

EXPLICIT_MEMORY_PATTERNS = [
    r"请记住",
    r"帮我记住",
    r"记住(?:这个|这件事|这一点|一下)?",
    r"别忘了",
    r"记一下",
    r"记下来",
    r"\bremember\s+this\b",
    r"\bplease\s+remember\b",
    r"\bdon'?t\s+forget\b",
    r"\bnote\s+this\s+down\b",
    r"\bmake\s+a\s+note\s+of\s+this\b",
]

MEMORY_DUPLICATE_SCORE_THRESHOLD = 0.92
MEMORY_REPLACEMENT_MIN_LENGTH_DELTA = 12
MEMORY_RETRIEVAL_MIN_SCORE = 0.35
MEMORY_RETRIEVAL_OVERSCAN = 5
MEMORY_EXTRACTION_INTERVAL_SECONDS = 300
MEMORY_FORCE_EXTRACTION_MULTIPLIER = 2
MEMORY_MAX_BATCH_MESSAGES = 40

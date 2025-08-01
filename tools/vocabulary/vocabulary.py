import os
import logging
from dotenv import load_dotenv
import time
from collections import defaultdict
from typing import Optional, List, Dict

from langfuse import observe
from langfuse.openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential
from pydantic import ValidationError

from .vocabulary_schema import BaseVocabularyRecord, VocabularyRecord
from .vocabulary_db import VocabularyDB


load_dotenv(override=True)

# 日志系统初始化
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

class Vocabulary:
    """
    用户词汇服务，负责单词查询、缓存、存储与推荐等功能。
    """

    DEFAULT_USER_ID = "anonymous"
    DEFAULT_MODEL = "gpt-4o-mini"
    SYSTEM_MESSAGE = (
        "You are a helpful vocabulary assistant. "
        "When explaining a word, tailor your explanation, examples, and details according to the word's difficulty level: "
        "For 'Beginner', use simple language and basic examples. "
        "For 'Intermediate', provide more detail and moderately complex examples. "
        "For 'Advanced', give in-depth explanations and sophisticated example sentences. "
        "Always include the word's definition, example sentences, and specify the difficulty level in your response."
    )

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        client: Optional[OpenAI] = None,
        db: Optional[VocabularyDB] = None,
        system_message: Optional[str] = None,
    ):
        self.model: str = model
        self.client: OpenAI = client or OpenAI()
        self.db: VocabularyDB = db or VocabularyDB(os.environ.get("REDIS_URL"))
        self.system_message: str = system_message or self.SYSTEM_MESSAGE
        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.info("Vocabulary initialized with model: %s", self.model)

    @observe()
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    def lookup_word(
        self,
        word: str,
        user_id: Optional[str] = None,
        cache: bool = True,
        **kwargs
    ) -> BaseVocabularyRecord | VocabularyRecord:
        """
        查询单词详细信息。优先缓存/数据库，无则调用 LLM 并保存结果。
        Args:
            word (str): 要查询的单词
            user_id (str, 可选): 用户ID
            cache (bool): 是否优先查缓存/数据库
            kwargs: 额外字段，存入 extra 字段
        Returns:
            BaseVocabularyRecord 或 VocabularyRecord
        """
        self.logger.info("Looking up word: '%s' for user: %s (cache=%s)", word, user_id, cache)
        user_id = user_id or self.DEFAULT_USER_ID
        word = self._preprocess_word(word)
        if not word:
            raise ValueError("Word cannot be empty or None")
        if cache:
            existing = self.db.get_vocabulary(user_id, word, reduce_familiarity=True)
            if existing:
                self.logger.info("Cache hit for word: '%s' (user: %s)", word, user_id)
                return existing
        try:
            record = self._llm_lookup_word(word)
            vocab_record = self._create_vocabulary_record(user_id, record, **kwargs)
            self.db.save_vocabulary(vocab_record)
            self.logger.info("Word '%s' saved to DB for user: %s", word, user_id)
            return vocab_record
        except Exception as e:
            self.logger.exception("lookup_word error for word: '%s' (user: %s)", word, user_id)
            raise

    @observe()
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    def get_vocabulary(
        self,
        user_id: str,
        n: int = 10,
        exclude_known: bool = False
    ) -> List[VocabularyRecord]:
        """
        获取用户最需要复习的 N 个词汇。分层采样，优先覆盖不同难度。
        Args:
            user_id (str): 用户ID
            n (int): 返回数量N，-1表示返回全部
            exclude_known (bool): 是否排除已掌握词汇
        Returns:
            List[VocabularyRecord]: 推荐词汇列表
        """
        self.logger.info("Fetching vocabulary for user: %s, n=%d, exclude_known=%s", user_id, n, exclude_known)
        all_words = self.db.get_all_words_by_user(user_id, exclude_known=exclude_known)
        if not all_words:
            self.logger.info("No vocabulary found for user: %s", user_id)
            return []
        if n == -1:
            return all_words
        selected = self._select_review_words(all_words, n)
        self.logger.info("Returning %d vocabulary records for user: %s", len(selected), user_id)
        return selected

    @observe()
    def _llm_lookup_word(self, word: str) -> BaseVocabularyRecord:
        """
        调用 LLM 查询单词详细信息。
        Args:
            word (str): 要查询的单词
        Returns:
            BaseVocabularyRecord
        """
        self.logger.info("Calling LLM for word: '%s'", word)
        completion = self.client.chat.completions.parse(
            model=self.model,
            messages=[
                {"role": "system", "content": self.system_message},
                {"role": "user", "content": word}
            ],
            response_format=BaseVocabularyRecord,
        )
        self.logger.info("LLM lookup completed for word: '%s'", word)
        return completion.choices[0].message.parsed

    def _create_vocabulary_record(
        self,
        user_id: str,
        record: BaseVocabularyRecord,
        **kwargs
    ) -> VocabularyRecord:
        """
        构造 VocabularyRecord 记录（带用户信息和时间戳）。
        Args:
            user_id (str): 用户ID
            record (BaseVocabularyRecord): 基础词汇记录
            kwargs: 额外字段，存入 extra
        Returns:
            VocabularyRecord
        Raises:
            ValidationError: 字段校验失败
        """
        now = time.time()
        data = record.model_dump()
        data["user_id"] = user_id
        data["create_timestamp"] = now
        data["update_timestamp"] = now
        data["extra"] = kwargs if kwargs else {}
        try:
            vocab_record = VocabularyRecord.model_validate(data)
            self.logger.info("VocabularyRecord created for word: '%s' (user: %s)", data.get("word"), user_id)
            return vocab_record
        except ValidationError as e:
            self.logger.exception("ValidationError for word: '%s' (user: %s)", data.get("word"), user_id)
            raise

    def _preprocess_word(self, word: Optional[str]) -> Optional[str]:
        """
        单词预处理（去除首尾空格并小写）。
        Args:
            word (str): 原始单词
        Returns:
            str: 处理后的单词，若为空返回 None
        """
        if not word:
            return None
        w = word.strip().lower()
        return w if w else None

    def _select_review_words(self, all_words: List[VocabularyRecord], n: int) -> List[VocabularyRecord]:
        """
        分层采样，优先覆盖不同难度，保证推荐词汇难度分布多样。
        Args:
            all_words (List[VocabularyRecord]): 用户所有词汇
            n (int): 推荐数量
        Returns:
            List[VocabularyRecord]: 推荐词汇列表
        """
        now = time.time()
        def score(v: VocabularyRecord) -> float:
            familiarity_score = 10 - (v.familiarity or 0)
            last_reviewed = v.last_reviewed_timestamp or v.update_timestamp or v.create_timestamp or 0
            time_since_review = now - last_reviewed
            return familiarity_score * 2 + time_since_review / (60*60*24)

        difficulty_buckets: Dict[str, List[VocabularyRecord]] = defaultdict(list)
        for v in all_words:
            difficulty_buckets[str(v.difficulty_level)].append(v)

        selected: List[VocabularyRecord] = []
        for bucket in difficulty_buckets.values():
            if bucket:
                bucket_sorted = sorted(bucket, key=score, reverse=True)
                selected.append(bucket_sorted[0])
        if len(selected) < n:
            selected_ids = set(id(v) for v in selected)
            remaining = [v for v in all_words if id(v) not in selected_ids]
            remaining_sorted = sorted(remaining, key=score, reverse=True)
            selected += remaining_sorted[:n-len(selected)]
        return selected[:n]

if __name__ == "__main__":
    # Example usage
    service = Vocabulary()
    try:
        record = service.lookup_word("apple", user_id="user123")
        record = service.lookup_word("sophisticated", user_id="user123")
        record = service.lookup_word("enumeration", user_id="user123")
        record = service.lookup_word("physiology", user_id="user123")

        words = service.get_vocabulary("user123", n=5)
        for w in words:
            print(f"Word: {w.word}, Difficulty: {w.difficulty_level}, Familiarity: {w.familiarity}, Last Reviewed: {w.last_reviewed_timestamp}")
        print("Total words:", len(words))

    except Exception as e:
        logging.exception("Error: %s", e)
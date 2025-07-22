import os
import json
from dotenv import load_dotenv
import time
from collections import defaultdict

from langfuse import observe
from langfuse.openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential
from pydantic import ValidationError


from schemas.vocabulary import BaseVocabularyRecord,VocabularyRecord
from db.vocabulary_db import VocabularyDB


load_dotenv(override=True)

class VocabularyService:

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

    def __init__(self,
                 model: str = DEFAULT_MODEL,
                 client: OpenAI = None,
                 db: VocabularyDB = None,
                 ):
        """
        初始化 VocabularyService 服务。
        :param model: 使用的 LLM 模型名称
        :param client: OpenAI 客户端实例
        :param db: 词汇数据库实例
        """
        self.client = client or OpenAI()
        self.model = model
        self.db = db or VocabularyDB(os.environ.get("REDIS_URL"))
        self.system_message = self.SYSTEM_MESSAGE
    
    @observe()
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    def lookup_word(self, word: str, user_id: str = None, 
                    save: bool = True, cache: bool = True, **kwargs) -> BaseVocabularyRecord | VocabularyRecord:
        """
        查询单词详细信息。
        优先从缓存/数据库获取，若无则调用 LLM 查询。
        可选：保存结果到数据库。
        
        :param word: 要查询的单词
        :param user_id: 用户ID（用于个性化存储）
        :param save: 是否保存结果到数据库
        :param cache: 是否优先查缓存/数据库
        :param kwargs: 额外字段，存入 extra 字段
        :return: BaseVocabularyRecord 或 VocabularyRecord
        """
        if not user_id:
            user_id = self.DEFAULT_USER_ID
            save = True
        word = self._preprocess_word(word)
        if not word or not word.strip():
            raise ValueError("Word cannot be empty or None")
        if cache:
            existing = self.db.get_vocabulary(user_id, word, reduce_familiarity=True)
            if existing:
                return existing
        record = self._llm_lookup_word(word)
        vocab_record = self._create_vocabulary_record(user_id, record, **kwargs)
        if save:
            self.db.save_vocabulary(vocab_record)
        return vocab_record

    @observe()
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    def get_vocabulary(self, user_id: str, n: int = 10, exclude_known: bool = False) -> list[VocabularyRecord]:
        """
        获取用户最需要复习的 N 个词汇。
        多路召回，分层采样，优先覆盖不同难度，保证推荐词汇难度分布多样。
        
        :param user_id: 用户ID
        :param n: 返回数量N，-1表示返回全部
        :param exclude_known: 是否排除已掌握词汇
        :return: VocabularyRecord 列表
        """
        all_words = self.db.get_all_words_by_user(user_id, exclude_known=exclude_known)
        if not all_words:
            return []
        if n == -1:
            return all_words
        now = time.time()
        # 计算优先级分数
        def score(v: VocabularyRecord):
            familiarity_score = 10 - (v.familiarity or 0)
            last_reviewed = v.last_reviewed_timestamp or v.update_timestamp or v.create_timestamp or 0
            time_since_review = now - last_reviewed
            return familiarity_score * 2 + time_since_review / (60*60*24)

        # 分层采样，优先覆盖不同难度
        difficulty_buckets = defaultdict(list)
        for v in all_words:
            difficulty_buckets[str(v.difficulty_level)].append(v)

        # 每层先取1个，剩余按优先级补齐
        selected = []
        for bucket in difficulty_buckets.values():
            if bucket:
                bucket_sorted = sorted(bucket, key=score, reverse=True)
                selected.append(bucket_sorted[0])
        # 如果还不够n个，按优先级补齐
        if len(selected) < n:
            selected_ids = set(id(v) for v in selected)
            remaining = [v for v in all_words if id(v) not in selected_ids]
            remaining_sorted = sorted(remaining, key=score, reverse=True)
            selected += remaining_sorted[:n-len(selected)]
        return selected[:n]

    @observe()
    def _llm_lookup_word(self, word: str) -> BaseVocabularyRecord:
        """
        调用 LLM 查询单词详细信息。
        :param word: 要查询的单词
        :return: BaseVocabularyRecord
        """
        completion = self.client.chat.completions.parse(
            model=self.model,
            messages=[
                {"role": "system", "content": self.system_message},
                {"role": "user", "content": word}
            ],
            response_format=BaseVocabularyRecord,
        )
        return completion.choices[0].message.parsed
    
    def _create_vocabulary_record(self, user_id: str, record: BaseVocabularyRecord, **kwargs) -> VocabularyRecord:
        """
        构造 VocabularyRecord 记录（带用户信息和时间戳）。
        :param user_id: 用户ID
        :param record: 基础词汇记录
        :param kwargs: 额外字段，存入 extra
        :return: VocabularyRecord
        """
        now = time.time()
        data = record.model_dump()
        data["user_id"] = user_id
        data["create_timestamp"] = now
        data["update_timestamp"] = now
        data["extra"] = kwargs if kwargs else {}
        try:
            return VocabularyRecord.model_validate(data)
        except ValidationError as e:
            print("ValidationError:", e)
            raise
    
    def _preprocess_word(self, word: str) -> str:
        """
        单词预处理（去除首尾空格并小写）。
        :param word: 原始单词
        :return: 处理后的单词，若为空返回 None
        """
        if not word:
            return None
        w = word.strip().lower()
        return w if w else None

if __name__ == "__main__":
    # Example usage
    service = VocabularyService()
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
        print(f"Error: {e}")
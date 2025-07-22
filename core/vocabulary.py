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

        self.client = client or OpenAI()
        self.model = model
        self.db = db or VocabularyDB(os.environ.get("REDIS_URL"))
        self.system_message = self.SYSTEM_MESSAGE
    
    @observe()
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    def lookup_word(self, word: str, user_id: str = None, 
                    save: bool = True, cache: bool = True, **kwargs) -> BaseVocabularyRecord | VocabularyRecord:
        """
        Look up a word using the LLM. Optionally save the result to the database if save=True and user_id is provided.
        If save=True and the word already exists for the user, return the existing record from the database.
        :param word: Word to look up
        :param user_id: User ID (required if save=True)
        :param save: Whether to save the result to the database
        :param kwargs: Additional fields to store in the record's extra field if saving
        :return: BaseVocabularyRecord (or VocabularyRecord if saved)
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
    def _llm_lookup_word(self, word: str) -> BaseVocabularyRecord:
        """
        Query the LLM to get the vocabulary record for the given word.
        :param word: Word to look up
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
        Preprocess the word for lookup, e.g., stripping whitespace.
        :param word: Word to preprocess
        :return: Preprocessed word or None if empty
        """
        if not word:
            return None
        w = word.strip().lower()
        return w if w else None
    
    @observe()
    def get_vocabulary(self, user_id: str, n: int = 10, exclude_known: bool = False) -> list[VocabularyRecord]:
        """
        多路召回，返回用户最需要复习的N个词汇。
        分层采样，优先覆盖不同难度，保证推荐词汇难度分布多样。
        :param user_id: 用户ID
        :param n: 返回数量N
        :return: VocabularyRecord列表
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
        # 先保证每个难度层至少有一个
        for bucket in difficulty_buckets.values():
            if bucket:
                bucket_sorted = sorted(bucket, key=score, reverse=True)
                selected.append(bucket_sorted[0])
        # 如果还不够n个，按优先级补齐
        if len(selected) < n:
            # 剩余未选中的词
            selected_ids = set(id(v) for v in selected)
            remaining = [v for v in all_words if id(v) not in selected_ids]
            remaining_sorted = sorted(remaining, key=score, reverse=True)
            selected += remaining_sorted[:n-len(selected)]
        # 最终只返回n个
        return selected[:n]

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
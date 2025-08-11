import os
import time
import redis
from dotenv import load_dotenv

load_dotenv(override=True)

from .vocabulary_schema import VocabularyRecord

class VocabularyDB:
    """
    VocabularyDB
    -------------------
    Redis-backed vocabulary database for user vocabulary records.
    所有词汇记录都以统一前缀（vocab:）隔离，便于多用户、多单词管理。
    主要功能：
    - 按用户/单词存储词汇记录
    - 支持词汇保存、获取、删除、批量获取、熟悉度调整、extra 字段设置
    - Redis key 统一封装，便于维护
    """
    VOCAB_PREFIX: str = "vocab"

    def __init__(self, redis_url: str = None):
        """
        初始化 VocabularyDB 实例，连接 Redis。
        Args:
            redis_url (str, optional): Redis 连接 URL。优先使用参数，否则读取环境变量 REDIS_URL。
        Raises:
            ValueError: 未提供 Redis 连接信息。
            ConnectionError: Redis 连接失败。
        """
        url = redis_url or os.environ.get("REDIS_URL")
        if not url:
            raise ValueError("REDIS_URL not set in environment or not provided as argument")
        try:
            self.client: redis.Redis = redis.Redis.from_url(url)
            self.client.ping()
        except Exception as e:
            raise ConnectionError(f"Failed to connect to Redis at {url}: {e}")
        
    def _make_key(self, user_id: str, word: str) -> str:
        """
        生成 Redis key，格式为 'vocab:<user_id>:<word>'。
        Args:
            user_id (str): 用户 ID / User ID.
            word (str): 单词 / Word.
        Returns:
            str: Redis key 字符串 / Redis key string.
        """
        return f"{self.VOCAB_PREFIX}:{user_id.lower()}:{word.lower()}"

    def save_vocabulary(self, vocab: VocabularyRecord) -> bool:
        """
        保存或更新一个词汇记录到 Redis，并更新时间戳。
        Args:
            vocab (VocabularyRecord): 词汇记录实例 / VocabularyRecord instance.
        Returns:
            bool: 总是 True / Always True.
        """
        vocab.update_timestamp = time.time()
        key = self._make_key(vocab.user_id, vocab.word)
        value = vocab.model_dump_json()
        self.client.set(key, value)
        return True

    def get_vocabulary(self, user_id: str, word: str, reduce_familiarity: bool = False) -> VocabularyRecord | None:
        """
        获取指定用户的某个单词的词汇记录。
        Args:
            user_id (str): 用户 ID / User ID.
            word (str): 单词 / Word.
            reduce_familiarity (bool): 是否减少熟悉度（如为 True，熟悉度减 1，最小为 0，并更新时间戳）/ If True, decrease familiarity by 1 (min 0) and update timestamp.
        Returns:
            VocabularyRecord | None: 词汇记录或 None / The vocabulary record or None if not found.
        """
        key = self._make_key(user_id, word)
        value = self.client.get(key)
        if value:
            vocab = VocabularyRecord.model_validate_json(value)
            if reduce_familiarity:
                vocab.familiarity = max(0, vocab.familiarity - 1)
                vocab.update_timestamp = time.time()
                self.client.set(key, vocab.model_dump_json())  # 更新时间戳
            return vocab
        return None

    def delete_vocabulary(self, user_id: str, word: str) -> int:
        """
        删除指定用户的某个单词的词汇记录。
        Args:
            user_id (str): 用户 ID / User ID.
            word (str): 单词 / Word.
        Returns:
            int: 删除的 key 数量（0 或 1）/ Number of keys deleted (0 or 1).
        """
        key = self._make_key(user_id, word)
        return self.client.delete(key)

    def get_all_words_by_user(self, user_id: str, exclude_known: bool = False) -> list[VocabularyRecord]:
        """
        获取指定用户的所有词汇记录。
        Args:
            user_id (str): 用户 ID / User ID.
            exclude_known (bool): 是否排除熟悉度为 10 的单词 / If True, exclude words with familiarity == 10.
        Returns:
            list[VocabularyRecord]: 词汇记录列表 / List of vocabulary records.
        """
        pattern = f"{self.VOCAB_PREFIX}:{user_id}:*"
        result = []
        for key in self.client.scan_iter(pattern):
            value = self.client.get(key)
            if value:
                vocab = VocabularyRecord.model_validate_json(value)
                if exclude_known and getattr(vocab, "familiarity", None) == 10:
                    continue
                result.append(vocab)
        return result

    def update_familiarity(self, user_id: str, word: str, delta: int) -> VocabularyRecord | None:
        """
        调整指定单词的熟悉度（加/减），范围限定在 0-10。
        Args:
            user_id (str): 用户 ID / User ID.
            word (str): 单词 / Word.
            delta (int): 增量（正负均可）/ Increment (positive or negative).
        Returns:
            VocabularyRecord | None: 更新后的词汇记录或 None / Updated vocabulary record or None if not found.
        """
        vocab = self.get_vocabulary(user_id, word)
        if not vocab:
            return None
        vocab.familiarity = max(0, min(10, vocab.familiarity + delta))
        vocab.update_timestamp = time.time()
        self.save_vocabulary(vocab)
        return vocab

    def set_extra(self, user_id: str, word: str, extra: dict[str, str], mode: str = "overwrite") -> VocabularyRecord | None:
        """
        设置指定单词的 extra 字段。
        Args:
            user_id (str): 用户 ID / User ID.
            word (str): 单词 / Word.
            extra (dict[str, str]): 要设置或合并的 extra 字典 / Extra dictionary to set or merge.
            mode (str): 'overwrite'（完全覆盖）或 'add'（合并）/ 'overwrite' (replace) or 'add' (merge).
        Returns:
            VocabularyRecord | None: 更新后的词汇记录或 None / Updated vocabulary record or None if not found.
        """
        vocab = self.get_vocabulary(user_id, word)
        if not vocab:
            return None
        if mode == "add":
            if vocab.extra is None:
                vocab.extra = extra.copy()
            else:
                vocab.extra.update(extra)
        else:  # overwrite
            vocab.extra = extra.copy()
        vocab.update_timestamp = time.time()
        self.save_vocabulary(vocab)
        return vocab

    def clear_all(self) -> None:
        """
        清空数据库中的所有词汇记录。
        Returns:
            None
        """
        pattern = f"{self.VOCAB_PREFIX}:*"
        keys = list(self.client.scan_iter(pattern))
        if keys:
            self.client.delete(*keys)
        return True

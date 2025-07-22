import os
import pytest
import time
from schemas.vocabulary import VocabularyRecord, DifficultyLevel
from db.vocabulary_db import VocabularyDB
from dotenv import load_dotenv

load_dotenv()


@pytest.fixture(scope="function")
def vocab_db():
    """Fixture: 每个测试用例前后清理 Redis，保证测试隔离。"""
    db = VocabularyDB(os.environ.get("TEST_REDIS_URL"))
    # 清理所有以 vocab: 开头的 key
    for key in db.client.keys("vocab:*"):
        db.client.delete(key)
    yield db
    for key in db.client.keys("vocab:*"):
        db.client.delete(key)


def test_save_and_get_vocabulary(vocab_db):
    """测试保存和获取单词功能。"""
    user_id = "user123"
    ts = time.time()
    vocab = VocabularyRecord(
        word="apple", 
        explanation="A fruit", 
        user_id=user_id, 
        create_timestamp=ts, 
        familiarity=3, 
        difficulty_level=DifficultyLevel.BEGINNER,
        example_sentences=["I eat an apple.", "The apple is red."],
        extra={"part_of_speech": "noun"}
    )
    assert vocab_db.save_vocabulary(vocab) is True
    result = vocab_db.get_vocabulary(user_id, "apple")
    assert isinstance(result, VocabularyRecord)
    assert result.word == "apple"
    assert result.explanation == "A fruit"
    assert result.user_id == user_id
    assert abs(result.create_timestamp - ts) < 1  # 时间戳误差容忍1秒
    assert result.extra["part_of_speech"] == "noun"
    assert result.familiarity == 3
    assert result.difficulty_level == DifficultyLevel.BEGINNER
    assert result.example_sentences == ["I eat an apple.", "The apple is red."]

def test_update_familiarity(vocab_db):
    """测试熟悉度增减和边界。"""
    user_id = "user_fam"
    vocab = VocabularyRecord(word="testword", explanation="test", user_id=user_id, create_timestamp=0, familiarity=5, extra=None)
    vocab_db.save_vocabulary(vocab)
    # 增加熟悉度
    updated = vocab_db.update_familiarity(user_id, "testword", 3)
    assert updated.familiarity == 8
    # 超过上限
    updated = vocab_db.update_familiarity(user_id, "testword", 5)
    assert updated.familiarity == 10
    # 降低熟悉度
    updated = vocab_db.update_familiarity(user_id, "testword", -7)
    assert updated.familiarity == 3
    # 低于下限
    updated = vocab_db.update_familiarity(user_id, "testword", -10)
    assert updated.familiarity == 0
    # 不存在的词
    assert vocab_db.update_familiarity(user_id, "notfound", 1) is None




def test_delete_vocabulary(vocab_db):
    """测试删除单词功能。"""
    user_id = "user_del"
    vocab = VocabularyRecord(word="banana", explanation="A fruit", user_id=user_id, create_timestamp=time.time(), familiarity=2, extra=None)
    vocab_db.save_vocabulary(vocab)
    assert vocab_db.get_vocabulary(user_id, "banana") is not None
    assert vocab_db.delete_vocabulary(user_id, "banana") == 1
    assert vocab_db.get_vocabulary(user_id, "banana") is None

def test_get_all_words_by_user(vocab_db):
    """测试获取用户所有单词功能。"""
    user_id = "user_list"
    vocab1 = VocabularyRecord(word="cat", explanation="An animal", user_id=user_id, create_timestamp=time.time(), familiarity=1, extra=None)
    vocab2 = VocabularyRecord(word="dog", explanation="Another animal", user_id=user_id, create_timestamp=time.time(), familiarity=2, extra=None)
    vocab_db.save_vocabulary(vocab1)
    vocab_db.save_vocabulary(vocab2)
    words = vocab_db.get_all_words_by_user(user_id)
    word_set = set([v.word for v in words])
    assert "cat" in word_set and "dog" in word_set
    assert all(isinstance(v, VocabularyRecord) for v in words)

def test_get_words_by_user_and_time(vocab_db):
    """测试按时间范围获取单词功能。"""
    user_id = "user_time"
    now = time.time()
    vocab1 = VocabularyRecord(word="early", explanation="Early word", user_id=user_id, create_timestamp=now-100, familiarity=4, extra=None)
    vocab2 = VocabularyRecord(word="late", explanation="Late word", user_id=user_id, create_timestamp=now+100, familiarity=5, extra=None)
    vocab_db.save_vocabulary(vocab1)
    vocab_db.save_vocabulary(vocab2)
    results = vocab_db.get_words_by_user_and_time(user_id, now-200, now)
    assert any(v.word == "early" for v in results)
    assert all(v.create_timestamp <= now for v in results)
    # 边界：无结果
    empty = vocab_db.get_words_by_user_and_time(user_id, now+200, now+300)
    assert empty == []

def test_get_nonexistent_vocabulary(vocab_db):
    """测试获取不存在的单词返回 None。"""
    result = vocab_db.get_vocabulary("user123", "nonexistent")
    assert result is None

def test_set_extra_overwrite_and_add(vocab_db):
    """测试 set_extra 的 add/overwrite 模式。"""
    user_id = "user_extra"
    vocab = VocabularyRecord(word="extraword", explanation="extra", user_id=user_id, create_timestamp=0, familiarity=1, extra={"a": "1"})
    vocab_db.save_vocabulary(vocab)
    # 覆盖模式
    updated = vocab_db.set_extra(user_id, "extraword", {"b": "2"}, mode="overwrite")
    assert updated.extra == {"b": "2"}
    # 合并模式
    updated = vocab_db.set_extra(user_id, "extraword", {"c": "3"}, mode="add")
    assert updated.extra == {"b": "2", "c": "3"}
    # extra 为 None 时 add
    vocab2 = VocabularyRecord(word="noneword", explanation="none", user_id=user_id, create_timestamp=0, familiarity=1, extra=None)
    vocab_db.save_vocabulary(vocab2)
    updated2 = vocab_db.set_extra(user_id, "noneword", {"x": "y"}, mode="add")
    assert updated2.extra == {"x": "y"}
    # 不存在的词
    assert vocab_db.set_extra(user_id, "notfound", {"a": "b"}) is None

def test_delete_vocabulary_not_exist(vocab_db):
    """测试删除不存在的单词返回 0。"""
    user_id = "user_del2"
    assert vocab_db.delete_vocabulary(user_id, "notfound") == 0

def test_get_all_words_by_user_empty(vocab_db):
    """测试获取用户所有单词为空时返回空列表。"""
    user_id = "user_empty"
    words = vocab_db.get_all_words_by_user(user_id)
    assert words == []
    assert isinstance(words, list)


def test_vocabularydb_init_without_url(monkeypatch):
    """测试未设置 REDIS_URL 时抛出异常。"""
    monkeypatch.delenv("REDIS_URL", raising=False)
    with pytest.raises(ValueError):
        VocabularyDB(redis_url=None)

def test_get_words_by_difficulty(vocab_db):
    """测试按难度级别获取词汇。"""
    user_id = "user_by_difficulty"
    
    # 创建不同难度的词汇
    vocab1 = VocabularyRecord(
        word="easy", 
        explanation="Simple", 
        user_id=user_id, 
        create_timestamp=time.time(), 
        familiarity=5,
        difficulty_level=DifficultyLevel.BEGINNER
    )
    vocab2 = VocabularyRecord(
        word="moderate", 
        explanation="Medium", 
        user_id=user_id, 
        create_timestamp=time.time(), 
        familiarity=3,
        difficulty_level=DifficultyLevel.INTERMEDIATE
    )
    vocab3 = VocabularyRecord(
        word="complex", 
        explanation="Complicated", 
        user_id=user_id, 
        create_timestamp=time.time(), 
        familiarity=1,
        difficulty_level=DifficultyLevel.ADVANCED
    )
    vocab4 = VocabularyRecord(
        word="simple", 
        explanation="Easy", 
        user_id=user_id, 
        create_timestamp=time.time(), 
        familiarity=6,
        difficulty_level=DifficultyLevel.BEGINNER
    )
    
    vocab_db.save_vocabulary(vocab1)
    vocab_db.save_vocabulary(vocab2)
    vocab_db.save_vocabulary(vocab3)
    vocab_db.save_vocabulary(vocab4)
    
    # 测试获取初级词汇
    beginner_words = vocab_db.get_words_by_difficulty(user_id, DifficultyLevel.BEGINNER)
    assert len(beginner_words) == 2
    word_set = {v.word for v in beginner_words}
    assert "easy" in word_set and "simple" in word_set
    
    # 测试获取中级词汇
    intermediate_words = vocab_db.get_words_by_difficulty(user_id, DifficultyLevel.INTERMEDIATE)
    assert len(intermediate_words) == 1
    assert intermediate_words[0].word == "moderate"
    
    # 测试获取高级词汇
    advanced_words = vocab_db.get_words_by_difficulty(user_id, DifficultyLevel.ADVANCED)
    assert len(advanced_words) == 1
    assert advanced_words[0].word == "complex"
    
    # 测试获取专家级词汇（应该为空）
    expert_words = vocab_db.get_words_by_difficulty(user_id, DifficultyLevel.EXPERT)
    assert len(expert_words) == 0


def test_vocabulary_with_default_values(vocab_db):
    """测试词汇记录的默认值。"""
    user_id = "user_defaults"
    vocab = VocabularyRecord(
        word="default", 
        explanation="Test default values", 
        user_id=user_id, 
        create_timestamp=time.time()
    )
    vocab_db.save_vocabulary(vocab)
    
    result = vocab_db.get_vocabulary(user_id, "default")
    assert result is not None
    assert result.familiarity == 0  # 默认值
    assert result.difficulty_level == DifficultyLevel.INTERMEDIATE  # 默认值
    assert result.example_sentences == []  # 默认值
    assert result.extra is None  # 默认值

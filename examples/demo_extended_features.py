#!/usr/bin/env python3
"""
演示扩展后的词汇功能：difficulty_level 和 example_sentences
"""

import os
import time
from schemas.vocabulary import VocabularyRecord, DifficultyLevel
from db.vocabulary_db import VocabularyDB
from dotenv import load_dotenv

load_dotenv(override=True)

def demo_extended_features():
    """演示新增的字段和功能"""
    
    # 初始化数据库连接
    vocab_db = VocabularyDB(os.getenv("TEST_REDIS_URL"))
    user_id = "demo_user"
    
    print("=== iLanguage 词汇扩展功能演示 ===\n")
    
    # 1. 创建带有新字段的词汇记录
    print("1. 创建带有难度级别和例句的词汇记录")
    vocab1 = VocabularyRecord(
        word="serendipity",
        explanation="The occurrence of events by chance in a happy way",
        user_id=user_id,
        create_timestamp=time.time(),
        familiarity=2,
        difficulty_level=DifficultyLevel.ADVANCED,
        example_sentences=[
            "Finding that book was pure serendipity.",
            "The discovery happened by serendipity."
        ]
    )
    vocab_db.save_vocabulary(vocab1)
    print(f"✓ 保存词汇: {vocab1.word} (难度: {vocab1.difficulty_level.value})")
    print(f"  例句数量: {len(vocab1.example_sentences)}")
    
    vocab2 = VocabularyRecord(
        word="hello",
        explanation="A greeting",
        user_id=user_id,
        create_timestamp=time.time(),
        familiarity=8,
        difficulty_level=DifficultyLevel.BEGINNER,
        example_sentences=["Hello, how are you?"]
    )
    vocab_db.save_vocabulary(vocab2)
    print(f"✓ 保存词汇: {vocab2.word} (难度: {vocab2.difficulty_level.value})")
    
    vocab3 = VocabularyRecord(
        word="sophisticated",
        explanation="Having great knowledge or experience",
        user_id=user_id,
        create_timestamp=time.time(),
        familiarity=4,
        difficulty_level=DifficultyLevel.INTERMEDIATE
    )
    vocab_db.save_vocabulary(vocab3)
    print(f"✓ 保存词汇: {vocab3.word} (难度: {vocab3.difficulty_level.value})")
    
    print("\n" + "="*50 + "\n")
    
    # 2. 演示设置难度级别
    print("2. 动态设置词汇难度级别")
    updated = vocab_db.set_difficulty_level(user_id, "sophisticated", DifficultyLevel.ADVANCED)
    if updated:
        print(f"✓ 将 '{updated.word}' 难度级别更新为: {updated.difficulty_level.value}")
    
    print("\n" + "="*50 + "\n")
    
    # 3. 演示例句操作
    print("3. 例句管理功能")
    
    # 添加单个例句
    vocab_db.add_example_sentence(user_id, "sophisticated", "She has a sophisticated taste in art.")
    print("✓ 为 'sophisticated' 添加例句")
    
    # 批量设置例句（覆盖模式）
    new_sentences = [
        "The software has a sophisticated interface.",
        "He gave a sophisticated analysis of the problem."
    ]
    vocab_db.set_example_sentences(user_id, "sophisticated", new_sentences, mode="overwrite")
    print("✓ 批量设置例句（覆盖模式）")
    
    # 批量添加例句（追加模式）
    additional_sentences = [
        "The restaurant offers sophisticated cuisine.",
        "She wore a sophisticated black dress."
    ]
    vocab_db.set_example_sentences(user_id, "sophisticated", additional_sentences, mode="add")
    print("✓ 批量添加例句（追加模式）")
    
    print("\n" + "="*50 + "\n")
    
    # 4. 按难度级别查询词汇
    print("4. 按难度级别查询词汇")
    
    for difficulty in DifficultyLevel:
        words = vocab_db.get_words_by_difficulty(user_id, difficulty)
        print(f"{difficulty.value.upper()}: {len(words)} 个词汇")
        for word in words:
            print(f"  - {word.word} (熟悉度: {word.familiarity}/10)")
    
    print("\n" + "="*50 + "\n")
    
    # 5. 查看完整的词汇信息
    print("5. 查看完整词汇信息")
    
    result = vocab_db.get_vocabulary(user_id, "sophisticated")
    if result:
        print(f"词汇: {result.word}")
        print(f"解释: {result.explanation}")
        print(f"难度级别: {result.difficulty_level.value}")
        print(f"熟悉度: {result.familiarity}/10")
        print(f"例句数量: {len(result.example_sentences)}")
        print("例句:")
        for i, sentence in enumerate(result.example_sentences, 1):
            print(f"  {i}. {sentence}")
    
    print("\n" + "="*50 + "\n")
    
    # 6. 统计信息
    print("6. 用户词汇统计")
    all_words = vocab_db.get_all_words_by_user(user_id)
    print(f"总词汇数: {len(all_words)}")
    
    # 按难度分组统计
    difficulty_stats = {}
    total_sentences = 0
    for word in all_words:
        difficulty = word.difficulty_level
        if difficulty not in difficulty_stats:
            difficulty_stats[difficulty] = 0
        difficulty_stats[difficulty] += 1
        total_sentences += len(word.example_sentences)
    
    print("难度分布:")
    for difficulty, count in difficulty_stats.items():
        print(f"  {difficulty.value}: {count} 个")
    
    print(f"总例句数: {total_sentences}")
    
    # 清理演示数据
    print("\n" + "="*50 + "\n")
    print("7. 清理演示数据")
    for word in all_words:
        vocab_db.delete_vocabulary(user_id, word.word)
        print(f"✓ 删除词汇: {word.word}")
    
    print("\n演示完成！")

if __name__ == "__main__":
    demo_extended_features()

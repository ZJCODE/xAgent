#!/usr/bin/env python3
"""
测试 DSL 同时支持 → 和 -> 两种箭头符号的功能
"""

import sys
import os

# Add the parent directory to the path so we can import xagent
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xagent.multi.workflow import parse_dependencies_dsl, validate_dsl_syntax


def test_both_arrow_types():
    """测试两种箭头符号的等效性"""
    print("🔬 Testing Both Arrow Types (→ and ->)")
    print("=" * 50)
    
    # 测试用例：使用不同箭头符号的等效 DSL
    test_cases = [
        # 简单依赖
        ("A→B", "A->B"),
        
        # 链式依赖
        ("A→B→C", "A->B->C"),
        
        # 并行分支
        ("A→B, A→C", "A->B, A->C"),
        
        # 多依赖合并
        ("A&B→C", "A&B->C"),
        
        # 复杂模式
        ("A→B, A→C, B&C→D", "A->B, A->C, B&C->D"),
        
        # 更复杂的模式
        ("research→analysis, research→planning, analysis&planning→synthesis→review", 
         "research->analysis, research->planning, analysis&planning->synthesis->review"),
        
        # 混合使用两种箭头（应该也能工作）
        ("A→B, B->C, C→D", "A->B, B→C, C->D"),
    ]
    
    print("1️⃣ Testing Equivalence")
    print("-" * 30)
    
    all_passed = True
    
    for i, (unicode_dsl, ascii_dsl) in enumerate(test_cases, 1):
        print(f"\nTest {i}:")
        print(f"  Unicode: '{unicode_dsl}'")
        print(f"  ASCII:   '{ascii_dsl}'")
        
        # 验证语法
        unicode_valid, unicode_error = validate_dsl_syntax(unicode_dsl)
        ascii_valid, ascii_error = validate_dsl_syntax(ascii_dsl)
        
        if not unicode_valid:
            print(f"  ❌ Unicode syntax error: {unicode_error}")
            all_passed = False
            continue
            
        if not ascii_valid:
            print(f"  ❌ ASCII syntax error: {ascii_error}")
            all_passed = False
            continue
        
        # 解析并比较结果
        unicode_result = parse_dependencies_dsl(unicode_dsl)
        ascii_result = parse_dependencies_dsl(ascii_dsl)
        
        if unicode_result == ascii_result:
            print(f"  ✅ Results match: {unicode_result}")
        else:
            print(f"  ❌ Results differ!")
            print(f"     Unicode result: {unicode_result}")
            print(f"     ASCII result:   {ascii_result}")
            all_passed = False
    
    print(f"\n2️⃣ Testing Mixed Arrow Usage")
    print("-" * 30)
    
    # 测试混合使用两种箭头
    mixed_cases = [
        "A→B, B->C",
        "A->B→C, C->D",
        "research→analysis, analysis->planning, planning→synthesis",
        "A->B, A→C, B&C->D",
    ]
    
    for mixed_dsl in mixed_cases:
        print(f"\nMixed: '{mixed_dsl}'")
        
        is_valid, error = validate_dsl_syntax(mixed_dsl)
        if not is_valid:
            print(f"  ❌ Syntax error: {error}")
            all_passed = False
            continue
        
        try:
            result = parse_dependencies_dsl(mixed_dsl)
            print(f"  ✅ Parsed successfully: {result}")
        except Exception as e:
            print(f"  ❌ Parse error: {e}")
            all_passed = False
    
    print(f"\n3️⃣ Testing Error Cases")
    print("-" * 30)
    
    # 测试错误用例
    error_cases = [
        "A->",       # 不完整的箭头
        "->B",       # 前面为空（这个应该是有效的）
        "A->B->",    # 末尾不完整
        "A&->B",     # 空依赖
        "A-->B",     # 错误的箭头格式
        "A->>B",     # 错误的箭头格式
    ]
    
    for error_dsl in error_cases:
        print(f"\nError test: '{error_dsl}'")
        is_valid, error_msg = validate_dsl_syntax(error_dsl)
        
        if error_dsl == "->B":  # 这个应该是有效的
            if is_valid:
                result = parse_dependencies_dsl(error_dsl)
                print(f"  ✅ Valid (root node): {result}")
            else:
                print(f"  ❌ Unexpected error: {error_msg}")
                all_passed = False
        else:
            if not is_valid:
                print(f"  ✅ Correctly rejected: {error_msg}")
            else:
                print(f"  ❌ Should have been rejected but was accepted")
                all_passed = False
    
    print(f"\n{'🎉 All tests passed!' if all_passed else '❌ Some tests failed'}")
    return all_passed


if __name__ == "__main__":
    print("Testing DSL support for both → and -> arrows...\n")
    success = test_both_arrow_types()
    
    if success:
        print("\n✅ Both arrow types work correctly!")
        print("Users can now use either → (Unicode) or -> (ASCII) arrows in their DSL strings.")
    else:
        print("\n❌ There are issues with the arrow support that need to be fixed.")

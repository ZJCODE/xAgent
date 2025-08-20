#!/usr/bin/env python3
"""
测试简化后的 DSL 功能，只支持 -> 箭头符号
"""

import sys
import os

# Add the parent directory to the path so we can import xagent
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xagent.multi.workflow import parse_dependencies_dsl, validate_dsl_syntax


def test_basic_functionality():
    """测试基本的 DSL 功能"""
    print("🔬 Testing Basic DSL Functionality (-> arrows only)")
    print("=" * 60)
    
    test_cases = [
        ("A->B", {"B": ["A"]}),
        ("A->B->C", {"B": ["A"], "C": ["B"]}),
        ("A->B, A->C", {"B": ["A"], "C": ["A"]}),
        ("A&B->C", {"C": ["A", "B"]}),
        ("A->B, A->C, B&C->D", {"B": ["A"], "C": ["A"], "D": ["B", "C"]}),
        ("research->analysis, research->planning, analysis&planning->synthesis->review", 
         {"analysis": ["research"], "planning": ["research"], "synthesis": ["analysis", "planning"], "review": ["synthesis"]}),
    ]
    
    all_passed = True
    
    for i, (dsl_string, expected) in enumerate(test_cases, 1):
        print(f"\nTest {i}: '{dsl_string}'")
        
        # Validate syntax
        is_valid, error_msg = validate_dsl_syntax(dsl_string)
        if not is_valid:
            print(f"  ❌ Syntax validation failed: {error_msg}")
            all_passed = False
            continue
        
        # Parse dependencies
        try:
            result = parse_dependencies_dsl(dsl_string)
            if result == expected:
                print(f"  ✅ Passed: {result}")
            else:
                print(f"  ❌ Failed: Expected {expected}, got {result}")
                all_passed = False
        except Exception as e:
            print(f"  ❌ Parse error: {e}")
            all_passed = False
    
    return all_passed


def test_unicode_arrow_rejection():
    """测试对 Unicode 箭头的拒绝"""
    print("\n🚫 Testing Unicode Arrow Rejection")
    print("=" * 60)
    
    unicode_cases = [
        "A→B",
        "A→B→C", 
        "A→B, A→C",
        "A&B→C",
    ]
    
    all_passed = True
    
    for case in unicode_cases:
        print(f"\nTesting rejection of: '{case}'")
        is_valid, error_msg = validate_dsl_syntax(case)
        if not is_valid:
            print(f"  ✅ Correctly rejected: {error_msg}")
        else:
            print(f"  ❌ Should have been rejected but was accepted")
            all_passed = False
    
    return all_passed


def test_error_cases():
    """测试错误情况"""
    print("\n⚠️  Testing Error Cases")
    print("=" * 60)
    
    error_cases = [
        "A->",          # Missing target
        "A->B->",       # Missing final target
        "A&->B",        # Empty dependency
        "A-->B",        # Double dash
        "A->>B",        # Double >
        "A<<-B",        # Wrong direction
    ]
    
    all_passed = True
    
    for case in error_cases:
        print(f"\nTesting error case: '{case}'")
        is_valid, error_msg = validate_dsl_syntax(case)
        if not is_valid:
            print(f"  ✅ Correctly rejected: {error_msg}")
        else:
            print(f"  ❌ Should have been rejected but was accepted")
            all_passed = False
    
    return all_passed


def test_edge_cases():
    """测试边界情况"""
    print("\n🎯 Testing Edge Cases")
    print("=" * 60)
    
    edge_cases = [
        ("", {}),                    # Empty string
        ("->B", {"B": []}),         # Root node (no dependencies)
        (" A -> B ", {"B": ["A"]}), # Whitespace handling
    ]
    
    all_passed = True
    
    for dsl_string, expected in edge_cases:
        print(f"\nTesting edge case: '{dsl_string}'")
        
        is_valid, error_msg = validate_dsl_syntax(dsl_string)
        if dsl_string and not is_valid:
            print(f"  ❌ Syntax validation failed: {error_msg}")
            all_passed = False
            continue
        
        try:
            result = parse_dependencies_dsl(dsl_string)
            if result == expected:
                print(f"  ✅ Passed: {result}")
            else:
                print(f"  ❌ Failed: Expected {expected}, got {result}")
                all_passed = False
        except Exception as e:
            print(f"  ❌ Parse error: {e}")
            all_passed = False
    
    return all_passed


def main():
    """运行所有测试"""
    print("Testing Simplified DSL (-> arrows only)...\n")
    
    results = []
    results.append(test_basic_functionality())
    results.append(test_unicode_arrow_rejection())
    results.append(test_error_cases())
    results.append(test_edge_cases())
    
    print("\n" + "=" * 60)
    print("📊 Test Summary")
    print("=" * 60)
    
    if all(results):
        print("✅ All tests passed! The simplified DSL is working correctly.")
        print("Users should now use only -> (ASCII) arrows in their DSL strings.")
        return True
    else:
        print("❌ Some tests failed")
        return False


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)

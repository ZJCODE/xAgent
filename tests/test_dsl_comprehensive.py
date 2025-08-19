#!/usr/bin/env python3
"""
DSL comprehensive test: verify all DSL functionality with ASCII arrows
"""

import asyncio
import sys
import os

# Add the parent directory to the path so we can import xagent
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xagent.core.agent import Agent
from xagent.multi.workflow import Workflow, parse_dependencies_dsl, validate_dsl_syntax


def test_dsl_comprehensive():
    """Comprehensive DSL functionality test"""
    print("🔬 Comprehensive DSL Test")
    print("=" * 60)
    
    # Test all supported patterns
    test_cases = [
        # Basic tests
        ("Simple", "A->B", {"B": ["A"]}),
        
        # Chain tests  
        ("Chain", "A->B->C", {"B": ["A"], "C": ["B"]}),
        
        # Parallel tests
        ("Parallel", "A->B, A->C", {"B": ["A"], "C": ["A"]}),
        
        # Multi-dependency tests
        ("Multi-dep", "A&B->C", {"C": ["A", "B"]}),
        
        # Complex pattern tests
        ("Complex", "A->B, A->C, B&C->D", {"B": ["A"], "C": ["A"], "D": ["B", "C"]}),
        
        # Real world examples
        ("Research flow", "research->analysis, research->planning, analysis&planning->synthesis", 
         {"analysis": ["research"], "planning": ["research"], "synthesis": ["analysis", "planning"]}),
    ]
    
    success_count = 0
    total_count = len(test_cases)
    
    for name, dsl, expected in test_cases:
        print(f"\n🧪 {name}")
        print(f"   DSL: '{dsl}'")
        
        # 验证语法
        is_valid, error = validate_dsl_syntax(dsl)
        if not is_valid:
            print(f"   ❌ Syntax error: {error}")
            continue
        
        # 解析并比较
        try:
            result = parse_dependencies_dsl(dsl)
            if result == expected:
                print(f"   ✅ Correct: {result}")
                success_count += 1
            else:
                print(f"   ❌ Wrong result:")
                print(f"      Expected: {expected}")
                print(f"      Got:      {result}")
        except Exception as e:
            print(f"   ❌ Parse error: {e}")
    
    print(f"\n📊 Test Results: {success_count}/{total_count} passed")
    
    # 测试错误情况
    print(f"\n🚫 Error Case Testing")
    print("-" * 30)
    
    error_cases = [
        ("Empty target", "A->", False),
        ("Invalid double dash", "A-->B", False),
        ("Invalid double arrow", "A->>B", False),
        ("Empty dependency", "A&->B", False),
        ("Root node", "->B", True),   # Should be valid
    ]
    
    error_success = 0
    for name, dsl, should_be_valid in error_cases:
        is_valid, error = validate_dsl_syntax(dsl)
        if is_valid == should_be_valid:
            status = "✅" if should_be_valid else "✅ (correctly rejected)"
            print(f"   {status} {name}: '{dsl}'")
            error_success += 1
        else:
            expected_status = "should be valid" if should_be_valid else "should be invalid"
            print(f"   ❌ {name}: '{dsl}' {expected_status} but got opposite")
    
    print(f"\n📊 Error Test Results: {error_success}/{len(error_cases)} passed")
    
    total_success = success_count + error_success
    total_tests = total_count + len(error_cases)
    
    print(f"\n🎯 Overall Results: {total_success}/{total_tests} tests passed")
    
    if total_success == total_tests:
        print("🎉 All tests passed! DSL support is working perfectly!")
        return True
    else:
        print("❌ Some tests failed. Please check the implementation.")
        return False


async def test_dsl_workflow_integration():
    """Test DSL workflow integration"""
    print("\n" + "=" * 60)
    print("🔗 DSL Workflow Integration Test")
    print("=" * 60)
    
    # Create simple test agents
    agent_a = Agent(name="agent_a", system_prompt="Test agent A")
    agent_b = Agent(name="agent_b", system_prompt="Test agent B")
    agent_c = Agent(name="agent_c", system_prompt="Test agent C")
    
    workflow = Workflow("dsl_integration_test")
    
    # 测试不同的箭头格式
    test_cases = [
        ("ASCII arrows", "agent_a->agent_b->agent_c"),
    ]
    
    print("Testing arrow formats in actual workflows:")
    
    for name, dsl in test_cases:
        print(f"\n🔬 {name}: '{dsl}'")
        
        try:
            # Validate syntax
            is_valid, error = validate_dsl_syntax(dsl)
            if not is_valid:
                print(f"   ❌ Syntax error: {error}")
                continue
            
            # Parse dependencies
            deps = parse_dependencies_dsl(dsl)
            print(f"   📋 Parsed dependencies: {deps}")
            
            # 创建工作流实例（不实际执行，避免需要 API 密钥）
            from xagent.multi.workflow import GraphWorkflow
            pattern = GraphWorkflow(
                agents=[agent_a, agent_b, agent_c],
                dependencies=dsl,  # 直接使用 DSL！
                name=f"test_{name.replace(' ', '_')}"
            )
            
            print(f"   ✅ GraphWorkflow created successfully with DSL")
            
        except Exception as e:
            print(f"   ❌ Integration error: {e}")
    
    print(f"\n🎉 DSL integration test completed!")


if __name__ == "__main__":
    print("Starting comprehensive DSL test...\n")
    
    # 运行解析测试
    parse_success = test_dsl_comprehensive()
    
    # 运行集成测试
    asyncio.run(test_dsl_workflow_integration())
    
    print("\n" + "=" * 60)
    if parse_success:
        print("✅ All comprehensive tests passed!")
        print("🎯 DSL now supports ASCII arrows (->)!")
        print("📚 Check the updated documentation for usage examples.")
    else:
        print("❌ Some tests failed. Please review the implementation.")

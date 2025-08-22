"""
Test script to verify AgentHTTPServer agent passing functionality.
"""

import asyncio
from xagent.core.agent import Agent
from xagent.interfaces.server import AgentHTTPServer
from xagent.components import MessageStorageLocal


def test_agent_passing():
    """Test that AgentHTTPServer can accept pre-configured Agent instances."""
    
    print("Testing AgentHTTPServer with custom Agent...")
    
    # Create a simple test agent
    test_agent = Agent(
        name="TestAgent",
        system_prompt="You are a test agent for verification purposes.",
        model="gpt-4o-mini",
        tools=[],  # No tools for simplicity
        message_storage=MessageStorageLocal()
    )
    
    # Test 1: Direct constructor with agent parameter
    print("Test 1: Direct constructor with agent parameter")
    try:
        server1 = AgentHTTPServer(agent=test_agent)
        assert server1.agent.name == "TestAgent"
        assert server1.agent.model == "gpt-4o-mini"
        print("✅ Direct constructor test passed")
    except Exception as e:
        print(f"❌ Direct constructor test failed: {e}")
        return False
    
    # Test 2: Verify that from_agent method is removed
    print("Test 2: Verify from_agent method removal")
    try:
        # This should raise AttributeError since we removed the method
        hasattr(AgentHTTPServer, 'from_agent')
        if not hasattr(AgentHTTPServer, 'from_agent'):
            print("✅ from_agent method successfully removed")
        else:
            print("❌ from_agent method still exists")
            return False
    except Exception as e:
        print(f"❌ from_agent method test failed: {e}")
        return False
    
    # Test 3: Verify FastAPI app is created
    print("Test 3: FastAPI app creation")
    try:
        assert server1.app is not None
        print("✅ FastAPI app creation test passed")
    except Exception as e:
        print(f"❌ FastAPI app creation test failed: {e}")
        return False
    
    # Test 4: Verify server configuration
    print("Test 4: Server configuration")
    try:
        assert "server" in server1.config
        print("✅ Server configuration test passed")
    except Exception as e:
        print(f"❌ Server configuration test failed: {e}")
        return False
    
    print("\n🎉 All tests passed! AgentHTTPServer agent passing functionality works correctly.")
    return True


def test_traditional_approach():
    """Test that traditional config-based approach still works."""
    
    print("\nTesting traditional config-based approach...")
    
    try:
        # This should still work as before
        server = AgentHTTPServer()  # Using default config
        assert server.agent is not None
        assert server.config is not None
        print("✅ Traditional approach test passed")
        return True
    except Exception as e:
        print(f"❌ Traditional approach test failed: {e}")
        return False


def main():
    """Run all tests."""
    print("AgentHTTPServer Agent Passing Functionality Tests")
    print("=" * 50)
    
    success = True
    success &= test_agent_passing()
    success &= test_traditional_approach()
    
    print("\n" + "=" * 50)
    if success:
        print("🎉 All tests passed! The implementation is working correctly.")
    else:
        print("❌ Some tests failed. Please check the implementation.")
    
    return success


if __name__ == "__main__":
    main()

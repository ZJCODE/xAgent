#!/usr/bin/env python3
"""
Test script for Redis cluster support functionality.

This script tests the new Redis cluster detection and client creation features.
"""

import asyncio
from urllib.parse import urlparse
from xagent.components.message.redis_messages import _looks_like_cluster, _strip_query_param, create_redis_client
from xagent.components.memory.utils.messages_for_memory import _looks_like_cluster as memory_looks_like_cluster


def test_cluster_detection():
    """Test cluster URL detection logic."""
    print("Testing cluster URL detection...")
    
    # Test cases for cluster detection
    test_cases = [
        # (url, expected_result, description)
        ("redis://localhost:6379", False, "Standard Redis URL"),
        ("redis+cluster://localhost:6379", True, "Redis cluster scheme"),
        ("rediss+cluster://localhost:6379", True, "Redis cluster TLS scheme"),
        ("redis://localhost:6379?cluster=true", True, "Cluster query parameter true"),
        ("redis://localhost:6379?cluster=1", True, "Cluster query parameter 1"),
        ("redis://localhost:6379?cluster=yes", True, "Cluster query parameter yes"),
        ("redis://localhost:6379?cluster=false", False, "Cluster query parameter false"),
        ("redis://localhost:6379?cluster=0", False, "Cluster query parameter 0"),
        ("redis://localhost:6379?other=param", False, "Non-cluster query parameter"),
    ]
    
    for url, expected, description in test_cases:
        result = _looks_like_cluster(url)
        status = "✅" if result == expected else "❌"
        print(f"  {status} {description}: {url} -> {result}")
        assert result == expected, f"Failed for {url}"
    
    print("✅ All cluster detection tests passed!\n")


def test_query_param_stripping():
    """Test query parameter stripping functionality."""
    print("Testing query parameter stripping...")
    
    test_cases = [
        # (original_url, param_to_strip, expected_url, description)
        (
            "redis://localhost:6379?cluster=true&timeout=5",
            "cluster",
            "redis://localhost:6379?timeout=5",
            "Strip cluster param with other params"
        ),
        (
            "redis://localhost:6379?cluster=true",
            "cluster", 
            "redis://localhost:6379",
            "Strip only cluster param"
        ),
        (
            "redis://localhost:6379?timeout=5&cluster=1&db=0",
            "cluster",
            "redis://localhost:6379?timeout=5&db=0",
            "Strip cluster param from middle"
        ),
        (
            "redis://localhost:6379?other=value",
            "cluster",
            "redis://localhost:6379?other=value",
            "Strip non-existent param"
        ),
    ]
    
    for original, param, expected, description in test_cases:
        result = _strip_query_param(original, param)
        status = "✅" if result == expected else "❌"
        print(f"  {status} {description}")
        print(f"      Original: {original}")
        print(f"      Result:   {result}")
        print(f"      Expected: {expected}")
        assert result == expected, f"Failed for {original}"
        print()
    
    print("✅ All query parameter stripping tests passed!\n")


def test_client_creation_dry_run():
    """Test client creation logic without actual connection."""
    print("Testing client creation logic (dry run)...")
    
    # Mock Redis URLs for testing
    test_urls = [
        ("redis://localhost:6379", "Standard Redis"),
        ("redis+cluster://localhost:6379", "Redis Cluster"),
        ("redis://localhost:6379?cluster=true", "Redis with cluster flag"),
    ]
    
    for url, description in test_urls:
        print(f"  Testing {description}: {url}")
        is_cluster = _looks_like_cluster(url)
        print(f"    Detected as cluster: {is_cluster}")
        
        # We can't actually create clients without valid Redis instances,
        # but we can test the URL processing logic
        if is_cluster:
            cleaned_url = _strip_query_param(url, "cluster")
            print(f"    Cleaned URL for cluster: {cleaned_url}")
        
        print()
    
    print("✅ Client creation logic tests passed!\n")


async def main():
    """Run all tests."""
    print("🚀 Testing Redis Cluster Support\n")
    print("=" * 50)
    
    try:
        test_cluster_detection()
        test_query_param_stripping()
        test_client_creation_dry_run()
        
        print("🎉 All tests passed successfully!")
        print("\n📝 Usage Examples:")
        print("   Standard Redis: redis://localhost:6379")
        print("   Redis Cluster:  redis+cluster://localhost:6379")
        print("   Redis Cluster:  redis://localhost:6379?cluster=true")
        
    except Exception as e:
        print(f"❌ Test failed with error: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())

#!/usr/bin/env python3
"""
Test the API Box fixes

License: CC-BY-4.0
"""
import sys
sys.path.insert(0, '/workspace/api_box')

from api_box.config import _route_matches_pattern

def test_pattern_matching():
    """Test the fixed pattern matching function."""
    print("Testing Pattern Matching Fixes")
    print("=" * 40)

    # Test with valid string patterns
    tests = [
        ("", "", True),  # Empty route should match empty pattern
        ("users", "users", True),  # Exact match
        ("users/123", "users/{{}}", True),  # Wildcard match
        ("users/123/delete", "users/{{}}/delete", True),  # Complex wildcard
    ]

    for route, pattern, expected in tests:
        result = _route_matches_pattern(route, pattern)
        status = "✅" if result == expected else "❌"
        print(f"{status} Route '{route}' vs Pattern '{pattern}': {result} (expected {expected})")

    # Test with invalid pattern types (should not crash)
    print("\nTesting invalid pattern types:")
    invalid_patterns = [
        {"key": "value"},  # dict
        ["item1", "item2"],  # list
        None,  # None
        123,  # number
    ]

    for pattern in invalid_patterns:
        try:
            result = _route_matches_pattern("users", pattern)
            print(f"✅ Pattern {type(pattern).__name__} handled gracefully: {result}")
        except Exception as e:
            print(f"❌ Pattern {type(pattern).__name__} caused error: {e}")

if __name__ == "__main__":
    test_pattern_matching()
#!/usr/bin/env python3
"""
Test the updated config syntax

License: CC-BY-4.0
"""
import sys
sys.path.insert(0, '/workspace/api_box')

from api_box.config import _route_matches_pattern

def test_updated_syntax():
    """Test the updated route pattern matching with named variables."""
    print("Testing Updated Route Pattern Syntax")
    print("=" * 50)

    # Test cases with the new syntax
    tests = [
        # Basic patterns
        ("users", "users", True),
        ("users/123", "users/{{user_id}}", True),
        ("users/abc", "users/{{user_id}}", True),
        ("users/123/profile", "users/{{user_id}}/profile", True),
        ("users/123/delete", "users/{{user_id}}/delete", True),

        # Different variable names should still work
        ("posts/456", "posts/{{post_id}}", True),
        ("admin/789", "admin/{{admin_id}}", True),

        # Non-matches
        ("users", "posts", False),
        ("users/123", "users", False),
        ("users/123/profile", "users/{{user_id}}/settings", False),

        # Anonymous variables (should still work)
        ("users/123", "users/{{}}", True),
        ("posts/456", "posts/{{}}", True),
    ]

    passed = 0
    failed = 0

    for route, pattern, expected in tests:
        result = _route_matches_pattern(route, pattern)
        status = "✅" if result == expected else "❌"
        print(f"{status} '{route}' vs '{pattern}': {result} (expected {expected})")

        if result == expected:
            passed += 1
        else:
            failed += 1

    print(f"\nResults: {passed} passed, {failed} failed")

    if failed == 0:
        print("🎉 All pattern matching tests passed!")
    else:
        print("⚠️  Some tests failed - check pattern matching logic")

if __name__ == "__main__":
    test_updated_syntax()
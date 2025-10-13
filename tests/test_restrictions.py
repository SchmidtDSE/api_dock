#!/usr/bin/env python3
"""
Test script for route restrictions and allowed routes functionality.

License: BSD 3-Clause
"""

#
# IMPORTS
#
import sys
from pathlib import Path

# Add the api_dock package to the path for testing
sys.path.insert(0, str(Path(__file__).parent.parent))

from api_dock.config import is_route_allowed, load_main_config

#
# CONSTANTS
#
CONFIG_PATH = "config/config.yaml"

#
# PUBLIC
#
def test_route_restrictions():
    """Test route restriction functionality."""
    print("Testing route restrictions...")

    # Load main config
    try:
        config = load_main_config(CONFIG_PATH)
        print("✓ Config loaded successfully")
    except Exception as e:
        print(f"✗ Failed to load config: {e}")
        return False

    # Test cases
    test_cases = [
        # Global restrictions (should block users/{}/delete for all remotes)
        ("users/123/delete", "remote_with_restrictions", False, "Global restriction"),
        ("users/456/delete", "remote_with_allowed_routes", False, "Global restriction"),

        # Remote-specific restrictions (remote_with_restrictions blocks users/{}/permissions)
        ("users/123/permissions", "remote_with_restrictions", False, "Remote-specific restriction"),
        ("users/123/permissions", "remote_with_allowed_routes", True, "Not restricted on this remote"),

        # Allowed routes (remote_with_allowed_routes only allows specific patterns)
        ("users", "remote_with_allowed_routes", True, "Explicitly allowed"),
        ("users/123", "remote_with_allowed_routes", True, "Explicitly allowed"),
        ("users/123/profile", "remote_with_allowed_routes", True, "Explicitly allowed"),
        ("users/123/settings", "remote_with_allowed_routes", False, "Not in allowed list"),
        ("admin/dashboard", "remote_with_allowed_routes", False, "Not in allowed list"),

        # Custom mapping remote (should respect restrictions)
        ("users/123/permissions", "remote_with_custom_mapping", True, "Should be allowed"),
        ("users/123/delete", "remote_with_custom_mapping", False, "Should be restricted"),

        # Admin routes (should be blocked for remote_with_restrictions only)
        ("admin/dashboard", "remote_with_restrictions", False, "Admin routes restricted"),
        ("admin/dashboard", "remote_with_custom_mapping", True, "Admin not restricted here"),
    ]

    passed = 0
    failed = 0

    for route, remote, expected, description in test_cases:
        result = is_route_allowed(route, config, remote)
        if result == expected:
            print(f"✓ {route} on {remote}: {result} ({description})")
            passed += 1
        else:
            print(f"✗ {route} on {remote}: expected {expected}, got {result} ({description})")
            failed += 1

    print(f"\nResults: {passed} passed, {failed} failed")
    return failed == 0


def main():
    """Run all tests."""
    print("=" * 60)
    print("API Dock Route Restrictions Test")
    print("=" * 60)

    success = test_route_restrictions()

    if success:
        print("\n✓ All tests passed!")
        return 0
    else:
        print("\n✗ Some tests failed!")
        return 1


if __name__ == "__main__":
    sys.exit(main())
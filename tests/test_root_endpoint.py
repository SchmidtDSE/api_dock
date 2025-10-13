#!/usr/bin/env python3
"""
Test script for API Dock root endpoint

License: BSD 3-Clause
"""
import sys
sys.path.insert(0, '/workspace/api_dock')

from api_dock.route_mapper import RouteMapper

def test_api_dock_metadata():
    """Test the enhanced metadata returned by the root endpoint."""
    print("Testing API Dock Enhanced Metadata")
    print("=" * 50)

    # Test with the main api_dock config
    print("\n1. Main API Dock Config:")
    print("-" * 30)
    route_mapper = RouteMapper("/workspace/api_dock/config/config.yaml")
    metadata = route_mapper.get_config_metadata()

    for key, value in metadata.items():
        print(f"{key}: {value}")

    # Test with the test project config
    print("\n2. Test Project Config:")
    print("-" * 30)
    route_mapper_test = RouteMapper("/workspace/api_dock_test_project/api_dock_config/config.yaml")
    metadata_test = route_mapper_test.get_config_metadata()

    for key, value in metadata_test.items():
        print(f"{key}: {value}")

    print("\n" + "=" * 50)
    print("Expected structure:")
    print("- name: API name")
    print("- description: API description")
    print("- authors: List of authors")
    print("- endpoints: List of non-remote endpoints")
    print("- remotes: List of remote API names")

if __name__ == "__main__":
    test_api_dock_metadata()
"""

Configuration Module for API Box

Handles loading and parsing of YAML configuration files for main API and remote APIs.

License: CC-BY-4.0

"""

#
# IMPORTS
#
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import yaml


#
# CONSTANTS
#
DEFAULT_CONFIG_DIR: str = "config"
DEFAULT_CONFIG_FILE: str = "config.yaml"
REMOTES_DIR: str = "remotes"


#
# PUBLIC
#
def load_main_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """Load the main configuration file.

    Args:
        config_path: Path to config file. If None, uses default path.

    Returns:
        Dictionary containing configuration data.

    Raises:
        FileNotFoundError: If config file doesn't exist.
        yaml.YAMLError: If config file is invalid YAML.
    """
    if config_path is None:
        config_path = os.path.join(DEFAULT_CONFIG_DIR, DEFAULT_CONFIG_FILE)

    return _load_yaml_file(config_path)


def find_remote_config(remote_name: str, main_config: Dict[str, Any], config_dir: Optional[str] = None) -> Dict[str, Any]:
    """Find and load configuration for a specific remote API by name.

    Args:
        remote_name: Name of the remote (from the name field in YAML).
        main_config: Main configuration dictionary.
        config_dir: Base config directory. If None, uses default.

    Returns:
        Dictionary containing remote configuration data.

    Raises:
        FileNotFoundError: If remote config file doesn't exist.
        yaml.YAMLError: If config file is invalid YAML.
    """
    # Get the mapping of remote names to config paths
    remote_mapping = get_remote_mapping(main_config, config_dir)

    if remote_name not in remote_mapping:
        raise FileNotFoundError(f"Remote '{remote_name}' not found in configuration")

    config_path = remote_mapping[remote_name]

    if config_path is None:
        # Handle inline configs (if we add support for them later)
        raise FileNotFoundError(f"Inline remote configs not yet supported for '{remote_name}'")

    return _load_yaml_file(config_path)


def find_remote_config_by_filename(remote_filename: str, config_dir: Optional[str] = None) -> Dict[str, Any]:
    """Find and load configuration for a specific remote API by filename (legacy).

    Args:
        remote_filename: Filename of the remote (e.g., "remote_1234").
        config_dir: Base config directory. If None, uses default.

    Returns:
        Dictionary containing remote configuration data.

    Raises:
        FileNotFoundError: If remote config file doesn't exist.
        yaml.YAMLError: If config file is invalid YAML.
    """
    if config_dir is None:
        config_dir = DEFAULT_CONFIG_DIR

    remote_config_path = os.path.join(config_dir, REMOTES_DIR, f"{remote_filename}.yaml")

    return _load_yaml_file(remote_config_path)


def get_remote_mapping(config: Dict[str, Any], config_dir: Optional[str] = None) -> Dict[str, str]:
    """Create mapping from remote names to config file paths.

    Args:
        config: Main configuration dictionary.
        config_dir: Base config directory. If None, uses default.

    Returns:
        Dictionary mapping remote names to their config file paths.
    """
    if config_dir is None:
        config_dir = DEFAULT_CONFIG_DIR

    remotes = config.get("remotes", [])
    remote_mapping = {}

    for remote in remotes:
        if isinstance(remote, str):
            # String format: use filename as fallback
            filename = remote
            config_path = os.path.join(config_dir, REMOTES_DIR, f"{filename}.yaml")

            # Try to load the config to get the actual name
            try:
                remote_config = _load_yaml_file(config_path)
                actual_name = remote_config.get("name", filename)
                remote_mapping[actual_name] = config_path
            except (FileNotFoundError, Exception):
                # If config can't be loaded, use filename as name
                remote_mapping[filename] = config_path

        elif isinstance(remote, dict) and "name" in remote:
            # Dict format: use the name directly
            remote_mapping[remote["name"]] = None  # Handle inline configs later if needed

    return remote_mapping


def get_remote_names(config: Dict[str, Any], config_dir: Optional[str] = None) -> List[str]:
    """Extract list of remote names from main config.

    Args:
        config: Main configuration dictionary.
        config_dir: Base config directory. If None, uses default.

    Returns:
        List of remote names.
    """
    return list(get_remote_mapping(config, config_dir).keys())


def is_route_allowed(route: str, config: Dict[str, Any], remote_name: Optional[str] = None) -> bool:
    """Check if a route is allowed based on configuration restrictions.

    Args:
        route: The route to check (e.g., "users/123/delete").
        config: Main configuration dictionary.
        remote_name: Name of the remote API (for remote-specific restrictions).

    Returns:
        True if route is allowed, False otherwise.
    """
    # Check global restrictions
    global_restricted = config.get("restricted", [])
    global_routes = config.get("routes", [])

    # Check remote-specific restrictions
    remote_restricted = []
    remote_routes = []

    if remote_name:
        remotes = config.get("remotes", [])
        for remote in remotes:
            if isinstance(remote, dict):
                if remote.get("name") == remote_name:
                    remote_restricted = remote.get("restricted", [])
                    remote_routes = remote.get("routes", [])
                    break

    # If explicit routes are defined (whitelist), check against them
    allowed_routes = remote_routes or global_routes
    if allowed_routes:
        return _route_matches_patterns(route, allowed_routes)

    # Otherwise, check against restricted patterns (blacklist)
    restricted_routes = remote_restricted or global_restricted
    if restricted_routes:
        return not _route_matches_patterns(route, restricted_routes)

    # If no restrictions, allow all routes
    return True


#
# INTERNAL
#
def _load_yaml_file(file_path: str) -> Dict[str, Any]:
    """Load a YAML file and return its contents.

    Args:
        file_path: Path to the YAML file.

    Returns:
        Dictionary containing YAML data.

    Raises:
        FileNotFoundError: If file doesn't exist.
        yaml.YAMLError: If file is invalid YAML.
    """
    try:
        with open(file_path, 'r') as file:
            return yaml.safe_load(file) or {}
    except FileNotFoundError:
        raise FileNotFoundError(f"Configuration file not found: {file_path}")
    except yaml.YAMLError as e:
        raise yaml.YAMLError(f"Invalid YAML in {file_path}: {e}")


def _route_matches_patterns(route: str, patterns: List[str]) -> bool:
    """Check if a route matches any of the given patterns.

    Args:
        route: The route to check.
        patterns: List of patterns to match against.

    Returns:
        True if route matches any pattern, False otherwise.
    """
    for pattern in patterns:
        if _route_matches_pattern(route, pattern):
            return True
    return False


def _route_matches_pattern(route: str, pattern: str) -> bool:
    """Check if a route matches a specific pattern.

    Patterns use <> as wildcards for path segments.
    Examples:
        - "<>/delete" matches "users/123/delete"
        - "<>" matches "users/123"
        - "users/<>/permissions" matches "users/123/permissions"

    Args:
        route: The route to check.
        pattern: The pattern to match against.

    Returns:
        True if route matches pattern, False otherwise.
    """
    route_parts = route.strip("/").split("/")
    pattern_parts = pattern.strip("/").split("/")

    if len(route_parts) != len(pattern_parts):
        return False

    for route_part, pattern_part in zip(route_parts, pattern_parts):
        if pattern_part != "<>" and pattern_part != route_part:
            return False

    return True
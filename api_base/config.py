"""

Configuration Module for API Base

Handles loading and parsing of YAML configuration files for main API and remote APIs.

License: BSD 3-Clause

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
DEFAULT_CONFIG_DIR: str = "api_base_config"
DEFAULT_CONFIG_FILE: str = "config.yaml"
REMOTES_DIR: str = "remotes"
DATABASES_DIR: str = "databases"


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


def find_remote_config(remote_name: str, main_config: Dict[str, Any], config_dir: Optional[str] = None, version: Optional[str] = None) -> Dict[str, Any]:
    """Find and load configuration for a specific remote API by name.

    Args:
        remote_name: Name of the remote (from the name field in YAML).
        main_config: Main configuration dictionary.
        config_dir: Base config directory. If None, uses default.
        version: Version string for versioned remotes (e.g., "0.1", "1.2"). If None, loads non-versioned or latest version.

    Returns:
        Dictionary containing remote configuration data.

    Raises:
        FileNotFoundError: If remote config file doesn't exist.
        yaml.YAMLError: If config file is invalid YAML.
    """
    if config_dir is None:
        config_dir = DEFAULT_CONFIG_DIR

    # Check if this is a versioned remote
    if is_versioned_remote(remote_name, main_config, config_dir):
        if version is None:
            raise FileNotFoundError(f"Remote '{remote_name}' is versioned - version parameter required")

        # Load versioned config
        config_path = os.path.join(config_dir, REMOTES_DIR, remote_name, f"{version}.yaml")
        return _load_yaml_file(config_path)

    # Non-versioned remote - use the regular mapping
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


def get_database_names(config: Dict[str, Any]) -> List[str]:
    """Extract list of database names from main config.

    Args:
        config: Main configuration dictionary.

    Returns:
        List of database names.
    """
    databases = config.get("databases", [])
    database_names = []

    for database in databases:
        if isinstance(database, str):
            database_names.append(database)
        elif isinstance(database, dict) and "name" in database:
            database_names.append(database["name"])

    return database_names


def is_versioned_remote(remote_name: str, main_config: Dict[str, Any], config_dir: Optional[str] = None) -> bool:
    """Check if a remote has versioned configurations.

    Args:
        remote_name: Name of the remote.
        main_config: Main configuration dictionary.
        config_dir: Base config directory. If None, uses default.

    Returns:
        True if remote has versioned configs (is a directory), False otherwise.
    """
    if config_dir is None:
        config_dir = DEFAULT_CONFIG_DIR

    remote_dir = os.path.join(config_dir, REMOTES_DIR, remote_name)
    return os.path.isdir(remote_dir)


def get_remote_versions(remote_name: str, main_config: Dict[str, Any], config_dir: Optional[str] = None) -> List[str]:
    """Get list of available versions for a versioned remote.

    Args:
        remote_name: Name of the remote.
        main_config: Main configuration dictionary.
        config_dir: Base config directory. If None, uses default.

    Returns:
        List of version strings (e.g., ["0.1", "0.2", "1.2"]).
        Returns empty list if remote is not versioned.
    """
    if config_dir is None:
        config_dir = DEFAULT_CONFIG_DIR

    if not is_versioned_remote(remote_name, main_config, config_dir):
        return []

    remote_dir = os.path.join(config_dir, REMOTES_DIR, remote_name)
    versions = []

    for filename in os.listdir(remote_dir):
        if filename.endswith('.yaml'):
            version = filename[:-5]  # Remove .yaml extension
            versions.append(version)

    return sorted(versions)


def resolve_latest_version(versions: List[str]) -> Optional[str]:
    """Resolve 'latest' to the highest version from a list.

    Args:
        versions: List of version strings.

    Returns:
        The latest version string, or None if list is empty.
    """
    if not versions:
        return None

    # Try to sort as floats
    try:
        float_versions = [(float(v), v) for v in versions]
        float_versions.sort(key=lambda x: x[0], reverse=True)
        return float_versions[0][1]
    except ValueError:
        # Fall back to string sorting
        sorted_versions = sorted(versions, reverse=True)
        return sorted_versions[0]


def is_route_allowed(route: str, config: Dict[str, Any], remote_name: Optional[str] = None, version: Optional[str] = None) -> bool:
    """Check if a route is allowed based on configuration restrictions.

    Args:
        route: The route to check (e.g., "users/123/delete").
        config: Main configuration dictionary.
        remote_name: Name of the remote API (for remote-specific restrictions).
        version: Version string for versioned remotes.

    Returns:
        True if route is allowed, False otherwise.
    """
    # Always allow empty route (root path) for API metadata access
    if not route or route == "":
        return True
    # Check global restrictions from main config
    global_restricted = config.get("restricted", [])
    global_routes = config.get("routes", [])

    # Check remote-specific restrictions from remote config file
    remote_restricted = []
    remote_routes = []

    if remote_name:
        try:
            # Load the remote config to check for restrictions/routes
            remote_config = find_remote_config(remote_name, config, version=version)
            remote_restricted = remote_config.get("restricted", [])
            remote_routes = remote_config.get("routes", [])
        except FileNotFoundError:
            # If remote config not found, just use global restrictions
            pass

    # If explicit routes are defined (whitelist), check against them
    # Remote-specific routes take precedence over global routes
    allowed_routes = remote_routes if remote_routes else global_routes
    if allowed_routes:
        return _route_matches_patterns(route, allowed_routes)

    # Otherwise, check against restricted patterns (blacklist)
    # Remote-specific restrictions take precedence over global restrictions
    restricted_routes = remote_restricted if remote_restricted else global_restricted
    if restricted_routes:
        return not _route_matches_patterns(route, restricted_routes)

    # If no restrictions, allow all routes
    return True


def find_route_mapping(full_route: str, method: str, remote_config: Dict[str, Any], remote_name: str) -> Optional[str]:
    """Find custom route mapping for a specific route and method.

    Args:
        full_route: The incoming route with remote name (e.g., "another_name/users/123/permissions").
        method: HTTP method (e.g., "GET", "POST").
        remote_config: Remote configuration dictionary.
        remote_name: Name of the remote for route_name substitution.

    Returns:
        Mapped remote route if found, None otherwise.
    """
    routes = remote_config.get("routes", [])

    for route_mapping in routes:
        if isinstance(route_mapping, dict):
            config_route = route_mapping.get("route", "")
            config_method = route_mapping.get("method", "").upper()
            remote_route = route_mapping.get("remote_route", "")

            # Check if method matches (case insensitive)
            if config_method and config_method != method.upper():
                continue

            # Replace {{route_name}} with actual remote name in the pattern
            pattern_with_name = config_route.replace("{{route_name}}", remote_name)

            # Check if route pattern matches and extract parameters
            params = _extract_route_params(full_route, pattern_with_name)
            if params is not None:
                # Add route_name to parameters for substitution
                params["route_name"] = remote_name
                # Substitute parameters in remote_route
                return _substitute_route_params(remote_route, params)

    return None


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

    Patterns use {{}} as wildcards for path segments.
    Examples:
        - "users/{{}}/delete" matches "users/123/delete"
        - "users/{{}}" matches "users/123"
        - "users/{{}}/permissions" matches "users/123/permissions"

    Args:
        route: The route to check.
        pattern: The pattern to match against.

    Returns:
        True if route matches pattern, False otherwise.
    """
    # Handle case where pattern is not a string (dict, list, etc.)
    if not isinstance(pattern, str):
        return False

    route_parts = route.strip("/").split("/")
    pattern_parts = pattern.strip("/").split("/")

    if len(route_parts) != len(pattern_parts):
        return False

    for route_part, pattern_part in zip(route_parts, pattern_parts):
        # Check if pattern part is a variable (starts and ends with double braces)
        if pattern_part.startswith("{{") and pattern_part.endswith("}}"):
            # Variable matches any value
            continue
        elif pattern_part != route_part:
            # Literal part must match exactly
            return False

    return True


def _extract_route_params(actual_route: str, pattern_route: str) -> Optional[Dict[str, str]]:
    """Extract parameters from an actual route using a pattern.

    Args:
        actual_route: The actual route (e.g., "users/123/permissions").
        pattern_route: The pattern route (e.g., "users/{user_id}/permissions").

    Returns:
        Dictionary of parameter mappings if match, None otherwise.
    """
    actual_parts = actual_route.strip("/").split("/")
    pattern_parts = pattern_route.strip("/").split("/")

    if len(actual_parts) != len(pattern_parts):
        return None

    params = {}
    for actual_part, pattern_part in zip(actual_parts, pattern_parts):
        if pattern_part.startswith("{") and pattern_part.endswith("}"):
            # Extract parameter name
            param_name = pattern_part[1:-1]
            params[param_name] = actual_part
        elif pattern_part != actual_part:
            # Fixed path segment doesn't match
            return None

    return params


def _substitute_route_params(template_route: str, params: Dict[str, str]) -> str:
    """Substitute parameters in a template route.

    Args:
        template_route: The template route (e.g., "user-permissions/{{user_id}}").
        params: Dictionary of parameter values.

    Returns:
        Route with substituted parameters.
    """
    result = template_route
    for param_name, param_value in params.items():
        # Handle both {param} and {{param}} formats
        result = result.replace(f"{{{param_name}}}", param_value)
        result = result.replace(f"{{{{{param_name}}}}}", param_value)
    return result
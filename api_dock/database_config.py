"""

Database Configuration Module for API Dock

Handles loading and parsing of database configuration files for SQL-based routes.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


#
# CONSTANTS
#
DATABASES_DIR: str = "databases"


#
# PUBLIC
#
def load_database_config(database_filename: str, config_dir: Optional[str] = None, version: Optional[str] = None) -> Dict[str, Any]:
    """Load a database configuration file.

    Args:
        database_filename: Name of the database config file (without .yaml extension).
        config_dir: Base config directory. If None, uses default.
        version: Version string for versioned databases (e.g., "0.1", "1.2"). If None, loads non-versioned database.

    Returns:
        Dictionary containing database configuration data.

    Raises:
        FileNotFoundError: If database config file doesn't exist.
        yaml.YAMLError: If config file is invalid YAML.
    """
    if config_dir is None:
        from api_dock.config import DEFAULT_CONFIG_DIR
        config_dir = DEFAULT_CONFIG_DIR

    # Check if this is a versioned database
    if version is not None or is_versioned_database(database_filename, config_dir):
        if version is None:
            raise FileNotFoundError(f"Database '{database_filename}' is versioned - version parameter required")

        # Load versioned config
        database_config_path = os.path.join(config_dir, DATABASES_DIR, database_filename, f"{version}.yaml")
    else:
        # Non-versioned database
        database_config_path = os.path.join(config_dir, DATABASES_DIR, f"{database_filename}.yaml")

    return _load_yaml_file(database_config_path)


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


def get_table_definition(table_name: str, database_config: Dict[str, Any]) -> Optional[str]:
    """Get the file path for a table from database configuration.

    Supports both string URIs and dict-based definitions:
    - String format: "table_name: s3://bucket/file.parquet"
    - Dict format: "table_name: {uri: s3://bucket/file.parquet, region: us-east-2}"

    Args:
        table_name: Name of the table.
        database_config: Database configuration dictionary.

    Returns:
        File path/URI for the table, or None if not found.
    """
    tables = database_config.get("tables", {})
    table_def = tables.get(table_name)

    # Handle both string and dict formats
    if isinstance(table_def, str):
        return table_def
    elif isinstance(table_def, dict):
        return table_def.get("uri") or table_def.get("path")
    else:
        return None


def get_table_metadata(table_name: str, database_config: Dict[str, Any]) -> Dict[str, Any]:
    """Get metadata for a table from database configuration.

    Returns metadata like region, auth headers, etc. if table is defined as a dict.

    Args:
        table_name: Name of the table.
        database_config: Database configuration dictionary.

    Returns:
        Dictionary containing table metadata (empty dict if table is string format).
        Possible keys: region, auth_headers, method, etc.
    """
    tables = database_config.get("tables", {})
    table_def = tables.get(table_name)

    if isinstance(table_def, dict):
        # Return all metadata except the URI/path itself
        metadata = {k: v for k, v in table_def.items() if k not in ['uri', 'path']}
        return metadata
    else:
        # String format has no metadata
        return {}


def get_named_query(query_name: str, database_config: Dict[str, Any]) -> Optional[str]:
    """Get a named query from database configuration.

    Args:
        query_name: Name of the query.
        database_config: Database configuration dictionary.

    Returns:
        Query SQL string, or None if not found.
    """
    queries = database_config.get("queries", {})
    return queries.get(query_name)


def is_versioned_database(database_name: str, config_dir: Optional[str] = None) -> bool:
    """Check if a database has versioned configurations.

    Args:
        database_name: Name of the database.
        config_dir: Base config directory. If None, uses default.

    Returns:
        True if database has versioned configs (is a directory), False otherwise.
    """
    if config_dir is None:
        from api_dock.config import DEFAULT_CONFIG_DIR
        config_dir = DEFAULT_CONFIG_DIR

    database_dir = os.path.join(config_dir, DATABASES_DIR, database_name)
    return os.path.isdir(database_dir)


def get_database_versions(database_name: str, config_dir: Optional[str] = None) -> List[str]:
    """Get list of available versions for a versioned database.

    Args:
        database_name: Name of the database.
        config_dir: Base config directory. If None, uses default.

    Returns:
        List of version strings (e.g., ["0.1", "0.2", "1.2"]).
        Returns empty list if database is not versioned.
    """
    if config_dir is None:
        from api_dock.config import DEFAULT_CONFIG_DIR
        config_dir = DEFAULT_CONFIG_DIR

    if not is_versioned_database(database_name, config_dir):
        return []

    database_dir = os.path.join(config_dir, DATABASES_DIR, database_name)
    versions = []

    for filename in os.listdir(database_dir):
        if filename.endswith('.yaml'):
            version = filename[:-5]  # Remove .yaml extension
            versions.append(version)

    return sorted(versions)


def resolve_latest_database_version(versions: List[str]) -> Optional[str]:
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


def find_database_route(path: str, database_config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Find a database route configuration that matches the given path.

    Args:
        path: The incoming route path (e.g., "users/123/permissions").
        database_config: Database configuration dictionary.

    Returns:
        Route configuration dict with 'route' and 'sql' keys, or None if not found.
    """
    routes = database_config.get("routes", [])

    for route_config in routes:
        if isinstance(route_config, dict):
            route_pattern = route_config.get("route", "")

            # Check if path matches the route pattern
            if _route_matches_pattern(path, route_pattern):
                return route_config

    return None


def validate_route_config(route_config: Dict[str, Any]) -> bool:
    """Validate route configuration with new declarative parameter format.

    Args:
        route_config: Route configuration dictionary.

    Returns:
        True if configuration is valid, False otherwise.
    """
    # Validate basic route structure
    if not isinstance(route_config, dict):
        return False

    # Check for required route field
    if 'route' not in route_config:
        return False

    # Validate query_params structure if present
    if 'query_params' in route_config:
        query_params = route_config['query_params']
        if not isinstance(query_params, list):
            return False

        for param_item in query_params:
            if not isinstance(param_item, dict):
                return False

            # Each parameter should have exactly one key (the parameter name)
            if len(param_item) != 1:
                return False

            param_name, param_config = next(iter(param_item.items()))

            # Validate parameter configuration structure
            if not isinstance(param_config, dict):
                return False

            # Check for valid configuration keys
            valid_keys = {'sql', 'response', 'conditional', 'action', 'required', 'default', 'missing_response'}
            if not any(key in param_config for key in valid_keys):
                return False

            # Validate conditional structure if present
            if 'conditional' in param_config:
                conditional = param_config['conditional']
                if not isinstance(conditional, dict):
                    return False

                # Each conditional value should have sql, response, or action
                for condition_key, condition_config in conditional.items():
                    if not isinstance(condition_config, dict):
                        return False
                    valid_condition_keys = {'sql', 'response', 'action'}
                    if not any(key in condition_config for key in valid_condition_keys):
                        return False

            # Validate action structure if present
            if 'action' in param_config:
                action = param_config['action']
                if not isinstance(action, (str, dict)):
                    return False

            # Validate missing_response structure if present
            if 'missing_response' in param_config:
                missing_response = param_config['missing_response']
                if not isinstance(missing_response, dict):
                    return False

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
        raise FileNotFoundError(f"Database configuration file not found: {file_path}")
    except yaml.YAMLError as e:
        raise yaml.YAMLError(f"Invalid YAML in {file_path}: {e}")


def _route_matches_pattern(path: str, pattern: str) -> bool:
    """Check if a path matches a route pattern.

    Patterns use {{}} as wildcards for path segments.
    Examples:
        - "users/{{}}" matches "users/123"
        - "users/{{user_id}}" matches "users/123"
        - "users/{{user_id}}/permissions" matches "users/123/permissions"

    Args:
        path: The path to check.
        pattern: The pattern to match against.

    Returns:
        True if path matches pattern, False otherwise.
    """
    if not isinstance(pattern, str):
        return False

    path_parts = path.strip("/").split("/")
    pattern_parts = pattern.strip("/").split("/")

    if len(path_parts) != len(pattern_parts):
        return False

    for path_part, pattern_part in zip(path_parts, pattern_parts):
        # Check if pattern part is a variable (starts and ends with double braces)
        if pattern_part.startswith("{{") and pattern_part.endswith("}}"):
            # Variable matches any value
            continue
        elif pattern_part != path_part:
            # Literal part must match exactly
            return False

    return True
"""

Database Configuration Module for API Dock

Handles loading and parsing of database configuration files for SQL-based routes.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from api_dock.types import TableReference


#
# CONSTANTS
#
DATABASES_DIR: str = "databases"

# Shared database config: databases/config.yaml. Its top-level ``database`` key
# holds global tables, shared ``meta`` defaults, and named ``schema`` groups.
SHARED_CONFIG_FILE: str = "config.yaml"
SHARED_CONFIG_KEY: str = "database"
SHARED_META_KEY: str = "meta"
SHARED_SCHEMA_KEY: str = "schema"

# Shared routes/query_params added to every database/version config, plus
# top-level exclusion lists that opt databases/versions out of all of them.
SHARED_ROUTES_KEY: str = "routes"
SHARED_QUERY_PARAMS_KEY: str = "query_params"
ROUTE_EXCLUSIONS_KEY: str = "route_exclusions"
QUERY_EXCLUSIONS_KEY: str = "query_exclusions"

# Per-route / per-query-param exclusion list key in the shared config.
EXCLUDE_KEY: str = "exclude"

# Exclusion version wildcard: matches every version (and unversioned databases).
ALL_VERSIONS: str = "*"

# Key in a version config naming the shared schema it uses for table lookups.
DATABASE_SCHEMA_KEY: str = "schema"

# Separator for qualified table references: [[schema.table]].
SCHEMA_SEPARATOR: str = "."

# Schema and table names exposed as DuckDB identifiers must be plain identifiers.
IDENTIFIER_PATTERN: re.Pattern = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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


def load_shared_database_config(config_dir: Optional[str] = None) -> Dict[str, Any]:
    """Load the shared database config (``databases/config.yaml``).

    The file is optional. Its ``database`` mapping holds global tables, a
    ``meta`` mapping of default table metadata, and a ``schema`` mapping of
    named table groups that any database route can reference.

    Args:
        config_dir: Base config directory. If None, uses default.

    Returns:
        The ``database`` mapping, or an empty dict if the file or key is missing.

    Raises:
        yaml.YAMLError: If the file exists but is invalid YAML.
        ValueError: If the ``database`` key is present but not a mapping.
    """
    return load_shared_config(config_dir).get(SHARED_CONFIG_KEY, {})


def load_shared_config(config_dir: Optional[str] = None) -> Dict[str, Any]:
    """Load the whole shared database config file (``databases/config.yaml``).

    Top-level keys: ``database`` (tables, ``meta``, ``schema``), ``routes`` and
    ``query_params`` (added to every database/version), and the
    ``route_exclusions`` / ``query_exclusions`` lists.

    Args:
        config_dir: Base config directory. If None, uses default.

    Returns:
        The file's contents with every known key normalized to its type (empty
        mapping/list when missing), or an empty dict if the file doesn't exist.

    Raises:
        yaml.YAMLError: If the file exists but is invalid YAML.
        ValueError: If the file or one of its known keys has the wrong type.
    """
    if config_dir is None:
        from api_dock.config import DEFAULT_CONFIG_DIR
        config_dir = DEFAULT_CONFIG_DIR

    shared_path = os.path.join(config_dir, DATABASES_DIR, SHARED_CONFIG_FILE)
    if not os.path.isfile(shared_path):
        return {}

    contents = _load_yaml_file(shared_path)
    if not isinstance(contents, dict):
        raise ValueError(f"{shared_path} must contain a mapping")

    normalized = dict(contents)
    expected_types = {
        SHARED_CONFIG_KEY: dict,
        SHARED_ROUTES_KEY: list,
        SHARED_QUERY_PARAMS_KEY: list,
        ROUTE_EXCLUSIONS_KEY: list,
        QUERY_EXCLUSIONS_KEY: list,
    }
    for key, expected in expected_types.items():
        value = contents.get(key) or expected()
        if not isinstance(value, expected):
            raise ValueError(f"'{key}' in {shared_path} must be a {expected.__name__}")
        normalized[key] = value
    return normalized


def apply_shared_definitions(
        database_config: Dict[str, Any],
        shared_file: Dict[str, Any],
        database_name: str,
        version: Optional[str] = None) -> Dict[str, Any]:
    """Merge the shared config's ``routes`` and ``query_params`` into a database config.

    Shared routes and query params apply to every database/version unless
    excluded, either by the top-level ``route_exclusions`` / ``query_exclusions``
    or by an item's own ``exclude`` list. Exclusion entries are
    ``"<slug>/<version>"`` strings (``"<slug>"`` or ``"<slug>/*"`` for all
    versions) or ``{slug: <slug>, version: <version or "*">}`` mappings.

    The version config always wins: its routes come first (so they take
    precedence when matching) and replace any shared route with the same shape
    (same segments, ``{{param}}`` names ignored); its top-level query params
    replace shared ones with the same name.

    Args:
        database_config: The version database configuration.
        shared_file: The whole shared config (see load_shared_config).
        database_name: Database slug from the URL (e.g. "birdnet").
        version: Resolved version (e.g. "2.4"), or None if unversioned.

    Returns:
        A new database config with merged ``routes`` and ``query_params``
        (``exclude`` keys removed from shared items). Returns the original
        config if there is nothing to merge.

    Raises:
        ValueError: If an exclusion entry is malformed.
    """
    shared_routes = shared_file.get(SHARED_ROUTES_KEY) or []
    shared_params = shared_file.get(SHARED_QUERY_PARAMS_KEY) or []
    if not shared_routes and not shared_params:
        return database_config

    merged = dict(database_config)

    if shared_routes and not _is_excluded(
            shared_file.get(ROUTE_EXCLUSIONS_KEY), database_name, version):
        own_routes = list(database_config.get("routes") or [])
        own_shapes = {
            _route_shape(route.get("route", ""))
            for route in own_routes if isinstance(route, dict)
        }
        for route in shared_routes:
            if not isinstance(route, dict):
                continue
            if _route_shape(route.get("route", "")) in own_shapes:
                continue
            if _is_excluded(route.get(EXCLUDE_KEY), database_name, version):
                continue
            own_routes.append({k: v for k, v in route.items() if k != EXCLUDE_KEY})
        merged["routes"] = own_routes

    if shared_params and not _is_excluded(
            shared_file.get(QUERY_EXCLUSIONS_KEY), database_name, version):
        own_params = list(database_config.get("query_params") or [])
        own_names = {
            next(iter(item)) for item in own_params
            if isinstance(item, dict) and len(item) == 1
        }
        for item in shared_params:
            if not isinstance(item, dict) or len(item) != 1:
                continue
            name, param_config = next(iter(item.items()))
            if name in own_names:
                continue
            if isinstance(param_config, dict):
                if _is_excluded(param_config.get(EXCLUDE_KEY), database_name, version):
                    continue
                param_config = {k: v for k, v in param_config.items() if k != EXCLUDE_KEY}
            own_params.append({name: param_config})
        merged["query_params"] = own_params

    return merged


def resolve_table_reference(
        table_name: str,
        database_config: Dict[str, Any],
        shared_config: Optional[Dict[str, Any]] = None) -> Optional[TableReference]:
    """Resolve a ``[[table]]`` reference to its URI and effective metadata.

    A qualified name (``schema.table``) is looked up in the shared config's
    ``schema`` section. An unqualified name is looked up, first match wins, in:

      1. the version config's ``tables``
      2. the shared schema named by the version config's ``schema`` key
      3. the shared config's global tables

    Shared ``meta`` applies to every table; a table's own keys override it.

    Args:
        table_name: Name inside the brackets (e.g. "detections" or
            "birdnet_2p4.detections").
        database_config: The version database configuration.
        shared_config: The shared ``database`` mapping (see
            load_shared_database_config), or None if there is none.

    Returns:
        A TableReference, or None if the table is not defined anywhere.

    Raises:
        ValueError: If a qualified reference uses a schema or table name that is
            not a plain SQL identifier.
    """
    shared = shared_config or {}
    defaults = shared.get(SHARED_META_KEY) or {}
    schemas = shared.get(SHARED_SCHEMA_KEY) or {}

    if SCHEMA_SEPARATOR in table_name:
        schema_name, name = table_name.split(SCHEMA_SEPARATOR, 1)
        for identifier in (schema_name, name):
            if not IDENTIFIER_PATTERN.match(identifier):
                raise ValueError(f"Invalid identifier '{identifier}' in table '{table_name}'")
        entry = _table_entry((schemas.get(schema_name) or {}).get(name), defaults)
        if entry is None:
            return None
        return TableReference(
            name=name, uri=entry[0], metadata=entry[1], schema=schema_name, qualified=True
        )

    entry = _table_entry((database_config.get("tables") or {}).get(table_name), defaults)
    if entry is not None:
        return TableReference(name=table_name, uri=entry[0], metadata=entry[1])

    schema_name = database_config.get(DATABASE_SCHEMA_KEY)
    if schema_name:
        entry = _table_entry((schemas.get(schema_name) or {}).get(table_name), defaults)
        if entry is not None:
            return TableReference(
                name=table_name, uri=entry[0], metadata=entry[1], schema=schema_name
            )

    if table_name not in (SHARED_META_KEY, SHARED_SCHEMA_KEY):
        entry = _table_entry(shared.get(table_name), defaults)
        if entry is not None:
            return TableReference(name=table_name, uri=entry[0], metadata=entry[1])

    return None


def get_local_table_references(
        database_config: Dict[str, Any],
        shared_config: Optional[Dict[str, Any]] = None) -> List[TableReference]:
    """Resolve every table in the version config's ``tables`` section.

    Args:
        database_config: The version database configuration.
        shared_config: The shared ``database`` mapping, or None.

    Returns:
        TableReferences (with shared ``meta`` applied) in config order.
    """
    references = []
    for table_name in database_config.get("tables") or {}:
        reference = resolve_table_reference(str(table_name), database_config, shared_config)
        if reference is not None:
            references.append(reference)
    return references


def merge_query_params(route_config: Dict[str, Any], database_config: Dict[str, Any]) -> Dict[str, Any]:
    """Merge top-level query_params into route config.

    Top-level query_params from the database config are applied to every route.
    Route-level params take precedence: if a route defines a param with the same
    name as a top-level param, the route version wins. Non-overridden top-level
    params are appended after route params.

    Args:
        route_config: Route configuration dictionary.
        database_config: Database configuration dictionary (may contain top-level query_params).

    Returns:
        Route config dict with merged query_params. Returns original route_config
        if no top-level query_params exist.
    """
    top_level = database_config.get("query_params", [])
    if not top_level:
        return route_config

    route_params = route_config.get("query_params", [])

    # Get names already defined at route level
    route_param_names = set()
    for item in route_params:
        if isinstance(item, dict) and len(item) == 1:
            route_param_names.add(next(iter(item)))

    # Append non-overridden top-level params after route params
    merged = list(route_params)
    for item in top_level:
        if isinstance(item, dict) and len(item) == 1:
            name = next(iter(item))
            if name not in route_param_names:
                merged.append(item)

    return {**route_config, "query_params": merged}


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

    # The sql selector may be a plain string or a list of selector rules.
    if 'sql' in route_config and not isinstance(route_config['sql'], (str, list)):
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
            valid_keys = {'sql', 'sql_append', 'response', 'conditional', 'action', 'required', 'default', 'missing_response'}
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


def load_database_config_with_inheritance(database_filename: str, main_config: Dict[str, Any], config_dir: Optional[str] = None, version: Optional[str] = None) -> Dict[str, Any]:
    """Load a database configuration file with cookie/authentication inheritance.

    Args:
        database_filename: Name of the database config file (without .yaml extension).
        main_config: Main configuration dictionary for inheritance.
        config_dir: Base config directory. If None, uses default.
        version: Version string for versioned databases.

    Returns:
        Dictionary containing database configuration data with inheritance applied.

    Raises:
        FileNotFoundError: If database config file doesn't exist.
        yaml.YAMLError: If config file is invalid YAML.
    """
    # Load the database config
    database_config = load_database_config(database_filename, config_dir, version)

    # Apply inheritance from main config
    from api_dock.config import merge_inherited_config
    merged_config = merge_inherited_config(database_config, main_config)

    return merged_config


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


def _is_excluded(exclusions: Any, database_name: str, version: Optional[str]) -> bool:
    """Check whether a database/version matches any entry of an exclusion list.

    Args:
        exclusions: List of ``"<slug>[/<version>]"`` strings or
            ``{slug, version}`` mappings, or None.
        database_name: Database slug from the URL.
        version: Resolved version, or None if unversioned.

    Returns:
        True if any entry matches.

    Raises:
        ValueError: If the list or one of its entries is malformed.
    """
    if not exclusions:
        return False
    if not isinstance(exclusions, list):
        raise ValueError(f"Exclusion list must be a list, got {type(exclusions).__name__}")

    for entry in exclusions:
        slug, excluded_version = _parse_exclusion(entry)
        if slug != database_name:
            continue
        if excluded_version == ALL_VERSIONS:
            return True
        if version is not None and _versions_equal(version, excluded_version):
            return True
    return False


def _parse_exclusion(entry: Any) -> Tuple[str, str]:
    """Parse one exclusion entry into (slug, version spec).

    Args:
        entry: ``"<slug>"``, ``"<slug>/<version>"``, or ``{slug, version}``.
            A missing version means all versions.

    Returns:
        Tuple of (slug, version) where version may be ALL_VERSIONS.

    Raises:
        ValueError: If the entry has no slug or an unsupported type.
    """
    if isinstance(entry, str):
        slug, _, excluded_version = entry.strip().strip("/").partition("/")
    elif isinstance(entry, dict):
        slug = str(entry.get("slug") or "")
        raw_version = entry.get("version")
        excluded_version = ALL_VERSIONS if raw_version is None else str(raw_version)
    else:
        raise ValueError(f"Invalid exclusion entry: {entry!r}")

    if not slug:
        raise ValueError(f"Exclusion entry has no slug: {entry!r}")
    return (slug, excluded_version.strip() or ALL_VERSIONS)


def _versions_equal(version: str, spec: str) -> bool:
    """Compare a version stem to an exclusion version, tolerating float forms.

    So "3.0" matches a YAML ``3.0`` or ``3`` as well as ``"3.0"``.

    Args:
        version: The resolved version stem.
        spec: The configured exclusion version.

    Returns:
        True if they represent the same version.
    """
    if str(version).strip() == str(spec).strip():
        return True
    try:
        return float(version) == float(spec)
    except ValueError:
        return False


def _route_shape(pattern: Any) -> str:
    """Normalize a route pattern so equivalent routes compare equal.

    Strips surrounding slashes and replaces every ``{{param}}`` segment with
    ``{{}}``, so ``/detections/{{id}}`` and ``detections/{{detection_id}}/``
    have the same shape.

    Args:
        pattern: Route pattern string.

    Returns:
        Normalized shape string.
    """
    parts = str(pattern).strip("/").split("/")
    return "/".join(
        "{{}}" if part.startswith("{{") and part.endswith("}}") else part for part in parts
    )


def _table_entry(
        table_def: Any,
        defaults: Dict[str, Any]) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Split a table definition into its URI and effective metadata.

    Args:
        table_def: A URI string or a dict with ``uri``/``path`` plus metadata.
        defaults: Shared ``meta`` defaults; the table's own keys override them.

    Returns:
        Tuple of (uri, metadata), or None if the definition has no URI.
    """
    if isinstance(table_def, str):
        return (table_def, dict(defaults))
    if isinstance(table_def, dict):
        uri = table_def.get("uri") or table_def.get("path")
        if not uri:
            return None
        own = {k: v for k, v in table_def.items() if k not in ("uri", "path")}
        return (str(uri), {**defaults, **own})
    return None


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
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
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml

from api_dock.postgres_config import check_connection_config, check_postgres_table_name
from api_dock.sql_template_check import (
    check_comment_at_end,
    check_commented_variables,
    check_quoted_variables,
)
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

# Named PostgreSQL connections in the shared ``database`` mapping. A table is a
# PostgreSQL table when its definition has ``table`` (its PostgreSQL name)
# instead of ``uri``; its ``connection`` comes from the table or from ``meta``.
SHARED_CONNECTIONS_KEY: str = "connections"
TABLE_CONNECTION_KEY: str = "connection"
POSTGRES_TABLE_KEY: str = "table"

# Keys of the shared ``database`` mapping that are not global tables.
SHARED_RESERVED_KEYS: frozenset = frozenset({
    SHARED_META_KEY, SHARED_SCHEMA_KEY, SHARED_CONNECTIONS_KEY,
})

# Shared routes/query_params added to every database/version config, plus
# top-level lists that restrict all of them to (inclusions) or opt
# databases/versions out of all of them (exclusions).
SHARED_ROUTES_KEY: str = "routes"
SHARED_QUERY_PARAMS_KEY: str = "query_params"
ROUTE_INCLUSIONS_KEY: str = "route_inclusions"
QUERY_INCLUSIONS_KEY: str = "query_inclusions"
ROUTE_EXCLUSIONS_KEY: str = "route_exclusions"
QUERY_EXCLUSIONS_KEY: str = "query_exclusions"

# Per-route / per-query-param inclusion and exclusion list keys in the shared
# config. They are removed from the item before it is merged.
INCLUDE_KEY: str = "include"
EXCLUDE_KEY: str = "exclude"
SELECTION_KEYS: frozenset = frozenset({INCLUDE_KEY, EXCLUDE_KEY})

# Database/version configs defined inline in the shared config (instead of as
# files): a list of {name, version | versions: [{version, ...}], ...} entries.
SLUGS_KEY: str = "slugs"
SLUG_NAME_KEY: str = "name"
SLUG_VERSION_KEY: str = "version"
SLUG_VERSIONS_KEY: str = "versions"

# Named groups of shared schemas: {group: [schema, ...]}, referenced in SQL as
# [[group.table]]. Group names may not collide with schema names.
SCHEMA_GROUPS_KEY: str = "schema_groups"

# Union selectors: [[*.table]] = every shared schema with that table; a trailing
# "!" ([[*!.table]], [[group!.table]]) drops the current version's schema.
ALL_SCHEMAS: str = "*"
EXCLUDE_SELF_SUFFIX: str = "!"

# Exclusion version wildcard: matches every version (and unversioned databases).
ALL_VERSIONS: str = "*"

# Key in a version config naming the shared schema it uses for table lookups.
DATABASE_SCHEMA_KEY: str = "schema"

# Separator for qualified table references: [[schema.table]].
SCHEMA_SEPARATOR: str = "."

# Schema and table names exposed as DuckDB identifiers must be plain identifiers.
IDENTIFIER_PATTERN: re.Pattern = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Keys a query_params entry may have; at least one must be present.
QUERY_PARAM_KEYS: frozenset = frozenset({
    'sql', 'multivalue_sql', 'sql_append', 'response', 'conditional', 'action',
    'required', 'default', 'missing_response',
})

# Keys each branch of a conditional query param may have; at least one must be present.
CONDITION_KEYS: frozenset = frozenset({'sql', 'response', 'action'})

# Checks for templates whose {{variables}} are bound. sql_append values are
# written into the SQL, so their templates only need to not end in a comment,
# which would hide the clauses joined after them.
BOUND_TEMPLATE_CHECKS: Tuple[Callable[[str], None], ...] = (
    check_quoted_variables, check_commented_variables, check_comment_at_end,
)
APPEND_TEMPLATE_CHECKS: Tuple[Callable[[str], None], ...] = (check_comment_at_end,)


#
# PUBLIC
#
def load_database_config(database_filename: str, config_dir: Optional[str] = None, version: Optional[str] = None) -> Dict[str, Any]:
    """Load a database configuration file.

    A database/version may be defined by a file (``databases/<name>.yaml`` or
    ``databases/<name>/<version>.yaml``) or by an entry in the shared config's
    ``slugs`` list. When both define the same database/version, the file wins.

    Args:
        database_filename: Name of the database config file (without .yaml extension).
        config_dir: Base config directory. If None, uses default.
        version: Version string for versioned databases (e.g., "0.1", "1.2"). If None, loads non-versioned database.

    Returns:
        Dictionary containing database configuration data.

    Raises:
        FileNotFoundError: If neither a config file nor a slug entry exists.
        yaml.YAMLError: If config file is invalid YAML.
        ValueError: If the shared config's ``slugs`` section is malformed.
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

    if not os.path.isfile(database_config_path):
        slug_versions = get_slug_configs(config_dir).get(database_filename, {})
        version_key = None if version is None else str(version)
        if version_key in slug_versions:
            return slug_versions[version_key]

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

    Top-level keys: ``database`` (tables, ``meta``, ``schema``), ``slugs``
    (inline database/version configs), ``schema_groups``, ``routes`` and
    ``query_params`` (added to every database/version), and the
    ``route_inclusions`` / ``query_inclusions`` and ``route_exclusions`` /
    ``query_exclusions`` lists.

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
        ROUTE_INCLUSIONS_KEY: list,
        QUERY_INCLUSIONS_KEY: list,
        ROUTE_EXCLUSIONS_KEY: list,
        QUERY_EXCLUSIONS_KEY: list,
        SLUGS_KEY: list,
        SCHEMA_GROUPS_KEY: dict,
    }
    for key, expected in expected_types.items():
        value = contents.get(key) or expected()
        if not isinstance(value, expected):
            raise ValueError(f"'{key}' in {shared_path} must be a {expected.__name__}")
        normalized[key] = value

    _validate_schema_groups(normalized, shared_path)
    _validate_connections(normalized, shared_path)
    return normalized


def apply_shared_definitions(
        database_config: Dict[str, Any],
        shared_file: Dict[str, Any],
        database_name: str,
        version: Optional[str] = None) -> Dict[str, Any]:
    """Merge the shared config's ``routes`` and ``query_params`` into a database config.

    Shared routes and query params apply to every database/version unless
    restricted. A non-empty inclusion list (top-level ``route_inclusions`` /
    ``query_inclusions`` or an item's own ``include``) limits them to the
    listed databases/versions; an exclusion list (``route_exclusions`` /
    ``query_exclusions`` or ``exclude``) removes the listed ones. An item is
    added only if it passes both the top-level and its own lists. Entries are
    ``"<slug>/<version>"`` strings (``"<slug>"`` or ``"<slug>/*"`` for all
    versions) or ``{slug: <slug>, version: <version or "*">}`` mappings.

    The version config always wins: its routes come first (so they take
    precedence when matching) and replace any shared route with the same shape
    (same segments, ``{{param}}`` names ignored); its top-level query params
    replace shared ones with the same name. Among shared items, the first one
    selected for a given route shape / param name is used, so the same route
    can be defined several times with different ``include``/``exclude`` lists.

    Args:
        database_config: The version database configuration.
        shared_file: The whole shared config (see load_shared_config).
        database_name: Database slug from the URL (e.g. "birdnet").
        version: Resolved version (e.g. "2.4"), or None if unversioned.

    Returns:
        A new database config with merged ``routes`` and ``query_params``
        (``include``/``exclude`` keys removed from shared items). Returns the original
        config if there is nothing to merge.

    Raises:
        ValueError: If an include/exclude entry is malformed.
    """
    shared_routes = shared_file.get(SHARED_ROUTES_KEY) or []
    shared_params = shared_file.get(SHARED_QUERY_PARAMS_KEY) or []
    if not shared_routes and not shared_params:
        return database_config

    merged = dict(database_config)

    if shared_routes and _is_selected(
            shared_file.get(ROUTE_INCLUSIONS_KEY), shared_file.get(ROUTE_EXCLUSIONS_KEY),
            database_name, version):
        own_routes = list(database_config.get("routes") or [])
        taken_shapes = {
            _route_shape(route.get("route", ""))
            for route in own_routes if isinstance(route, dict)
        }
        for route in shared_routes:
            if not isinstance(route, dict):
                continue
            shape = _route_shape(route.get("route", ""))
            if shape in taken_shapes:
                continue
            if not _is_selected(
                    route.get(INCLUDE_KEY), route.get(EXCLUDE_KEY), database_name, version):
                continue
            own_routes.append({k: v for k, v in route.items() if k not in SELECTION_KEYS})
            taken_shapes.add(shape)
        merged["routes"] = own_routes

    if shared_params and _is_selected(
            shared_file.get(QUERY_INCLUSIONS_KEY), shared_file.get(QUERY_EXCLUSIONS_KEY),
            database_name, version):
        own_params = list(database_config.get("query_params") or [])
        taken_names = {
            next(iter(item)) for item in own_params
            if isinstance(item, dict) and len(item) == 1
        }
        for item in shared_params:
            if not isinstance(item, dict) or len(item) != 1:
                continue
            name, param_config = next(iter(item.items()))
            if name in taken_names:
                continue
            if isinstance(param_config, dict):
                if not _is_selected(
                        param_config.get(INCLUDE_KEY), param_config.get(EXCLUDE_KEY),
                        database_name, version):
                    continue
                param_config = {
                    k: v for k, v in param_config.items() if k not in SELECTION_KEYS
                }
            own_params.append({name: param_config})
            taken_names.add(name)
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
            name=name, uri=entry[0], metadata=entry[1], connection=entry[2],
            schema=schema_name, qualified=True
        )

    entry = _table_entry((database_config.get("tables") or {}).get(table_name), defaults)
    if entry is not None:
        return TableReference(
            name=table_name, uri=entry[0], metadata=entry[1], connection=entry[2]
        )

    schema_name = database_config.get(DATABASE_SCHEMA_KEY)
    if schema_name:
        entry = _table_entry((schemas.get(schema_name) or {}).get(table_name), defaults)
        if entry is not None:
            return TableReference(
                name=table_name, uri=entry[0], metadata=entry[1], connection=entry[2],
                schema=schema_name
            )

    if table_name not in SHARED_RESERVED_KEYS:
        entry = _table_entry(shared.get(table_name), defaults)
        if entry is not None:
            return TableReference(
                name=table_name, uri=entry[0], metadata=entry[1], connection=entry[2]
            )

    return None


def resolve_schema_union(
        selector: str,
        table_name: str,
        shared_config: Optional[Dict[str, Any]] = None,
        schema_groups: Optional[Dict[str, List[str]]] = None) -> Optional[List[TableReference]]:
    """Resolve a union selector (``*`` or a schema group) to its member tables.

    Args:
        selector: ``*`` for every shared schema, or a ``schema_groups`` name
            (without any trailing ``!``).
        table_name: Table to read from each member schema.
        shared_config: The shared ``database`` mapping, or None.
        schema_groups: The shared ``schema_groups`` mapping, or None.

    Returns:
        Qualified TableReferences in schema/group order, or None if the
        selector is neither ``*`` nor a group (i.e. it names a single schema).

    Raises:
        ValueError: If no schema has the table (``*``), or a group member is
            not a schema or lacks the table.
    """
    schemas = (shared_config or {}).get(SHARED_SCHEMA_KEY) or {}
    groups = schema_groups or {}

    if selector == ALL_SCHEMAS:
        members = [name for name, tables in schemas.items() if table_name in (tables or {})]
        if not members:
            raise ValueError(f"No shared schema has a table named '{table_name}'")
    elif selector in groups:
        members = list(groups[selector])
        for schema_name in members:
            if table_name not in (schemas.get(schema_name) or {}):
                raise ValueError(
                    f"Schema '{schema_name}' in group '{selector}' has no table '{table_name}'"
                )
    else:
        return None

    references = []
    for schema_name in members:
        reference = resolve_table_reference(
            f"{schema_name}{SCHEMA_SEPARATOR}{table_name}", {}, shared_config
        )
        if reference is None:
            raise ValueError(f"Table '{schema_name}.{table_name}' not found")
        references.append(reference)
    return references


def get_schema_sources(
        database_names: List[str],
        config_dir: Optional[str] = None) -> Dict[str, Tuple[str, Optional[str]]]:
    """Map each shared schema to the database/version that uses it.

    Args:
        database_names: Served database names (from the main config).
        config_dir: Base config directory. If None, uses default.

    Returns:
        Schema name -> (database name, version or None). Schemas used by more
        than one database/version are left out (their source is ambiguous).
    """
    sources: Dict[str, Tuple[str, Optional[str]]] = {}
    ambiguous = set()

    for database_name in database_names:
        if is_versioned_database(database_name, config_dir):
            versions: List[Optional[str]] = list(get_database_versions(database_name, config_dir))
        else:
            versions = [None]
        for version in versions:
            try:
                config = load_database_config(database_name, config_dir, version)
            except FileNotFoundError:
                continue
            schema_name = config.get(DATABASE_SCHEMA_KEY) if isinstance(config, dict) else None
            if not schema_name:
                continue
            if schema_name in sources:
                ambiguous.add(schema_name)
            sources[schema_name] = (database_name, version)

    return {k: v for k, v in sources.items() if k not in ambiguous}


def route_templates(route_config: Dict[str, Any]) -> List[str]:
    """Every SQL template a route can use: each selector branch and query param.

    Args:
        route_config: Route configuration, merged with top-level query_params.

    Returns:
        The route's sql (every selector branch, and each branch's sql_append)
        and each query param's sql, multivalue_sql, conditional sql and
        sql_append, as strings.
    """
    return [
        template
        for _, template in _bound_templates(route_config) + _append_templates(route_config)
        if isinstance(template, str)
    ]


def check_table_definitions(
        tables: Dict[str, Any],
        shared_config: Optional[Dict[str, Any]],
        location: str) -> None:
    """Check that PostgreSQL table definitions name a known connection and a valid table.

    File tables are not checked here (their URIs are read by DuckDB).

    Args:
        tables: Mapping of table name -> definition (a ``tables`` section, a
            schema, or the shared global tables).
        shared_config: The shared ``database`` mapping (for ``connections`` and
            ``meta``), or None.
        location: Where the tables are, for error messages.

    Raises:
        ValueError: If a PostgreSQL table has no or an unknown connection, or an
            invalid PostgreSQL name.
    """
    shared = shared_config or {}
    connections = shared.get(SHARED_CONNECTIONS_KEY) or {}
    defaults = shared.get(SHARED_META_KEY) or {}
    for table_name, table_def in (tables or {}).items():
        entry = _table_entry(table_def, defaults)
        if entry is None or not (isinstance(table_def, dict) and POSTGRES_TABLE_KEY in table_def):
            continue
        uri, _, connection = entry
        prefix = f"{location}: table '{table_name}'"
        if connection is None:
            raise ValueError(f"{prefix} needs a '{TABLE_CONNECTION_KEY}' (or one in meta)")
        if connection not in connections:
            raise ValueError(f"{prefix} uses unknown connection '{connection}'")
        try:
            check_postgres_table_name(str(table_name), uri)
        except ValueError as error:
            raise ValueError(f"{location}: {error}") from error


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
        True if the database has a config directory or versioned entries in
        the shared config's ``slugs``, False otherwise.

    Raises:
        ValueError: If the shared config's ``slugs`` section is malformed.
    """
    if config_dir is None:
        from api_dock.config import DEFAULT_CONFIG_DIR
        config_dir = DEFAULT_CONFIG_DIR

    database_dir = os.path.join(config_dir, DATABASES_DIR, database_name)
    if os.path.isdir(database_dir):
        return True
    return any(v is not None for v in get_slug_configs(config_dir).get(database_name, {}))


def get_database_versions(database_name: str, config_dir: Optional[str] = None) -> List[str]:
    """Get list of available versions for a versioned database.

    Args:
        database_name: Name of the database.
        config_dir: Base config directory. If None, uses default.

    Returns:
        List of version strings (e.g., ["0.1", "0.2", "1.2"]) from version
        files and the shared config's ``slugs``, without duplicates, lowest
        first (see ``api_dock.config.sort_versions``).
        Returns empty list if database is not versioned.

    Raises:
        ValueError: If the shared config's ``slugs`` section is malformed.
    """
    if config_dir is None:
        from api_dock.config import DEFAULT_CONFIG_DIR
        config_dir = DEFAULT_CONFIG_DIR

    if not is_versioned_database(database_name, config_dir):
        return []

    database_dir = os.path.join(config_dir, DATABASES_DIR, database_name)
    versions = set()

    if os.path.isdir(database_dir):
        for filename in os.listdir(database_dir):
            if filename.endswith('.yaml'):
                versions.add(filename[:-5])  # Remove .yaml extension

    slug_versions = get_slug_configs(config_dir).get(database_name, {})
    versions.update(v for v in slug_versions if v is not None)

    from api_dock.config import sort_versions
    return sort_versions(versions)


def get_slug_configs(config_dir: Optional[str] = None) -> Dict[str, Dict[Optional[str], Dict[str, Any]]]:
    """Build database configs from the shared config's ``slugs`` list.

    Each entry has a ``name`` and one of:
      - ``version: <v>`` — one versioned config;
      - ``versions: [{version: <v>, ...}, ...]`` — several versions, where the
        entry's other keys are defaults each version's keys override;
      - neither — an unversioned database.
    Every other key (description, authors, schema, tables, routes,
    query_params, ...) is used exactly as in a database config file.

    Args:
        config_dir: Base config directory. If None, uses default.

    Returns:
        Mapping of database name -> {version string (None if unversioned) ->
        database config dict}. Versions are strings (YAML ``5.0`` -> "5.0").

    Raises:
        ValueError: If an entry is malformed, uses both ``version`` and
            ``versions``, mixes versioned and unversioned definitions of a
            name, or defines the same name/version twice.
    """
    slugs = load_shared_config(config_dir).get(SLUGS_KEY) or []
    configs: Dict[str, Dict[Optional[str], Dict[str, Any]]] = {}

    for entry in slugs:
        if not isinstance(entry, dict) or not entry.get(SLUG_NAME_KEY):
            raise ValueError(f"slugs: each entry needs a '{SLUG_NAME_KEY}': {entry!r}")
        name = str(entry[SLUG_NAME_KEY])
        if SLUG_VERSION_KEY in entry and SLUG_VERSIONS_KEY in entry:
            raise ValueError(
                f"slugs: '{name}' uses both '{SLUG_VERSION_KEY}' and '{SLUG_VERSIONS_KEY}'"
            )

        defaults = {
            k: v for k, v in entry.items()
            if k not in (SLUG_NAME_KEY, SLUG_VERSION_KEY, SLUG_VERSIONS_KEY)
        }
        if SLUG_VERSIONS_KEY in entry:
            version_entries = entry[SLUG_VERSIONS_KEY]
            if not isinstance(version_entries, list) or not version_entries:
                raise ValueError(f"slugs: '{name}' '{SLUG_VERSIONS_KEY}' must be a non-empty list")
        else:
            version_entries = [{SLUG_VERSION_KEY: entry.get(SLUG_VERSION_KEY)}]

        for version_entry in version_entries:
            if not isinstance(version_entry, dict):
                raise ValueError(f"slugs: '{name}' has an invalid version entry: {version_entry!r}")
            raw_version = version_entry.get(SLUG_VERSION_KEY)
            if SLUG_VERSIONS_KEY in entry and raw_version is None:
                raise ValueError(f"slugs: '{name}' has a version entry without a version")
            version = None if raw_version is None else str(raw_version).strip()

            versions = configs.setdefault(name, {})
            if version in versions:
                raise ValueError(f"slugs: '{name}' version {version} is defined twice")
            if versions and (version is None) != (None in versions):
                raise ValueError(f"slugs: '{name}' mixes versioned and unversioned entries")

            own = {k: v for k, v in version_entry.items() if k != SLUG_VERSION_KEY}
            versions[version] = {SLUG_NAME_KEY: name, **defaults, **own}

    return configs


def resolve_latest_database_version(versions: List[str]) -> Optional[str]:
    """Resolve 'latest' to the highest version from a list.

    Args:
        versions: List of version strings.

    Returns:
        The latest version string, or None if list is empty.
    """
    from api_dock.config import resolve_latest_version
    return resolve_latest_version(versions)


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


def validate_route_config(route_config: Dict[str, Any]) -> None:
    """Check the basic shape of a route configuration.

    Args:
        route_config: Route configuration dictionary.

    Raises:
        ValueError: If the route is malformed. The message gives the reason.
    """
    if not isinstance(route_config, dict):
        raise ValueError("route must be a mapping")
    if 'route' not in route_config:
        raise ValueError("missing required 'route' key")
    # The sql selector may be a plain string or a list of selector rules.
    if 'sql' in route_config and not isinstance(route_config['sql'], (str, list)):
        raise ValueError("'sql' must be a string or a list of selector rules")

    query_params = route_config.get('query_params', [])
    if not isinstance(query_params, list):
        raise ValueError("'query_params' must be a list")
    for param_item in query_params:
        _validate_query_param(param_item)


def check_database_config(
        database_config: Dict[str, Any],
        template_check: Optional[Callable[[str, Dict[str, Any]], None]] = None) -> None:
    """Check every named query and route of a loaded database config.

    Each route is merged with top-level query_params, as a request would be,
    then checked for shape and for {{variables}} inside quotes or comments in
    any template whose values are bound. sql_append templates are not checked
    for variables because their values are written into the SQL text. No
    template may end inside a comment.

    Args:
        database_config: Database configuration, already merged with the main
            config (and with the shared config's routes/query_params).
        template_check: Optional extra check run on every query and route
            template, called as ``template_check(template, route_config)``
            (``route_config`` is ``{}`` for named queries); it raises
            ValueError for a bad template (e.g. an unknown ``[[table]]``).

    Raises:
        ValueError: If a query or route fails a check. The message names the
            route (or query) and the template's location.
    """
    def extra_checks(route_config: Dict[str, Any]) -> Tuple[Callable[[str], None], ...]:
        """The template_check bound to one route, as a single-argument check."""
        if template_check is None:
            return ()
        return (lambda template: template_check(template, route_config),)

    for query_name, query in database_config.get('queries', {}).items():
        _check_template(
            f"queries.{query_name}", query, BOUND_TEMPLATE_CHECKS + extra_checks({})
        )

    for index, route_config in enumerate(database_config.get('routes', [])):
        try:
            validate_route_config(route_config)
            merged = merge_query_params(route_config, database_config)
            validate_route_config(merged)
            for location, template in _bound_templates(merged):
                _check_template(
                    location, template, BOUND_TEMPLATE_CHECKS + extra_checks(merged)
                )
            for location, template in _append_templates(merged):
                _check_template(
                    location, template, APPEND_TEMPLATE_CHECKS + extra_checks(merged)
                )
        except ValueError as error:
            raise ValueError(f"route '{_route_label(route_config, index)}': {error}") from error


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
def _validate_query_param(param_item: Any) -> None:
    """Check the shape of one query_params entry.

    Args:
        param_item: A query_params list entry, expected as ``{name: config}``.

    Raises:
        ValueError: If the entry is malformed. The message names the param.
    """
    if not isinstance(param_item, dict) or len(param_item) != 1:
        raise ValueError("each query_params entry must be a mapping with one param name")

    param_name, param_config = next(iter(param_item.items()))
    if not isinstance(param_config, dict):
        raise ValueError(f"query param '{param_name}' must be a mapping")
    if not any(key in param_config for key in QUERY_PARAM_KEYS):
        raise ValueError(
            f"query param '{param_name}' has none of the keys {sorted(QUERY_PARAM_KEYS)}"
        )
    if 'conditional' in param_config:
        _validate_conditional(param_name, param_config['conditional'])
    if 'action' in param_config and not isinstance(param_config['action'], (str, dict)):
        raise ValueError(f"query param '{param_name}' action must be a string or mapping")
    missing_response = param_config.get('missing_response', {})
    if not isinstance(missing_response, dict):
        raise ValueError(f"query param '{param_name}' missing_response must be a mapping")


def _validate_conditional(param_name: str, conditional: Any) -> None:
    """Check the shape of a query param's conditional section.

    Args:
        param_name: Name of the query param, for error messages.
        conditional: The param's ``conditional`` value.

    Raises:
        ValueError: If the section is malformed. The message names the param.
    """
    if not isinstance(conditional, dict):
        raise ValueError(f"query param '{param_name}' conditional must be a mapping")
    for condition_key, condition_config in conditional.items():
        if not isinstance(condition_config, dict) or not any(
                key in condition_config for key in CONDITION_KEYS):
            raise ValueError(
                f"query param '{param_name}' conditional '{condition_key}' needs one of "
                f"{sorted(CONDITION_KEYS)}"
            )


def _check_template(
        location: str,
        template: Any,
        checks: Tuple[Callable[[str], None], ...]) -> None:
    """Run checks on one SQL template, adding its location to any error.

    Args:
        location: Where the template is in the config, e.g. ``query_params.name.sql``.
        template: The template; non-string values are skipped.
        checks: Functions that raise ValueError for a bad template.

    Raises:
        ValueError: If a check fails.
    """
    if not isinstance(template, str):
        return
    try:
        for check in checks:
            check(template)
    except ValueError as error:
        raise ValueError(f"{location}: {error}") from error


def _route_label(route_config: Any, index: int) -> str:
    """Name a route for error messages.

    Args:
        route_config: Route configuration entry.
        index: Position of the entry in the routes list.

    Returns:
        The route pattern, or ``routes[<index>]`` if the entry has none.
    """
    if isinstance(route_config, dict) and 'route' in route_config:
        return str(route_config['route'])
    return f"routes[{index}]"


def _bound_templates(route_config: Dict[str, Any]) -> List[Tuple[str, Any]]:
    """List a route's SQL templates whose {{variables}} are bound, with their locations.

    Args:
        route_config: Route configuration, merged with top-level query_params.

    Returns:
        ``(location, template)`` pairs for the route sql (every selector
        branch), and each query param's sql, multivalue_sql and conditional sql.
    """
    templates = [
        (location, node) if isinstance(node, str) else (f"{location}.sql", node['sql'])
        for location, node in _selector_leaves(route_config.get('sql', ''), 'sql')
    ]
    for param_item in route_config.get('query_params', []):
        param_name, param_config = next(iter(param_item.items()))
        prefix = f"query_params.{param_name}"
        for key in ('sql', 'multivalue_sql'):
            if key in param_config:
                templates.append((f"{prefix}.{key}", param_config[key]))
        for condition_key, condition_config in param_config.get('conditional', {}).items():
            if 'sql' in condition_config:
                location = f"{prefix}.conditional.{condition_key}.sql"
                templates.append((location, condition_config['sql']))
    return templates


def _append_templates(route_config: Dict[str, Any]) -> List[Tuple[str, Any]]:
    """List a route's sql_append templates, with their locations.

    Args:
        route_config: Route configuration, merged with top-level query_params.

    Returns:
        ``(location, template)`` pairs for each selector branch's sql_append
        (a string or a list) and each query param's sql_append.
    """
    templates = []
    for location, node in _selector_leaves(route_config.get('sql', ''), 'sql'):
        append = node.get('sql_append') if isinstance(node, dict) else None
        if isinstance(append, list):
            templates.extend(
                (f"{location}.sql_append[{index}]", item) for index, item in enumerate(append)
            )
        elif append is not None:
            templates.append((f"{location}.sql_append", append))
    for param_item in route_config.get('query_params', []):
        param_name, param_config = next(iter(param_item.items()))
        if 'sql_append' in param_config:
            templates.append((f"query_params.{param_name}.sql_append", param_config['sql_append']))
    return templates


def _selector_leaves(node: Any, location: str) -> List[Tuple[str, Any]]:
    """List the leaves of an sql selector node, with their locations.

    A node is a string, a leaf ``{sql, sql_append}``, a rule, or a list of
    rules (see sql_builder.resolve_route_sql).

    Args:
        node: The selector node.
        location: The node's location in the route config.

    Returns:
        ``(location, leaf)`` pairs, where each leaf is a SQL string or a
        mapping with an ``sql`` key.
    """
    if isinstance(node, str):
        return [(location, node)]
    if isinstance(node, list):
        return [
            leaf
            for index, item in enumerate(node)
            for leaf in _selector_leaves(item, f"{location}[{index}]")
        ]
    if not isinstance(node, dict):
        return []
    if 'sql' in node:
        return [(location, node)]

    leaves = []
    for key in ('then', 'else', 'default'):
        if key in node:
            leaves.extend(_selector_leaves(node[key], f"{location}.{key}"))
    match = node.get('match')
    if isinstance(match, dict):
        for value, branch in match.items():
            leaves.extend(_selector_leaves(branch, f"{location}.match.{value}"))
    elif isinstance(match, list):
        leaves.extend(_selector_leaves(match, f"{location}.match"))
    return leaves


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


def _validate_connections(shared_file: Dict[str, Any], shared_path: str) -> None:
    """Validate the shared ``connections`` and the PostgreSQL tables that use them.

    Args:
        shared_file: The normalized shared config.
        shared_path: Path of the shared config (for error messages).

    Raises:
        ValueError: If a connection entry is invalid, or a PostgreSQL table in a
            schema or among the global tables is invalid.
    """
    shared = shared_file.get(SHARED_CONFIG_KEY) or {}
    connections = shared.get(SHARED_CONNECTIONS_KEY) or {}
    if not isinstance(connections, dict):
        raise ValueError(f"{SHARED_CONNECTIONS_KEY} must be a mapping in {shared_path}")
    try:
        for name, entry in connections.items():
            check_connection_config(str(name), entry)
        global_tables = {k: v for k, v in shared.items() if k not in SHARED_RESERVED_KEYS}
        check_table_definitions(global_tables, shared, "global tables")
        for schema_name, tables in (shared.get(SHARED_SCHEMA_KEY) or {}).items():
            check_table_definitions(tables or {}, shared, f"schema '{schema_name}'")
    except ValueError as error:
        raise ValueError(f"{error} (in {shared_path})") from error


def _validate_schema_groups(shared_file: Dict[str, Any], shared_path: str) -> None:
    """Validate the shared ``schema_groups`` mapping.

    Args:
        shared_file: The normalized shared config.
        shared_path: Path of the shared config (for error messages).

    Raises:
        ValueError: If a group name isn't a plain identifier or collides with a
            schema name, or a group isn't a non-empty list of known schemas.
    """
    schemas = (shared_file.get(SHARED_CONFIG_KEY) or {}).get(SHARED_SCHEMA_KEY) or {}
    for group, members in (shared_file.get(SCHEMA_GROUPS_KEY) or {}).items():
        if not IDENTIFIER_PATTERN.match(str(group)):
            raise ValueError(f"{SCHEMA_GROUPS_KEY}: invalid group name '{group}' in {shared_path}")
        if group in schemas:
            raise ValueError(
                f"{SCHEMA_GROUPS_KEY}: '{group}' is also a schema name in {shared_path}"
            )
        if not isinstance(members, list) or not members:
            raise ValueError(
                f"{SCHEMA_GROUPS_KEY}: '{group}' must be a non-empty list in {shared_path}"
            )
        for member in members:
            if member not in schemas:
                raise ValueError(
                    f"{SCHEMA_GROUPS_KEY}: '{group}' names unknown schema '{member}' "
                    f"in {shared_path}"
                )


def _is_selected(
        inclusions: Any,
        exclusions: Any,
        database_name: str,
        version: Optional[str]) -> bool:
    """Check whether a database/version passes an inclusion and exclusion list.

    Args:
        inclusions: Inclusion list; if non-empty, the database/version must
            match one of its entries. None/empty means no restriction.
        exclusions: Exclusion list; the database/version must match none of
            its entries. None/empty excludes nothing.
        database_name: Database slug from the URL.
        version: Resolved version, or None if unversioned.

    Returns:
        True if the database/version is included and not excluded.

    Raises:
        ValueError: If a list or one of its entries is malformed.
    """
    if inclusions and not _matches_any(inclusions, database_name, version):
        return False
    return not _matches_any(exclusions, database_name, version)


def _matches_any(entries: Any, database_name: str, version: Optional[str]) -> bool:
    """Check whether a database/version matches any entry of an include/exclude list.

    Args:
        entries: List of ``"<slug>[/<version>]"`` strings or
            ``{slug, version}`` mappings, or None.
        database_name: Database slug from the URL.
        version: Resolved version, or None if unversioned.

    Returns:
        True if any entry matches.

    Raises:
        ValueError: If the list or one of its entries is malformed.
    """
    if not entries:
        return False
    if not isinstance(entries, list):
        raise ValueError(f"Include/exclude list must be a list, got {type(entries).__name__}")

    for entry in entries:
        slug, entry_version = _parse_selection_entry(entry)
        if slug != database_name:
            continue
        if entry_version == ALL_VERSIONS:
            return True
        if version is not None and _versions_equal(version, entry_version):
            return True
    return False


def _parse_selection_entry(entry: Any) -> Tuple[str, str]:
    """Parse one include/exclude entry into (slug, version spec).

    Args:
        entry: ``"<slug>"``, ``"<slug>/<version>"``, or ``{slug, version}``.
            A missing version means all versions.

    Returns:
        Tuple of (slug, version) where version may be ALL_VERSIONS.

    Raises:
        ValueError: If the entry has no slug or an unsupported type.
    """
    if isinstance(entry, str):
        slug, _, entry_version = entry.strip().strip("/").partition("/")
    elif isinstance(entry, dict):
        slug = str(entry.get("slug") or "")
        raw_version = entry.get("version")
        entry_version = ALL_VERSIONS if raw_version is None else str(raw_version)
    else:
        raise ValueError(f"Invalid include/exclude entry: {entry!r}")

    if not slug:
        raise ValueError(f"Include/exclude entry has no slug: {entry!r}")
    return (slug, entry_version.strip() or ALL_VERSIONS)


def _versions_equal(version: str, spec: str) -> bool:
    """Compare a version stem to an include/exclude version, tolerating float forms.

    So "3.0" matches a YAML ``3.0`` or ``3`` as well as ``"3.0"``.

    Args:
        version: The resolved version stem.
        spec: The configured include/exclude version.

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
        defaults: Dict[str, Any]) -> Optional[Tuple[str, Dict[str, Any], Optional[str]]]:
    """Split a table definition into its location, metadata and connection.

    A string or a dict with ``uri``/``path`` is a file table; a dict with
    ``table`` is a PostgreSQL table, whose connection comes from the table or,
    failing that, from ``meta``. File tables never get a connection.

    Args:
        table_def: A URI string, a dict with ``uri``/``path`` plus metadata, or a
            dict with ``table`` (and ``connection``) for a PostgreSQL table.
        defaults: Shared ``meta`` defaults; the table's own keys override them.

    Returns:
        Tuple of (uri or PostgreSQL table name, metadata, connection name or
        None), or None if the definition has neither a URI nor a table name.
    """
    file_defaults = {k: v for k, v in defaults.items() if k != TABLE_CONNECTION_KEY}
    if isinstance(table_def, str):
        return (table_def, file_defaults, None)
    if not isinstance(table_def, dict):
        return None
    if POSTGRES_TABLE_KEY in table_def:
        metadata = {**defaults, **table_def}
        connection = metadata.pop(TABLE_CONNECTION_KEY, None)
        table = metadata.pop(POSTGRES_TABLE_KEY)
        return (str(table), metadata, None if connection is None else str(connection))
    uri = table_def.get("uri") or table_def.get("path")
    if not uri:
        return None
    own = {k: v for k, v in table_def.items() if k not in ("uri", "path")}
    return (str(uri), {**file_defaults, **own}, None)


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
"""

Database Configuration Module for API Dock

Handles loading and parsing of database configuration files for SQL-based routes.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import os
from functools import partial
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml

from api_dock.postgres_config import check_postgres_config
from api_dock.resolvers import (
    BIND_KEY,
    RESOLVE_KEY,
    RESOLVERS_KEY,
    check_params_declared,
    check_resolve_list,
    check_resolvers,
    check_table_value,
    check_value_uses,
    get_resolvers,
    resolver_values_in,
)
from api_dock.route_headers import HEADERS_KEY, check_route_headers, route_path_variables
from api_dock.sql_template_check import (
    check_comment_at_end,
    check_commented_variables,
    check_quoted_variables,
)
from api_dock.templated_tables import (
    TEMPLATED_TABLE_KEYS,
    check_table,
    is_templated_table,
    table_names_in,
    table_sources,
    table_value_name,
)


#
# CONSTANTS
#
DATABASES_DIR: str = "databases"

# The backend: key selects the database that runs a config's SQL.
BACKEND_KEY: str = "backend"
DUCKDB_BACKEND: str = "duckdb"
POSTGRES_BACKEND: str = "postgres"
BACKENDS: Tuple[str, ...] = (DUCKDB_BACKEND, POSTGRES_BACKEND)

# internal: true hides a database from HTTP requests, metadata and listings.
INTERNAL_KEY: str = "internal"

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


def get_backend_name(database_config: Dict[str, Any]) -> str:
    """Get the backend a database config uses.

    Args:
        database_config: Database configuration dictionary.

    Returns:
        ``duckdb`` when there is no backend key, otherwise the key's value.

    Raises:
        ValueError: If the value is not a known backend.
    """
    backend = database_config.get(BACKEND_KEY, DUCKDB_BACKEND)
    if backend not in BACKENDS:
        raise ValueError(
            f"unknown backend '{backend}'; use one of {', '.join(BACKENDS)}"
        )
    return backend


def is_internal_database(database_config: Dict[str, Any]) -> bool:
    """Return whether a checked database config sets ``internal: true``."""
    return database_config.get(INTERNAL_KEY, False) is True


def needs_startup_snapshot(database_config: Any) -> bool:
    """Return whether a config uses a setting that is read only at startup.

    These settings are ``internal:``, ``resolvers:``, route ``resolve:`` and
    ``headers:``, and templated tables with their ``format:``, ``files:`` and
    ``allow:`` keys. A key counts even if its value is malformed, so a live
    config can't start to use one.

    Args:
        database_config: Database config as loaded from its file.

    Returns:
        True if the config has one of these keys.
    """
    if not isinstance(database_config, dict):
        return False
    if INTERNAL_KEY in database_config or RESOLVERS_KEY in database_config:
        return True
    tables = database_config.get('tables')
    if isinstance(tables, dict) and any(_is_startup_table(table) for table in tables.values()):
        return True
    routes = database_config.get('routes')
    if not isinstance(routes, list):
        return False
    return any(
        isinstance(route, dict) and (HEADERS_KEY in route or RESOLVE_KEY in route)
        for route in routes
    )


def route_sql_leaves(route_config: Dict[str, Any], database_config: Dict[str, Any]) -> List[str]:
    """List the base SQL templates a route can run, with [[query]] references expanded.

    Args:
        route_config: Route configuration.
        database_config: Database configuration with its queries.

    Returns:
        One SQL template per selector leaf.
    """
    leaves = [
        node if isinstance(node, str) else node['sql']
        for _, node in _selector_leaves(route_config.get('sql', ''), 'sql')
    ]
    return [
        _expand_named_query(leaf, database_config) for leaf in leaves if isinstance(leaf, str)
    ]


def check_database_config(database_config: Dict[str, Any]) -> None:
    """Check the backend, every named query and every route of a loaded database config.

    PostgreSQL configs also have their connection fields, resource limits and
    table names checked, and E'...' strings are recognised in their templates.
    Each route is merged with top-level query_params, as a request would be,
    then checked for shape and for {{variables}} inside quotes or comments in
    any template whose values are bound. sql_append templates are not checked
    for variables because their values are written into the SQL text. No
    template may end inside a comment. ``internal:`` must be true or false,
    and route ``headers:`` are checked with check_route_headers().
    ``resolvers:``, templated tables and each route's resolver values are
    checked too. Checks that need the resolver's target are in
    resolver_targets.

    Args:
        database_config: Database configuration, already merged with the main config.

    Raises:
        ValueError: If a query or route fails a check. The message names the
            route (or query) and the template's location.
    """
    _check_internal(database_config)
    check_resolvers(database_config)
    is_postgres = get_backend_name(database_config) == POSTGRES_BACKEND
    _check_tables(database_config, is_postgres)
    if is_postgres:
        check_postgres_config(database_config)
    bound_checks, append_checks = _template_checks(is_postgres)

    for query_name, query in database_config.get('queries', {}).items():
        _check_template(f"queries.{query_name}", query, bound_checks)

    for index, route_config in enumerate(database_config.get('routes', [])):
        try:
            validate_route_config(route_config)
            merged = merge_query_params(route_config, database_config)
            validate_route_config(merged)
            _check_route_resolvers(merged, database_config)
            check_route_headers(route_config, _header_resolver_values(merged, database_config))
            for location, template in _bound_templates(merged):
                _check_template(location, template, bound_checks)
            for location, template in _append_templates(merged):
                _check_template(location, template, append_checks)
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
def _check_internal(database_config: Dict[str, Any]) -> None:
    """Refuse an ``internal:`` value that is not true or false."""
    value = database_config.get(INTERNAL_KEY, False)
    if not isinstance(value, bool):
        raise ValueError(f"internal: must be true or false, not {value!r}")


def _is_startup_table(table_def: Any) -> bool:
    """Return whether a table is templated or has a templated-table key."""
    if is_templated_table(table_def):
        return True
    return isinstance(table_def, dict) and any(key in table_def for key in TEMPLATED_TABLE_KEYS)


def _check_tables(database_config: Dict[str, Any], is_postgres: bool) -> None:
    """Check the templated-table rules of every table, and the resolver each one uses."""
    resolvers = get_resolvers(database_config)
    for table_name, table_def in database_config.get('tables', {}).items():
        try:
            check_table(table_name, table_def, is_postgres)
            if is_templated_table(table_def):
                check_table_value(table_def, table_value_name(table_def), resolvers)
        except ValueError as error:
            raise ValueError(f"tables.{table_name}: {error}") from error


def _check_route_resolvers(route_config: Dict[str, Any], database_config: Dict[str, Any]) -> None:
    """Check a route's resolve: list and every resolver value it uses.

    Values count in bound SQL (after [[query]] expansion), in headers and in
    the URI of each templated table that the route's SQL names. sql_append
    templates can't use a value or read a templated table, because their
    values are written into the SQL text.

    Args:
        route_config: Route configuration, merged with top-level query_params.
        database_config: Database configuration.

    Raises:
        ValueError: If a check fails. The message gives the reason.
    """
    resolvers = get_resolvers(database_config)
    check_resolve_list(route_config, resolvers)
    check_params_declared(
        route_config, resolvers, route_path_variables(route_config.get('route', ''))
    )
    sql_templates = _value_templates(route_config, database_config)
    check_value_uses(sql_templates + _header_templates(route_config), route_config, resolvers)
    _check_table_uses(route_config, database_config, sql_templates)
    for location, template in _append_templates(route_config):
        if isinstance(template, str):
            _check_append_template(location, template, database_config, resolvers)


def _check_table_uses(
        route_config: Dict[str, Any], database_config: Dict[str, Any],
        templates: List[str]) -> None:
    """Refuse a route that reads a templated table without listing its resolver."""
    tables = database_config.get('tables', {})
    listed = route_config.get(RESOLVE_KEY, [])
    for template in templates:
        for table_name in table_names_in(template):
            table_def = tables.get(table_name)
            if not is_templated_table(table_def):
                continue
            value_name = table_value_name(table_def)
            resolver = value_name.partition('.')[0]
            if resolver not in listed:
                raise ValueError(
                    f"reads table '{table_name}', which uses '{{{{{value_name}}}}}', but does "
                    f"not list '{resolver}' in resolve:"
                )


def _check_append_template(
        location: str, template: str, database_config: Dict[str, Any],
        resolvers: Dict[str, Dict[str, Any]]) -> None:
    """Refuse a resolver value or a templated table source in an sql_append template."""
    values = resolver_values_in(template, resolvers)
    if values:
        resolver, column = values[0]
        raise ValueError(
            f"{location}: can't use the resolver value '{{{{{resolver}.{column}}}}}'"
        )
    tables = database_config.get('tables', {})
    for table_name in table_sources(template):
        if is_templated_table(tables.get(table_name)):
            raise ValueError(f"{location}: can't read the templated table '{table_name}'")


def _header_templates(route_config: Dict[str, Any]) -> List[str]:
    """List a route's header templates; check_route_headers() checks their shape."""
    headers = route_config.get(HEADERS_KEY, {})
    if not isinstance(headers, dict):
        return []
    return [template for template in headers.values() if isinstance(template, str)]


def _header_resolver_values(
        route_config: Dict[str, Any], database_config: Dict[str, Any]) -> List[str]:
    """List the ``<resolver>.<column>`` names that a route's headers can use."""
    resolvers = get_resolvers(database_config)
    return [
        f"{name}.{column}"
        for name in route_config.get(RESOLVE_KEY, [])
        for column in resolvers[name].get(BIND_KEY, [])
    ]


def _value_templates(route_config: Dict[str, Any], database_config: Dict[str, Any]) -> List[str]:
    """List a route's bound SQL templates, with [[query]] references expanded."""
    return [
        _expand_named_query(template, database_config)
        for _, template in _bound_templates(route_config)
        if isinstance(template, str)
    ]


def _expand_named_query(template: str, database_config: Dict[str, Any]) -> str:
    """Return the named query's text if template is one [[query]] reference.

    This is the rule that sql_builder uses for a route's base SQL.
    """
    stripped = template.strip()
    if stripped.startswith("[[") and stripped.endswith("]]"):
        query = database_config.get('queries', {}).get(stripped[2:-2])
        if isinstance(query, str):
            return query
    return template


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


def _template_checks(escape_strings: bool) -> Tuple[
        Tuple[Callable[[str], None], ...], Tuple[Callable[[str], None], ...]]:
    """Return bound and append checks, recognizing PostgreSQL escape strings if enabled."""
    if not escape_strings:
        return BOUND_TEMPLATE_CHECKS, APPEND_TEMPLATE_CHECKS
    return (
        tuple(partial(check, escape_strings=True) for check in BOUND_TEMPLATE_CHECKS),
        tuple(partial(check, escape_strings=True) for check in APPEND_TEMPLATE_CHECKS),
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
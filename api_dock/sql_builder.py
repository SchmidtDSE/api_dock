"""

SQL Query Builder Module for API Dock

Builds SQL queries with table and parameter substitution for database routes.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from api_dock.database_config import (
    ALL_SCHEMAS,
    DATABASE_SCHEMA_KEY,
    EXCLUDE_SELF_SUFFIX,
    get_named_query,
    IDENTIFIER_PATTERN,
    resolve_schema_union,
    resolve_table_reference,
    SCHEMA_SEPARATOR,
)
from api_dock.sql_template_check import QUOTED_VARIABLE_PATTERN
from api_dock.types import SqlContext, TableReference


#
# CONSTANTS
#
# Query-param values treated as "falsy" for _truthy/_falsy matching in the sql
# selector. Comparison is case-insensitive after stripping surrounding space.
FALSY_VALUES: frozenset = frozenset({"", "0", "false", "no", "off", "null", "none"})

# Response returned when an sql selector matches no branch and defines no
# default (else / trailing string / _default) or no_match override.
DEFAULT_NO_MATCH_RESPONSE: Dict[str, Any] = {
    "error": "No matching query configuration for the given parameters",
    "http_status": 400,
}

# Marker written into SQL for each bound value (DuckDB's positional style).
SQL_MARKER: str = "?"

# A {{variable}} placeholder; group 1 is the variable name.
VARIABLE_PATTERN: re.Pattern[str] = re.compile(r'\{\{([^{}]+)\}\}')


# Route key selecting which source facts to add as columns to [[*.table]] /
# [[group.table]] union rows, and each fact's default column name.
SOURCE_COLUMNS_KEY: str = "source_columns"
SOURCE_COLUMN_DEFAULTS: Dict[str, str] = {
    "schema": "schema_name",
    "name": "name",
    "version": "version",
}

# {{self.<fact>}} placeholders for the database/version being queried.
SELF_PARAM_PREFIX: str = "self."

# Sentinel signalling that a selector rule did not fire (distinct from a rule
# that fires but resolves to an empty base SQL).
_NO_MATCH: Any = object()


#
# PUBLIC
#
class SqlSelectionError(Exception):
    """Raised when an sql selector matches no branch and defines no default.

    Carries the JSON response body and HTTP status to return to the client, so
    the caller can surface a proper URL error (default 400) rather than a
    generic 500.

    Attributes:
        response: JSON-serializable response body to return to the client.
        status_code: HTTP status code to return.
    """

    def __init__(self, response: Dict[str, Any]) -> None:
        """Initialize the error from a response spec.

        Args:
            response: Response body dict; its ``http_status`` (default 400)
                becomes the HTTP status code.
        """
        self.response = response
        self.status_code = int(response.get("http_status", 400))
        super().__init__(str(response.get("error", "No matching query configuration")))


def build_sql_query(
        route_config: Dict[str, Any],
        database_config: Dict[str, Any],
        path_params: Optional[Dict[str, str]] = None,
        query_params: Optional[Dict[str, str]] = None,
        cookies: Optional[Dict[str, str]] = None,
        multi_query_params: Optional[Dict[str, List[str]]] = None,
        shared_config: Optional[Dict[str, Any]] = None,
        context: Optional[SqlContext] = None) -> Tuple[str, List[Optional[str]]]:
    """Build SQL query text and the values to bind to its markers.

    Each ``{{var}}`` in the base SQL, named queries, and WHERE fragments is
    written as a ``?`` marker and its value is added to the returned list, so
    request values never become SQL text. ``sql_append`` values are the
    exception: they pass an allowed-character check and are written into the text.

    Args:
        route_config: Route configuration dictionary with sql and query_params.
        database_config: Database configuration dictionary with tables definitions.
        path_params: Dictionary of path parameters extracted from the route.
        query_params: Dictionary of query parameters from URL.
        cookies: Dictionary of cookie values from request.
        multi_query_params: Dictionary mapping query parameter names to the full
            list of values received (for keys repeated in the URL). When a param
            has a ``multivalue_sql`` template and more than one value was passed,
            that template is used instead of ``sql``.
        shared_config: The shared ``database`` mapping from
            ``databases/config.yaml`` (schemas, global tables, meta), or None.
        context: Request context (database name/version, schema groups and
            sources) for unions, ``source_columns`` and ``{{self.*}}``.

    Returns:
        Tuple of ``(sql, values)``: the SQL text with ``?`` markers and the
        values in the order the markers appear.

    Raises:
        ValueError: If a referenced table or query is not defined in config, a
            bound ``{{var}}`` has no value, or an ``sql_append`` value fails the
            allowed-character check.
        SqlSelectionError: If an sql selector matches no branch.
    """
    sql_query, values, _ = build_sql_query_with_tables(
        route_config, database_config, path_params, query_params, cookies,
        multi_query_params, shared_config, context
    )
    return sql_query, values


def build_sql_query_with_tables(
        route_config: Dict[str, Any],
        database_config: Dict[str, Any],
        path_params: Optional[Dict[str, str]] = None,
        query_params: Optional[Dict[str, str]] = None,
        cookies: Optional[Dict[str, str]] = None,
        multi_query_params: Optional[Dict[str, List[str]]] = None,
        shared_config: Optional[Dict[str, Any]] = None,
        context: Optional[SqlContext] = None
) -> Tuple[str, List[Optional[str]], List[TableReference]]:
    """Build a SQL query, its bound values, and the tables it references.

    Same as build_sql_query, but also returns every table resolved from a
    ``[[...]]`` reference, so the caller can set up storage authentication and
    create views for ``[[schema.table]]`` references before executing.

    Args:
        route_config: Route configuration dictionary with sql and query_params.
        database_config: Database configuration dictionary with tables definitions.
        path_params: Dictionary of path parameters extracted from the route.
        query_params: Dictionary of query parameters from URL.
        cookies: Dictionary of cookie values from request.
        multi_query_params: Full value lists for repeated query keys.
        shared_config: The shared ``database`` mapping, or None.
        context: Request context for unions, ``source_columns`` and
            ``{{self.*}}`` placeholders, or None.

    Returns:
        Tuple of (sql_query, values, table_references): the SQL with ``?``
        markers, the values in marker order, and the referenced tables
        (unique by SQL name, in first-reference order).

    Raises:
        ValueError: If a referenced table or query is not defined in config, a
            bound ``{{var}}`` has no value, a union is misused,
            ``source_columns`` is invalid, or an ``sql_append`` value fails the
            allowed-character check.
        SqlSelectionError: If an sql selector matches no branch.
    """
    table_refs: Dict[str, TableReference] = {}
    if context is None:
        context = SqlContext()
    source_columns = _normalize_source_columns(route_config.get(SOURCE_COLUMNS_KEY))

    def expand_tables(text: str) -> str:
        """Expand [[...]] table references, recording each referenced table."""
        return _substitute_table_references(
            text, database_config, shared_config, table_refs, context, source_columns
        )

    if path_params is None:
        path_params = {}
    if query_params is None:
        query_params = {}
    if cookies is None:
        cookies = {}
    if multi_query_params is None:
        multi_query_params = {}

    # Resolve the base SQL via the selector. ``sql`` may be a plain string or a
    # rule-list decision tree; selection is driven by path, query, and cookie
    # params. branch_appends are post-WHERE clauses (e.g. GROUP BY) contributed
    # by the selected branch, applied before route-level sql_append fragments.
    selection_params = {**path_params, **query_params}
    selection_params.update({f"cookies.{key}": value for key, value in cookies.items()})
    sql_template, branch_appends = resolve_route_sql(route_config, selection_params)

    sql_template = _resolve_named_query(sql_template, database_config)

    params = _substitution_params(route_config, path_params, query_params, cookies)
    self_params = _self_params(database_config, context)
    params.update(self_params)

    # Each piece is bound on its own and pieces are joined in the order they
    # appear in the final SQL, so the value list stays in marker order.
    # Strip whitespace/newlines from base SQL (YAML block scalars add trailing \n)
    sql_query, values = _bind_variables(expand_tables(sql_template).strip(), params)

    where_fragments = build_where_clause_from_params(
        route_config, query_params, path_params, multi_query_params, cookies, self_params
    )
    sql_query, values = _add_where_fragments(sql_query, values, where_fragments, expand_tables)

    post_where = _post_where_clauses(
        route_config, branch_appends, query_params, path_params, params, expand_tables
    )
    if post_where:
        sql_query += ' ' + ' '.join(post_where)

    return (sql_query, values, list(table_refs.values()))


def build_schema_view_statements(table_refs: List[TableReference]) -> List[str]:
    """Build DuckDB statements exposing ``[[schema.table]]`` references as views.

    Each qualified reference becomes ``CREATE SCHEMA IF NOT EXISTS <schema>``
    plus ``CREATE OR REPLACE VIEW <schema>.<table>`` over the table's URI, so SQL
    can address it as ``schema.table`` (and columns as ``schema.table.col``).
    Unqualified references are inlined and need no view.

    Args:
        table_refs: Table references from build_sql_query_with_tables.

    Returns:
        SQL statements to execute (after storage auth) before the query.
    """
    statements: List[str] = []
    schemas_created = set()
    for reference in table_refs:
        if not reference.qualified or not reference.schema:
            continue
        if reference.schema not in schemas_created:
            statements.append(f"CREATE SCHEMA IF NOT EXISTS {reference.schema}")
            schemas_created.add(reference.schema)
        statements.append(
            f"CREATE OR REPLACE VIEW {reference.sql_name} AS "
            f"SELECT * FROM {_escape_sql_value(reference.uri)}"
        )
    return statements


def check_table_references(
        template: str,
        database_config: Dict[str, Any],
        shared_config: Optional[Dict[str, Any]] = None,
        schema_groups: Optional[Dict[str, List[str]]] = None,
        source_columns: Any = None) -> None:
    """Check that every ``[[...]]`` reference in a template resolves.

    Uses the same expansion as a request, so unknown tables, schemas and schema
    groups, unions outside FROM/JOIN, misused ``!`` selectors, and invalid
    ``source_columns`` are all reported. A template that is exactly a named
    query reference (``[[query_name]]``) is skipped; named queries are checked
    on their own.

    Args:
        template: SQL template to check.
        database_config: The version database configuration.
        shared_config: The shared ``database`` mapping, or None.
        schema_groups: The shared ``schema_groups`` mapping, or None.
        source_columns: The route's ``source_columns`` value, or None.

    Raises:
        ValueError: If a reference doesn't resolve.
    """
    stripped = template.strip()
    if _is_named_query_reference(stripped, database_config):
        return
    _substitute_table_references(
        stripped, database_config, shared_config, None,
        SqlContext(schema_groups=schema_groups or {}),
        _normalize_source_columns(source_columns),
    )


def resolve_route_sql(
        route_config: Dict[str, Any],
        selection_params: Dict[str, str]) -> Tuple[str, List[str]]:
    """Resolve a route's ``sql`` selector to a base SQL string and append clauses.

    The ``sql`` value may be a plain string (used directly) or a list of rules
    forming a first-match-wins decision tree. Each rule selects an sql node based
    on the presence and value of params. An sql node is itself a string, a leaf
    object (``{sql, sql_append}``), or a nested rule list.

    Rule forms (evaluated top-to-bottom, first match wins):
      - ``{when: <param>, then: <node>}`` — fires when the param is truthy.
      - ``{when: <param>, equals: <valspec>, then: <node>}`` — fires on a value.
      - ``{when: <param>, match: {<valspec>: <node>, ...}}`` — value map.
      - ``{when: [<params>], then: <node>}`` — fires when all params are truthy.
      - ``{when: [<params>], match: [{values: [...], then: <node>}, ...]}``
      - ``{else: <node>}`` or a trailing bare string — the default.
      - ``{no_match: <response>}`` — raise a URL error instead of a default.

    Args:
        route_config: Route configuration dictionary. ``sql`` is a string or a
            list of selector rules.
        selection_params: Merged params available for matching, keyed by name
            (path and query params, plus cookies as ``cookies.<name>``). A key
            is "present" iff it appears here.

    Returns:
        Tuple of ``(base_sql, branch_appends)`` where ``branch_appends`` are
        post-WHERE SQL fragments contributed by the selected branch.

    Raises:
        SqlSelectionError: If no branch matches and no default/no_match is given.
    """
    sql_spec = route_config.get('sql', '')
    resolved = _resolve_sql_node(sql_spec, selection_params)
    if resolved is None:
        raise SqlSelectionError(dict(DEFAULT_NO_MATCH_RESPONSE))
    return resolved


def process_query_parameters(
        route_config: Dict[str, Any],
        query_params: Dict[str, str],
        path_params: Dict[str, str],
        cookies: Optional[Dict[str, str]] = None
) -> Tuple[bool, Any, int, Optional[str]]:
    """Process query parameters according to declarative configuration.

    Args:
        route_config: Route configuration dictionary with query_params section.
        query_params: Dictionary of query parameters from URL.
        path_params: Dictionary of path parameters for variable substitution.
        cookies: Dictionary of cookie values from request.

    Returns:
        Tuple of (should_return_early, response_data, status_code, error_message)
        If should_return_early=True, return response_data immediately
        If False, continue with SQL building
    """
    if cookies is None:
        cookies = {}

    query_param_configs = route_config.get('query_params', [])
    if not query_param_configs:
        return (False, None, 200, None)

    # Combine path and query parameters for variable substitution
    all_params = {**path_params, **query_params}

    # Add cookies with "cookies." prefix for substitution
    cookies_params = {f"cookies.{key}": value for key, value in cookies.items()}
    all_params.update(cookies_params)

    # Process each parameter configuration
    for param_item in query_param_configs:
        if not isinstance(param_item, dict) or len(param_item) != 1:
            continue

        param_name, param_config = next(iter(param_item.items()))
        param_value = query_params.get(param_name)

        # Process direct response parameters first (highest priority)
        if 'response' in param_config and param_value is not None:
            response_data = param_config['response']
            # Substitute variables in response if it's a dictionary
            if isinstance(response_data, dict):
                response_data = _substitute_variables_in_dict(response_data, all_params)
            elif isinstance(response_data, str):
                response_data = _substitute_variables_in_string(response_data, all_params)
            return (True, response_data, 200, None)

        # Process conditional parameters
        if 'conditional' in param_config and param_value is not None:
            conditional_config = param_config['conditional']
            if param_value in conditional_config:
                condition_config = conditional_config[param_value]

                # Check for response in condition
                if 'response' in condition_config:
                    response_data = condition_config['response']
                    if isinstance(response_data, dict):
                        response_data = _substitute_variables_in_dict(response_data, all_params)
                    elif isinstance(response_data, str):
                        response_data = _substitute_variables_in_string(response_data, all_params)
                    return (True, response_data, 200, None)

                # Check for action in condition
                if 'action' in condition_config:
                    try:
                        action_result = execute_parameter_action(condition_config, all_params)
                        return (True, action_result, 200, None)
                    except Exception as e:
                        return (True, {"error": f"Action execution failed: {str(e)}"}, 500, None)

            # Check for default condition if param_value doesn't match any condition
            elif 'default' in conditional_config:
                default_config = conditional_config['default']
                if 'response' in default_config:
                    response_data = default_config['response']
                    if isinstance(response_data, dict):
                        response_data = _substitute_variables_in_dict(response_data, all_params)
                    elif isinstance(response_data, str):
                        response_data = _substitute_variables_in_string(response_data, all_params)
                    return (True, response_data, 200, None)

        # Process required parameters
        if param_config.get('required', False) and param_value is None:
            if 'missing_response' in param_config:
                missing_response = param_config['missing_response']
                status_code = missing_response.get('http_status', 400)
                return (True, missing_response, status_code, None)
            else:
                return (True, {"error": f"Required parameter '{param_name}' is missing"}, 400, None)

    # No early returns triggered, continue with SQL building
    return (False, None, 200, None)


def build_where_clause_from_params(
        route_config: Dict[str, Any],
        query_params: Dict[str, str],
        path_params: Dict[str, str],
        multi_query_params: Optional[Dict[str, List[str]]] = None,
        cookies: Optional[Dict[str, str]] = None,
        extra_params: Optional[Dict[str, Optional[str]]] = None
) -> List[Tuple[str, List[Optional[str]]]]:
    """Build WHERE clause fragments, with their bound values, from parameter configurations.

    Args:
        route_config: Route configuration dictionary with query_params section.
        query_params: Dictionary of query parameters from URL.
        path_params: Dictionary of path parameters.
        multi_query_params: Dictionary mapping query parameter names to the full
            list of values received. When a param has a ``multivalue_sql``
            template and more than one value was passed for it, that template is
            used (with ``{{param}}`` expanded to one marker per value) instead
            of the single-value ``sql`` template.
        cookies: Dictionary of cookie values, available as ``{{cookies.<name>}}``.
        extra_params: Additional values fragments may reference (e.g. the
            ``{{self.*}}`` placeholders).

    Returns:
        List of ``(fragment, values)`` pairs in config order. Fragments are to be
        joined with AND; each fragment's values follow its markers in order.

    Raises:
        ValueError: If a fragment references a variable with no value.
    """
    if multi_query_params is None:
        multi_query_params = {}
    if cookies is None:
        cookies = {}

    query_param_configs = route_config.get('query_params', [])
    where_fragments = []

    all_params = _substitution_params(route_config, path_params, query_params, cookies)
    all_params.update(extra_params or {})

    for param_item in query_param_configs:
        if not isinstance(param_item, dict) or len(param_item) != 1:
            continue

        param_name, param_config = next(iter(param_item.items()))
        param_value = query_params.get(param_name)

        # Skip parameters that have response, action, or sql_append configurations
        if 'response' in param_config:
            continue
        if 'sql_append' in param_config:
            continue

        # Skip value-only params (no sql/multivalue_sql/conditional/response/action)
        if ('sql' not in param_config and 'multivalue_sql' not in param_config
                and 'conditional' not in param_config
                and 'response' not in param_config and 'action' not in param_config):
            continue

        # Handle multivalue params: use multivalue_sql when more than one value
        # was passed for this key in the URL (e.g. ?id=1&id=2).
        param_values = multi_query_params.get(param_name)
        if ('multivalue_sql' in param_config and param_values is not None
                and len(param_values) > 1):
            _append_bound_fragment(
                where_fragments, param_config['multivalue_sql'], all_params,
                {param_name: param_values}
            )
            continue

        # Handle conditional parameters that have SQL
        if 'conditional' in param_config and param_value is not None:
            conditional_config = param_config['conditional']
            if param_value in conditional_config and 'sql' in conditional_config[param_value]:
                sql_fragment = conditional_config[param_value]['sql']
                if sql_fragment:  # Skip empty SQL fragments
                    _append_bound_fragment(where_fragments, sql_fragment, all_params)
            continue

        # Handle regular SQL parameters
        if 'sql' in param_config:
            sql_fragment = param_config['sql']

            # Handle parameters with default values (always include)
            if 'default' in param_config:
                # Use provided value or default
                effective_value = param_value if param_value is not None else param_config['default']
                effective_params = {**all_params, param_name: str(effective_value)}
                _append_bound_fragment(where_fragments, sql_fragment, effective_params)

            # Handle optional parameters (only include if provided)
            elif param_value is not None:
                _append_bound_fragment(where_fragments, sql_fragment, all_params)

    return where_fragments


def build_append_clause_from_params(
        route_config: Dict[str, Any],
        query_params: Dict[str, str],
        path_params: Dict[str, str]
) -> List[str]:
    """Build post-WHERE SQL fragments from sql_append parameter configurations.

    Collects sql_append fragments (ORDER BY, LIMIT, OFFSET, etc.) in config order.
    Uses raw (unquoted) substitution since these values are identifiers, keywords,
    or integers — not string literals.

    Args:
        route_config: Route configuration dictionary with query_params section.
        query_params: Dictionary of query parameters from URL.
        path_params: Dictionary of path parameters.

    Returns:
        List of SQL fragments to append after WHERE clause, in config order.
    """
    query_param_configs = route_config.get('query_params', [])
    append_fragments = []

    # Combine all params and apply defaults for cross-param references
    all_params = {**path_params, **query_params}
    all_params = _apply_default_values(route_config, all_params)

    for param_item in query_param_configs:
        if not isinstance(param_item, dict) or len(param_item) != 1:
            continue

        param_name, param_config = next(iter(param_item.items()))

        if 'sql_append' not in param_config:
            continue

        param_value = query_params.get(param_name)
        sql_append_fragment = param_config['sql_append']

        # Handle parameters with default values (always include)
        if 'default' in param_config:
            effective_value = param_value if param_value is not None else str(param_config['default'])
            effective_params = {**all_params, param_name: effective_value}
            substituted = _substitute_variables_raw(sql_append_fragment, effective_params).strip()
            if substituted:
                append_fragments.append(substituted)

        # Handle optional parameters (only include if provided)
        elif param_value is not None:
            substituted = _substitute_variables_raw(sql_append_fragment, all_params).strip()
            if substituted:
                append_fragments.append(substituted)

    return append_fragments


def execute_parameter_action(action_config: Dict[str, Any], all_params: Dict[str, str]) -> Any:
    """Execute custom action defined in parameter configuration.

    Args:
        action_config: Action configuration dictionary.
        all_params: Combined path and query parameters.

    Returns:
        Action result (JSON response, string, or other data)
    """
    # For now, return a placeholder response
    # In full implementation, this would dynamically import and execute the specified method
    action_name = action_config.get('action', 'unknown_action')

    return {
        "action_executed": action_name,
        "message": f"Custom action '{action_name}' would be executed here",
        "parameters": all_params
    }


def validate_required_parameters(
        route_config: Dict[str, Any],
        query_params: Dict[str, str]
) -> Optional[Tuple[Any, int]]:
    """Validate required parameters and return error if missing.

    Args:
        route_config: Route configuration dictionary.
        query_params: Dictionary of query parameters from URL.

    Returns:
        None if all required params present, otherwise (error_response, status_code)
    """
    query_param_configs = route_config.get('query_params', [])

    for param_item in query_param_configs:
        if not isinstance(param_item, dict) or len(param_item) != 1:
            continue

        param_name, param_config = next(iter(param_item.items()))

        if param_config.get('required', False) and param_name not in query_params:
            if 'missing_response' in param_config:
                missing_response = param_config['missing_response']
                status_code = missing_response.get('http_status', 400)
                return (missing_response, status_code)
            else:
                return ({"error": f"Required parameter '{param_name}' is missing"}, 400)

    return None


def extract_path_parameters(path: str, pattern: str) -> Dict[str, str]:
    """Extract parameters from a path using a route pattern.

    Args:
        path: The actual path (e.g., "users/123/permissions").
        pattern: The route pattern (e.g., "users/{{user_id}}/permissions").

    Returns:
        Dictionary mapping parameter names to values.
    """
    path_parts = path.strip("/").split("/")
    pattern_parts = pattern.strip("/").split("/")

    if len(path_parts) != len(pattern_parts):
        return {}

    params = {}
    for path_part, pattern_part in zip(path_parts, pattern_parts):
        if pattern_part.startswith("{{") and pattern_part.endswith("}}"):
            # Extract parameter name
            param_name = pattern_part[2:-2]
            params[param_name] = path_part

    return params


#
# INTERNAL
#
def _substitute_table_references(
        sql: str,
        database_config: Dict[str, Any],
        shared_config: Optional[Dict[str, Any]] = None,
        collected: Optional[Dict[str, TableReference]] = None,
        context: Optional[SqlContext] = None,
        source_columns: Optional[List[Tuple[str, str]]] = None) -> str:
    """Substitute [[table_name]] references with table file paths in FROM clauses.

    Unqualified tables (version ``tables``, the version's shared schema, or
    shared global tables) expand to ``'<uri>' AS name`` after FROM/JOIN and to
    ``name`` elsewhere. Qualified ``[[schema.table]]`` references expand to the
    view name ``schema.table`` after FROM/JOIN (no alias, so a user alias or
    ``schema.table.col`` still works) and to ``table`` elsewhere (DuckDB does
    not accept ``schema.table.*``). Union references (``[[*.table]]``,
    ``[[*!.table]]``, ``[[group.table]]``, ``[[group!.table]]``) expand, after
    FROM/JOIN only, to a parenthesized ``UNION ALL BY NAME`` over the member
    schema views; the SQL must give it an alias.

    Args:
        sql: SQL query template with [[table_name]] placeholders.
        database_config: Database configuration dictionary.
        shared_config: The shared ``database`` mapping, or None.
        collected: Optional dict filled with each resolved TableReference,
            keyed by its SQL name.
        context: Request context for union references, or None.
        source_columns: Normalized (fact, column) pairs added to union rows.

    Returns:
        SQL with table references substituted.

    Raises:
        ValueError: If a referenced table is not defined in config, or a union
            reference is used outside FROM/JOIN.
    """
    # Find all [[table_name]] references
    table_pattern = r'\[\[([^\]]+)\]\]'

    def replace_table_reference(match):
        table_name = match.group(1)

        # Check context: if preceded by FROM or JOIN, use full reference
        # Otherwise, just use the table name (alias)
        start_pos = match.start()
        context_before = sql[max(0, start_pos-20):start_pos].upper()
        in_from_clause = 'FROM' in context_before or 'JOIN' in context_before

        union = _resolve_union(table_name, database_config, shared_config, context)
        if union is not None:
            members, exclude_self = union
            if not in_from_clause:
                raise ValueError(f"[[{table_name}]] can only be used after FROM/JOIN")
            union_sql, used_members = _render_union(
                members, exclude_self, database_config, context, source_columns or []
            )
            if collected is not None:
                for member in used_members:
                    collected.setdefault(member.sql_name, member)
            return union_sql

        reference = resolve_table_reference(table_name, database_config, shared_config)

        if reference is None:
            raise ValueError(f"Table '{table_name}' not found in database configuration")
        if collected is not None:
            collected.setdefault(reference.sql_name, reference)

        if in_from_clause:
            # Full reference for FROM/JOIN clauses
            if reference.qualified:
                return reference.sql_name
            return f"'{reference.uri}' AS {reference.name}"
        else:
            # Just the table name (alias) for other contexts like SELECT
            return reference.name

    result_sql = re.sub(table_pattern, replace_table_reference, sql)
    return result_sql


def _resolve_union(
        reference: str,
        database_config: Dict[str, Any],
        shared_config: Optional[Dict[str, Any]],
        context: Optional[SqlContext]) -> Optional[Tuple[List[TableReference], bool]]:
    """Resolve a ``[[selector.table]]`` union reference, if it is one.

    Args:
        reference: Text inside the brackets (e.g. "*!.detections").
        database_config: The version database configuration.
        shared_config: The shared ``database`` mapping, or None.
        context: Request context (for schema groups), or None.

    Returns:
        Tuple of (all member TableReferences, exclude_self flag), or None if
        the reference is not a union (plain table or single schema).

    Raises:
        ValueError: If ``!`` is used on something other than ``*`` or a group.
    """
    if SCHEMA_SEPARATOR not in reference:
        return None
    selector, table_name = reference.split(SCHEMA_SEPARATOR, 1)
    exclude_self = selector.endswith(EXCLUDE_SELF_SUFFIX)
    base = selector[:-len(EXCLUDE_SELF_SUFFIX)] if exclude_self else selector

    groups = context.schema_groups if context is not None else {}
    members = resolve_schema_union(base, table_name, shared_config, groups)
    if members is None:
        if exclude_self:
            raise ValueError(
                f"'{EXCLUDE_SELF_SUFFIX}' only applies to '{ALL_SCHEMAS}' or a schema group: "
                f"[[{reference}]]"
            )
        return None
    return (members, exclude_self)


def _render_union(
        members: List[TableReference],
        exclude_self: bool,
        database_config: Dict[str, Any],
        context: Optional[SqlContext],
        source_columns: List[Tuple[str, str]]) -> Tuple[str, List[TableReference]]:
    """Render union members as a parenthesized ``UNION ALL BY NAME`` subquery.

    Each member reads its schema view and adds the requested source columns.
    If ``exclude_self`` removes every member, the excluded member is kept with
    ``LIMIT 0`` so the result has the right columns but no rows. A single
    member is paired with an empty copy of itself so DuckDB's duplicate-name
    check still catches a source column clashing with a real column.

    Args:
        members: Member table references (all qualified schema views).
        exclude_self: Drop the current version's schema from the members.
        database_config: The version database configuration.
        context: Request context (schema sources), or None.
        source_columns: (fact, column) pairs to add to each row.

    Returns:
        Tuple of (SQL subquery text without an alias, the members it reads).
    """
    self_schema = database_config.get(DATABASE_SCHEMA_KEY)
    selected = [m for m in members if not (exclude_self and m.schema == self_schema)]

    selects = [f"({_union_member_select(m, context, source_columns)})" for m in selected]
    if not selects:
        selected = [members[0]]
        selects = [f"({_union_member_select(members[0], context, source_columns)} LIMIT 0)"]
    elif len(selects) == 1 and source_columns:
        selects.append(f"({_union_member_select(selected[0], context, source_columns)} LIMIT 0)")
    return ("(" + " UNION ALL BY NAME ".join(selects) + ")", selected)


def _union_member_select(
        member: TableReference,
        context: Optional[SqlContext],
        source_columns: List[Tuple[str, str]]) -> str:
    """Build one union member's SELECT with its source columns.

    Args:
        member: The member's (qualified) table reference.
        context: Request context (schema sources), or None.
        source_columns: (fact, column) pairs to add.

    Returns:
        ``SELECT *[, <value> AS <column> ...] FROM schema.table``.
    """
    sources = context.schema_sources if context is not None else {}
    source_name, source_version = sources.get(member.schema, (None, None))
    values = {"schema": member.schema, "name": source_name, "version": source_version}

    columns = "".join(
        f", {_sql_literal_or_null(values[fact])} AS {column}" for fact, column in source_columns
    )
    return f"SELECT *{columns} FROM {member.sql_name}"


def _normalize_source_columns(spec: Any) -> List[Tuple[str, str]]:
    """Normalize a route's ``source_columns`` into (fact, column) pairs.

    Args:
        spec: None, a list of fact names (default column names), or a mapping
            of fact name -> column name.

    Returns:
        (fact, column) pairs in config order (empty if spec is None).

    Raises:
        ValueError: If a fact is unknown or a column name isn't an identifier.
    """
    if spec is None:
        return []
    if isinstance(spec, list):
        pairs = [(str(fact), SOURCE_COLUMN_DEFAULTS.get(str(fact), "")) for fact in spec]
    elif isinstance(spec, dict):
        pairs = [(str(fact), str(column)) for fact, column in spec.items()]
    else:
        raise ValueError(f"{SOURCE_COLUMNS_KEY} must be a list or mapping, got {spec!r}")

    for fact, column in pairs:
        if fact not in SOURCE_COLUMN_DEFAULTS:
            raise ValueError(
                f"{SOURCE_COLUMNS_KEY}: unknown fact '{fact}' "
                f"(expected one of {', '.join(SOURCE_COLUMN_DEFAULTS)})"
            )
        if not IDENTIFIER_PATTERN.match(column):
            raise ValueError(f"{SOURCE_COLUMNS_KEY}: invalid column name '{column}'")
    return pairs


def _sql_literal_or_null(value: Any) -> str:
    """Render a value as an escaped SQL string literal, or NULL for None.

    Args:
        value: The value to render.

    Returns:
        ``'value'`` (quotes doubled) or ``CAST(NULL AS VARCHAR)``.
    """
    if value is None:
        return "CAST(NULL AS VARCHAR)"
    return _escape_sql_value(str(value))


def _escape_sql_value(value: str) -> str:
    """Render a config-supplied value as a quoted SQL string literal.

    Only for values that come from the configuration (table URIs, schema and
    source names); request values are always bound with ``?`` markers instead.

    Args:
        value: The value to escape.

    Returns:
        SQL-safe escaped value.
    """
    # Escape single quotes by doubling them
    escaped = value.replace("'", "''")

    # Wrap in single quotes for SQL string literal
    return f"'{escaped}'"


def _substitute_variables_in_string(template: str, params: Dict[str, str]) -> str:
    """Substitute {{variable}} placeholders in a non-SQL string as plain text.

    Used for ``response`` bodies, which are JSON, not SQL. Placeholders with no
    value are left unchanged.

    Args:
        template: String template with {{variable}} placeholders.
        params: Dictionary of parameter values.

    Returns:
        String with variables substituted.
    """
    def replace_variable(match: re.Match[str]) -> str:
        """Return one placeholder's value as text (or the placeholder if unknown)."""
        name = match.group(1)
        return str(params[name]) if name in params else match.group(0)

    return VARIABLE_PATTERN.sub(replace_variable, template)


def _is_named_query_reference(sql_template: str, database_config: Dict[str, Any]) -> bool:
    """Check whether a template is exactly a ``[[query_name]]`` reference.

    Args:
        sql_template: The template, stripped of surrounding whitespace.
        database_config: Database configuration with queries definitions.

    Returns:
        True if the template names a query defined in ``queries``.
    """
    return (sql_template.startswith("[[") and sql_template.endswith("]]")
            and get_named_query(sql_template[2:-2], database_config) is not None)


def _resolve_named_query(sql_template: str, database_config: Dict[str, Any]) -> str:
    """Replace a route sql that is a single [[query]] reference with that query's text.

    Args:
        sql_template: The route's base SQL.
        database_config: Database configuration dictionary with queries definitions.

    Returns:
        The named query's SQL, or sql_template unchanged if it is not a reference.

    Raises:
        ValueError: If the referenced query is not defined in config.
    """
    if not (sql_template.startswith("[[") and sql_template.endswith("]]")):
        return sql_template

    query_name = sql_template[2:-2]
    resolved_query = get_named_query(query_name, database_config)
    if resolved_query is None:
        raise ValueError(f"Named query '{query_name}' not found in database configuration")
    return resolved_query


def _substitution_params(
        route_config: Dict[str, Any],
        path_params: Dict[str, str],
        query_params: Dict[str, str],
        cookies: Dict[str, str]) -> Dict[str, str]:
    """Collect every value a template can reference, keyed by variable name.

    Args:
        route_config: Route configuration dictionary with query_params section.
        path_params: Dictionary of path parameters.
        query_params: Dictionary of query parameters from URL.
        cookies: Dictionary of cookie values, keyed as ``cookies.<name>``.

    Returns:
        Path and query values, config defaults for params not given, and cookies.
    """
    params = _apply_default_values(route_config, {**path_params, **query_params})
    params.update({f"cookies.{key}": value for key, value in cookies.items()})
    return params


def _add_where_fragments(
        sql: str,
        values: List[Optional[str]],
        fragments: List[Tuple[str, List[Optional[str]]]],
        expand_tables: Callable[[str], str]) -> Tuple[str, List[Optional[str]]]:
    """Join bound WHERE fragments onto the base SQL with AND.

    Args:
        sql: Bound base SQL.
        values: Values for the markers in sql.
        fragments: ``(fragment, values)`` pairs from build_where_clause_from_params.
        expand_tables: Expands [[table]] references (see build_sql_query_with_tables).

    Returns:
        Tuple of the combined SQL and its values in marker order.

    Raises:
        ValueError: If a fragment references an undefined table.
    """
    if not fragments:
        return sql, values

    joiner = ' AND ' if 'WHERE' in sql.upper() else ' WHERE '
    clauses = [expand_tables(fragment) for fragment, _ in fragments]
    combined_values = list(values)
    for _, fragment_values in fragments:
        combined_values.extend(fragment_values)
    return sql + joiner + ' AND '.join(clauses), combined_values


def _post_where_clauses(
        route_config: Dict[str, Any],
        branch_appends: List[str],
        query_params: Dict[str, str],
        path_params: Dict[str, str],
        params: Dict[str, Optional[str]],
        expand_tables: Callable[[str], str]) -> List[str]:
    """Build the clauses that follow WHERE, in the order they are written.

    Branch appends from the sql selector come before route-level sql_append
    fragments, so a branch GROUP BY precedes a shared ORDER BY / LIMIT. Both may
    name columns, which can't be bound, so both use the allowed-character check.

    Args:
        route_config: Route configuration dictionary with query_params section.
        branch_appends: sql_append clauses from the selected sql branch.
        query_params: Dictionary of query parameters from URL.
        path_params: Dictionary of path parameters.
        params: All substitution values (see _substitution_params).
        expand_tables: Expands [[table]] references (see build_sql_query_with_tables).

    Returns:
        SQL clauses to append after the WHERE clause.

    Raises:
        ValueError: If a value fails the allowed-character check or a table is undefined.
    """
    branch = [
        _substitute_variables_raw(expand_tables(clause), params)
        for clause in branch_appends
    ]
    route = [
        expand_tables(clause)
        for clause in build_append_clause_from_params(route_config, query_params, path_params)
    ]
    return branch + route


def _bind_variables(
        template: str,
        params: Dict[str, Optional[str]],
        list_params: Optional[Dict[str, List[str]]] = None) -> Tuple[str, List[Optional[str]]]:
    """Replace each {{variable}} in an SQL template with a marker and collect its value.

    The template is read in one pass, so a value is never scanned for further
    {{variables}}. A string literal that is exactly one placeholder
    (``'{{name}}'``) is read as ``{{name}}``; any other quoted placeholder
    (e.g. ``'%{{name}}%'``) is left as text and fails when the query runs.

    Args:
        template: SQL template with {{variable}} placeholders.
        params: Dictionary of parameter values.
        list_params: Parameters whose placeholder becomes a parenthesized list
            with one marker per value, for use with ``IN``.

    Returns:
        Tuple of the SQL text with markers and the values in marker order.

    Raises:
        ValueError: If a placeholder has no value in params or list_params.
    """
    if list_params is None:
        list_params = {}
    values: List[Optional[str]] = []
    template = QUOTED_VARIABLE_PATTERN.sub(r'{{\1}}', template)

    def replace_variable(match: re.Match[str]) -> str:
        """Return the marker(s) for one placeholder and record its value(s)."""
        name = match.group(1)
        if name in list_params:
            values.extend(str(value) for value in list_params[name])
            return "(" + ", ".join(SQL_MARKER for _ in list_params[name]) + ")"
        if name not in params:
            raise ValueError(f"No value for SQL variable '{name}'")
        value = params[name]
        values.append(None if value is None else str(value))
        return SQL_MARKER

    sql = VARIABLE_PATTERN.sub(replace_variable, template)
    return sql, values


def _append_bound_fragment(
        fragments: List[Tuple[str, List[Optional[str]]]],
        template: str,
        params: Dict[str, Optional[str]],
        list_params: Optional[Dict[str, List[str]]] = None) -> None:
    """Bind a WHERE fragment template and append it unless it is empty.

    Args:
        fragments: List of ``(fragment, values)`` pairs to append to.
        template: SQL fragment template with {{variable}} placeholders.
        params: Dictionary of parameter values.
        list_params: Parameters to expand to a marker list (see _bind_variables).
    """
    sql, values = _bind_variables(template, params, list_params)
    sql = sql.strip()
    if sql:
        fragments.append((sql, values))


def _self_params(
        database_config: Dict[str, Any],
        context: Optional[SqlContext]) -> Dict[str, Optional[str]]:
    """The ``{{self.schema}}``, ``{{self.name}}`` and ``{{self.version}}`` values.

    Args:
        database_config: The version database configuration (its ``schema``).
        context: Request context (database name and version), or None.

    Returns:
        Mapping of ``self.<fact>`` to its value (None, bound as NULL, if unknown).
    """
    return {
        f"{SELF_PARAM_PREFIX}schema": database_config.get(DATABASE_SCHEMA_KEY),
        f"{SELF_PARAM_PREFIX}name": context.name if context is not None else None,
        f"{SELF_PARAM_PREFIX}version": context.version if context is not None else None,
    }


def _substitute_variables_raw(template: str, params: Dict[str, str]) -> str:
    """Substitute {{variable}} placeholders WITHOUT SQL quote-escaping.

    Used for sql_append fragments where values are column names, sort directions
    (ASC/DESC), or integer limits — not string literals that need quoting.

    Values are sanitized to prevent SQL injection: only alphanumeric characters,
    underscores, dots, spaces, and common SQL tokens are allowed.

    Args:
        template: String template with {{variable}} placeholders.
        params: Dictionary of parameter values.

    Returns:
        String with variables substituted (unquoted).
    """
    result = template
    for param_name, param_value in params.items():
        placeholder = f"{{{{{param_name}}}}}"
        if placeholder in result:
            sanitized = _sanitize_sql_identifier(str(param_value))
            result = result.replace(placeholder, sanitized)
    return result


def _sanitize_sql_identifier(value: str) -> str:
    """Sanitize a value for use as a SQL identifier, keyword, or integer.

    Allows: alphanumeric, underscores, dots, spaces, commas, and
    common SQL keywords (ASC, DESC). Rejects anything else to prevent injection.

    Args:
        value: The raw value to sanitize.

    Returns:
        Sanitized value safe for use in ORDER BY, LIMIT, OFFSET, etc.

    Raises:
        ValueError: If value contains disallowed characters.
    """
    import re as _re
    # Allow alphanumeric, underscores, dots, parens, commas, spaces, single hyphens
    # Reject double dashes (SQL comment), semicolons, quotes, etc.
    if '--' in value or not _re.match(r'^[a-zA-Z0-9_.(), \-]+$', value):
        raise ValueError(f"Invalid sql_append value: {value!r}")
    return value


def _apply_default_values(route_config: Dict[str, Any], params: Dict[str, str]) -> Dict[str, str]:
    """Apply default values from query_params config for params not already present.

    This makes default values from value-only params (and sql/sql_append params)
    available for cross-parameter variable substitution.

    Args:
        route_config: Route configuration dictionary with query_params section.
        params: Current combined parameters (path + query).

    Returns:
        New params dict with defaults applied for missing keys.
    """
    query_param_configs = route_config.get('query_params', [])
    result = dict(params)

    for param_item in query_param_configs:
        if not isinstance(param_item, dict) or len(param_item) != 1:
            continue

        param_name, param_config = next(iter(param_item.items()))

        if param_name not in result and 'default' in param_config:
            result[param_name] = str(param_config['default'])

    return result


def _substitute_variables_in_dict(template_dict: Dict[str, Any], params: Dict[str, str]) -> Dict[str, Any]:
    """Substitute {{variable}} placeholders in dictionary values.

    Args:
        template_dict: Dictionary with potential {{variable}} placeholders in values.
        params: Dictionary of parameter values.

    Returns:
        Dictionary with variables substituted.
    """
    result = {}
    for key, value in template_dict.items():
        if isinstance(value, str):
            result[key] = _substitute_variables_in_string(value, params)
        elif isinstance(value, dict):
            result[key] = _substitute_variables_in_dict(value, params)
        elif isinstance(value, list):
            result[key] = [_substitute_variables_in_string(str(item), params) if isinstance(item, str) else item for item in value]
        else:
            result[key] = value
    return result


def _resolve_sql_node(node: Any, params: Dict[str, str]) -> Optional[Tuple[str, List[str]]]:
    """Resolve an sql node to a base SQL string and post-WHERE append fragments.

    An sql node is a string (base SQL, no appends), a leaf object
    (``{sql, sql_append}``), or a rule list (nested selection).

    Args:
        node: The sql node to resolve.
        params: Merged selection params (see resolve_route_sql).

    Returns:
        Tuple of (base_sql, branch_appends), or None if a rule-list node matched
        nothing and defined no default.

    Raises:
        SqlSelectionError: If a nested no_match rule is reached.
    """
    if isinstance(node, str):
        return (node, [])
    if isinstance(node, dict):
        if 'sql' in node:
            return (str(node.get('sql', '')), _as_append_list(node.get('sql_append')))
        return None
    if isinstance(node, list):
        return _resolve_rule_list(node, params)
    return None


def _resolve_rule_list(rules: List[Any], params: Dict[str, str]) -> Optional[Tuple[str, List[str]]]:
    """Resolve a rule list, returning the first matching branch's node.

    Args:
        rules: List of selector rules (see resolve_route_sql).
        params: Merged selection params.

    Returns:
        Tuple of (base_sql, branch_appends), or None if nothing matched.

    Raises:
        SqlSelectionError: If a no_match rule is reached before any match.
    """
    for item in rules:
        # Terminal bare-string default.
        if isinstance(item, str):
            return (item, [])
        if not isinstance(item, dict):
            continue
        if 'else' in item:
            return _require_node(item['else'], params)
        if 'no_match' in item:
            raise SqlSelectionError(_no_match_response(item['no_match']))
        if 'when' in item:
            matched = _match_rule(item, params)
            if matched is _NO_MATCH:
                continue
            return _require_node(matched, params)
    return None


def _require_node(node: Any, params: Dict[str, str]) -> Tuple[str, List[str]]:
    """Resolve an sql node that must yield a result (committed branch).

    Args:
        node: The sql node to resolve.
        params: Merged selection params.

    Returns:
        Tuple of (base_sql, branch_appends).

    Raises:
        SqlSelectionError: If the node resolves to no match (e.g. a nested rule
            list with no default).
    """
    resolved = _resolve_sql_node(node, params)
    if resolved is None:
        raise SqlSelectionError(dict(DEFAULT_NO_MATCH_RESPONSE))
    return resolved


def _match_rule(rule: Dict[str, Any], params: Dict[str, str]) -> Any:
    """Evaluate a ``when`` rule, returning its payload node or _NO_MATCH.

    Args:
        rule: A rule dict containing ``when`` plus optional equals/match/then.
        params: Merged selection params.

    Returns:
        The selected sql node to resolve, or _NO_MATCH if the rule did not fire.
    """
    when = rule.get('when')
    if isinstance(when, list):
        return _match_multi(rule, [str(w) for w in when], params)
    return _match_single(rule, str(when), params)


def _match_single(rule: Dict[str, Any], param: str, params: Dict[str, str]) -> Any:
    """Evaluate a single-param ``when`` rule.

    Args:
        rule: The rule dict.
        param: The single param name from ``when``.
        params: Merged selection params.

    Returns:
        The selected sql node, or _NO_MATCH.
    """
    present = param in params
    value = params.get(param)
    if 'match' in rule:
        node = _resolve_value_map(rule['match'], value, present)
        return node if node is not None else _NO_MATCH
    spec = rule.get('equals', '_truthy')
    if _value_matches(spec, value, present):
        return rule.get('then', '')
    return _NO_MATCH


def _match_multi(rule: Dict[str, Any], param_names: List[str], params: Dict[str, str]) -> Any:
    """Evaluate a multi-param (list ``when``) rule.

    ``match`` is a positional case list; otherwise the rule fires when every
    named param satisfies ``equals`` (positional) or, by default, is truthy.

    Args:
        rule: The rule dict.
        param_names: The param names from ``when``.
        params: Merged selection params.

    Returns:
        The selected sql node, or _NO_MATCH.
    """
    if 'match' in rule:
        cases = rule['match']
        if not isinstance(cases, list):
            return _NO_MATCH
        for case in cases:
            if not isinstance(case, dict):
                continue
            if 'default' in case:
                return case['default']
            values = case.get('values')
            if not isinstance(values, list) or len(values) != len(param_names):
                continue
            if _all_positions_match(param_names, values, params):
                return case.get('then', '')
        return _NO_MATCH

    specs = rule.get('equals')
    if isinstance(specs, list) and len(specs) == len(param_names):
        if _all_positions_match(param_names, specs, params):
            return rule.get('then', '')
        return _NO_MATCH

    # Default: fire only when every named param is truthy (AND).
    for name in param_names:
        if not _value_matches('_truthy', params.get(name), name in params):
            return _NO_MATCH
    return rule.get('then', '')


def _all_positions_match(param_names: List[str], specs: List[Any], params: Dict[str, str]) -> bool:
    """Check that each param satisfies its positional value spec.

    Args:
        param_names: Param names, aligned with ``specs``.
        specs: Value specs, one per param.
        params: Merged selection params.

    Returns:
        True if every position matches, False otherwise.
    """
    for name, spec in zip(param_names, specs):
        if not _value_matches(spec, params.get(name), name in params):
            return False
    return True


def _resolve_value_map(value_map: Any, value: Optional[str], present: bool) -> Any:
    """Select an sql node from a single-param value map by precedence.

    Precedence (order-independent): exact literal > _falsy/_truthy >
    _present/_absent > _default. An absent param matches only _absent.

    Args:
        value_map: Mapping of value spec -> sql node.
        value: The param's value (or None if absent).
        present: Whether the param is present.

    Returns:
        The matching sql node, or None if nothing matched.
    """
    if not isinstance(value_map, dict):
        return None

    if not present:
        return value_map.get('_absent')

    value_norm = str(value).strip().lower()
    # 1. Exact literal (case-insensitive) among non-special keys.
    for key, node in value_map.items():
        if str(key).startswith('_'):
            continue
        if str(key).strip().lower() == value_norm:
            return node
    # 2. Truthiness.
    if _is_truthy(value):
        if '_truthy' in value_map:
            return value_map['_truthy']
    elif '_falsy' in value_map:
        return value_map['_falsy']
    # 3. Presence.
    if '_present' in value_map:
        return value_map['_present']
    # 4. Catch-all.
    return value_map.get('_default')


def _value_matches(spec: Any, value: Optional[str], present: bool) -> bool:
    """Check whether a param value satisfies a single value spec.

    Args:
        spec: A literal string or special token (_any, _present, _absent,
            _truthy, _falsy, _default).
        value: The param's value (or None if absent).
        present: Whether the param is present.

    Returns:
        True if the value satisfies the spec, False otherwise.
    """
    spec_str = str(spec)
    if spec_str in ('_any', '_default'):
        return True
    if spec_str == '_present':
        return present
    if spec_str == '_absent':
        return not present
    if spec_str == '_truthy':
        return present and _is_truthy(value)
    if spec_str == '_falsy':
        return present and not _is_truthy(value)
    return present and str(value).strip().lower() == spec_str.strip().lower()


def _is_truthy(value: Optional[str]) -> bool:
    """Return whether a param value is "truthy" per FALSY_VALUES.

    Args:
        value: The value to test.

    Returns:
        True unless the normalized value is in FALSY_VALUES.
    """
    return str(value).strip().lower() not in FALSY_VALUES


def _as_append_list(value: Any) -> List[str]:
    """Normalize a leaf node's sql_append into a list of clause strings.

    Args:
        value: A string, list of strings, or None.

    Returns:
        List of append clause strings (empty if value is None/unsupported).
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _no_match_response(spec: Any) -> Dict[str, Any]:
    """Build a no_match response body, defaulting http_status to 400.

    Args:
        spec: A response dict or a plain error string/value.

    Returns:
        Response body dict with an http_status key.
    """
    if isinstance(spec, dict):
        response = dict(spec)
        response.setdefault('http_status', 400)
        return response
    return {'error': str(spec), 'http_status': 400}
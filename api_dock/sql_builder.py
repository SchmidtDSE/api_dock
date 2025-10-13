"""

SQL Query Builder Module for API Dock

Builds SQL queries with table and parameter substitution for database routes.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import re
from typing import Any, Dict, Optional

from api_dock.database_config import get_named_query, get_table_definition


#
# PUBLIC
#
def build_sql_query(
        sql_template: str,
        database_config: Dict[str, Any],
        path_params: Optional[Dict[str, str]] = None) -> str:
    """Build SQL query from template with table and parameter substitution.

    Args:
        sql_template: SQL template with [[table_name]] and {{param_name}} placeholders.
        database_config: Database configuration dictionary with tables definitions.
        path_params: Dictionary of path parameters extracted from the route.

    Returns:
        Complete SQL query with all substitutions applied.

    Raises:
        ValueError: If referenced table or query is not defined in config.
    """
    if path_params is None:
        path_params = {}

    # Check if sql_template is a reference to a named query
    if sql_template.startswith("[[") and sql_template.endswith("]]"):
        query_name = sql_template[2:-2]
        resolved_query = get_named_query(query_name, database_config)

        if resolved_query is None:
            raise ValueError(f"Named query '{query_name}' not found in database configuration")

        sql_template = resolved_query

    # Substitute table references [[table_name]] with FROM clauses
    sql_with_tables = _substitute_table_references(sql_template, database_config)

    # Substitute path parameters {{param_name}} with values
    sql_with_params = _substitute_parameters(sql_with_tables, path_params)

    return sql_with_params


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
def _substitute_table_references(sql: str, database_config: Dict[str, Any]) -> str:
    """Substitute [[table_name]] references with table file paths in FROM clauses.

    Args:
        sql: SQL query template with [[table_name]] placeholders.
        database_config: Database configuration dictionary.

    Returns:
        SQL with table references substituted.

    Raises:
        ValueError: If a referenced table is not defined in config.
    """
    # Find all [[table_name]] references
    table_pattern = r'\[\[([^\]]+)\]\]'

    def replace_table_reference(match):
        table_name = match.group(1)
        table_path = get_table_definition(table_name, database_config)

        if table_path is None:
            raise ValueError(f"Table '{table_name}' not found in database configuration")

        # Check context: if preceded by FROM or JOIN, use full reference
        # Otherwise, just use the table name (alias)
        start_pos = match.start()
        context_before = sql[max(0, start_pos-20):start_pos].upper()

        if 'FROM' in context_before or 'JOIN' in context_before:
            # Full reference for FROM/JOIN clauses
            return f"'{table_path}' AS {table_name}"
        else:
            # Just the table name (alias) for other contexts like SELECT
            return table_name

    result_sql = re.sub(table_pattern, replace_table_reference, sql)
    return result_sql


def _substitute_parameters(sql: str, params: Dict[str, str]) -> str:
    """Substitute {{param_name}} placeholders with parameter values.

    Args:
        sql: SQL query with {{param_name}} placeholders.
        params: Dictionary of parameter values.

    Returns:
        SQL with parameters substituted.
    """
    result_sql = sql
    for param_name, param_value in params.items():
        # For SQL safety, wrap string values in single quotes
        # Note: In production, use parameterized queries for security
        safe_value = _escape_sql_value(param_value)
        result_sql = result_sql.replace(f"{{{{{param_name}}}}}", safe_value)

    return result_sql


def _escape_sql_value(value: str) -> str:
    """Escape a value for use in SQL query.

    Args:
        value: The value to escape.

    Returns:
        SQL-safe escaped value.
    """
    # Escape single quotes by doubling them
    escaped = value.replace("'", "''")

    # Wrap in single quotes for SQL string literal
    return f"'{escaped}'"
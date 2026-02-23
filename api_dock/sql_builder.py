"""

SQL Query Builder Module for API Dock

Builds SQL queries with table and parameter substitution for database routes.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import re
from typing import Any, Dict, List, Optional, Tuple

from api_dock.database_config import get_named_query, get_table_definition


#
# PUBLIC
#
def build_sql_query(
        route_config: Dict[str, Any],
        database_config: Dict[str, Any],
        path_params: Optional[Dict[str, str]] = None,
        query_params: Optional[Dict[str, str]] = None) -> str:
    """Build SQL query with fragment-based WHERE clause support.

    Args:
        route_config: Route configuration dictionary with sql and query_params.
        database_config: Database configuration dictionary with tables definitions.
        path_params: Dictionary of path parameters extracted from the route.
        query_params: Dictionary of query parameters from URL.

    Returns:
        Complete SQL query with all substitutions applied.

    Raises:
        ValueError: If referenced table or query is not defined in config.
    """
    if path_params is None:
        path_params = {}
    if query_params is None:
        query_params = {}

    # Get the base SQL template from route config
    sql_template = route_config.get('sql', '')

    # Check if sql_template is a reference to a named query
    if sql_template.startswith("[[") and sql_template.endswith("]]"):
        query_name = sql_template[2:-2]
        resolved_query = get_named_query(query_name, database_config)

        if resolved_query is None:
            raise ValueError(f"Named query '{query_name}' not found in database configuration")

        sql_template = resolved_query

    # Substitute table references [[table_name]] with FROM clauses
    sql_with_tables = _substitute_table_references(sql_template, database_config)

    # Build WHERE clause fragments from query parameters
    where_fragments = build_where_clause_from_params(route_config, query_params, path_params)

    # Combine base SQL with WHERE fragments
    if where_fragments:
        # Check if SQL already has a WHERE clause
        if 'WHERE' in sql_with_tables.upper():
            # Add fragments with AND
            where_clause = ' AND ' + ' AND '.join(where_fragments)
        else:
            # Add WHERE clause
            where_clause = ' WHERE ' + ' AND '.join(where_fragments)
        sql_with_tables += where_clause

    # Substitute remaining path parameters {{param_name}} with values
    all_params = {**path_params, **query_params}
    sql_with_params = _substitute_variables_in_string(sql_with_tables, all_params)

    return sql_with_params


# Keep backward compatibility with old signature
def build_sql_query_legacy(
        sql_template: str,
        database_config: Dict[str, Any],
        path_params: Optional[Dict[str, str]] = None) -> str:
    """Legacy build SQL query function for backward compatibility.

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


def process_query_parameters(
        route_config: Dict[str, Any],
        query_params: Dict[str, str],
        path_params: Dict[str, str]
) -> Tuple[bool, Any, int, Optional[str]]:
    """Process query parameters according to declarative configuration.

    Args:
        route_config: Route configuration dictionary with query_params section.
        query_params: Dictionary of query parameters from URL.
        path_params: Dictionary of path parameters for variable substitution.

    Returns:
        Tuple of (should_return_early, response_data, status_code, error_message)
        If should_return_early=True, return response_data immediately
        If False, continue with SQL building
    """
    query_param_configs = route_config.get('query_params', [])
    if not query_param_configs:
        return (False, None, 200, None)

    # Combine path and query parameters for variable substitution
    all_params = {**path_params, **query_params}

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
        path_params: Dict[str, str]
) -> List[str]:
    """Build WHERE clause fragments from parameter configurations.

    Args:
        route_config: Route configuration dictionary with query_params section.
        query_params: Dictionary of query parameters from URL.
        path_params: Dictionary of path parameters.

    Returns:
        List of SQL WHERE conditions to be joined with AND
    """
    query_param_configs = route_config.get('query_params', [])
    where_fragments = []

    # Combine path and query parameters for variable substitution
    all_params = {**path_params, **query_params}

    for param_item in query_param_configs:
        if not isinstance(param_item, dict) or len(param_item) != 1:
            continue

        param_name, param_config = next(iter(param_item.items()))
        param_value = query_params.get(param_name)

        # Skip parameters that have response or action configurations
        if 'response' in param_config:
            continue

        # Handle conditional parameters that have SQL
        if 'conditional' in param_config and param_value is not None:
            conditional_config = param_config['conditional']
            if param_value in conditional_config and 'sql' in conditional_config[param_value]:
                sql_fragment = conditional_config[param_value]['sql']
                if sql_fragment:  # Skip empty SQL fragments
                    substituted_fragment = _substitute_variables_in_string(sql_fragment, all_params)
                    where_fragments.append(substituted_fragment)
            continue

        # Handle regular SQL parameters
        if 'sql' in param_config:
            sql_fragment = param_config['sql']

            # Handle parameters with default values (always include)
            if 'default' in param_config:
                # Use provided value or default
                effective_value = param_value if param_value is not None else param_config['default']
                effective_params = {**all_params, param_name: effective_value}
                substituted_fragment = _substitute_variables_in_string(sql_fragment, effective_params)
                if substituted_fragment:  # Skip empty fragments
                    where_fragments.append(substituted_fragment)

            # Handle optional parameters (only include if provided)
            elif param_value is not None:
                substituted_fragment = _substitute_variables_in_string(sql_fragment, all_params)
                if substituted_fragment:  # Skip empty fragments
                    where_fragments.append(substituted_fragment)

    return where_fragments


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


def _substitute_variables_in_string(template: str, params: Dict[str, str]) -> str:
    """Substitute {{variable}} placeholders in a string template.

    Args:
        template: String template with {{variable}} placeholders.
        params: Dictionary of parameter values.

    Returns:
        String with variables substituted.
    """
    result = template
    for param_name, param_value in params.items():
        placeholder = f"{{{{{param_name}}}}}"
        if placeholder in result:
            # For SQL fragments, escape the value
            if any(sql_keyword in result.upper() for sql_keyword in ['SELECT', 'WHERE', 'FROM', 'JOIN', 'AND', 'OR']):
                safe_value = _escape_sql_value(str(param_value))
            else:
                safe_value = str(param_value)
            result = result.replace(placeholder, safe_value)
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
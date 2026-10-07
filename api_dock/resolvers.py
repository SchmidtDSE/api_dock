"""

Resolvers Module for API Dock

A resolver calls a route of an internal database and gets exactly one row
back. A route lists its resolvers in ``resolve:`` and uses a value of the row
as ``{{<resolver>.<column>}}``. This module checks resolver configs and route
references, fills resolver params and turns the row's values into text. The
checks that need the target database are in resolver_targets.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import math
import os
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple
from uuid import UUID

from api_dock.route_headers import VARIABLE_PATTERN


#
# CONSTANTS
#
RESOLVERS_KEY: str = "resolvers"
RESOLVE_KEY: str = "resolve"
VIA_KEY: str = "via"
PARAMS_KEY: str = "params"
BIND_KEY: str = "bind"
RESOLVER_KEYS: frozenset = frozenset({VIA_KEY, PARAMS_KEY, BIND_KEY})

RESOLVER_NAME_PATTERN: re.Pattern[str] = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Cookie values are written {{cookies.<name>}}, so no resolver can have this name.
COOKIES_PREFIX: str = "cookies"

# The prefix that marks an injected cookie value read from the environment.
ENV_PREFIX: str = "env:"


#
# PUBLIC
#
class ResolverError(Exception):
    """Raised when a resolver can't supply its values.

    Attributes:
        status_code: Status of the response to the caller: 404, 500 or 503.
        reason: What happened, for the server log.
    """

    def __init__(self, status_code: int, reason: str) -> None:
        """Create the error.

        Args:
            status_code: Status of the response to the caller.
            reason: What happened, for the server log.
        """
        self.status_code = status_code
        self.reason = reason
        super().__init__(reason)


def get_resolvers(database_config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Return a checked config's resolvers by name; empty if it has none."""
    resolvers = database_config.get(RESOLVERS_KEY, {})
    return resolvers if isinstance(resolvers, dict) else {}


def check_resolvers(database_config: Dict[str, Any]) -> None:
    """Check the shape of a config's ``resolvers:``.

    Args:
        database_config: Database configuration.

    Raises:
        ValueError: If a name, key, via, params or bind is malformed, or a
            params template uses a cookie or a resolver value.
    """
    resolvers = database_config.get(RESOLVERS_KEY, {})
    if not isinstance(resolvers, dict):
        raise ValueError("resolvers: must be a mapping of names to resolvers")
    for name, resolver in resolvers.items():
        _check_resolver_name(name)
        try:
            _check_resolver(resolver, set(resolvers))
        except ValueError as error:
            raise ValueError(f"resolvers.{name}: {error}") from error


def check_resolve_list(route_config: Dict[str, Any], resolvers: Mapping[str, Any]) -> None:
    """Check that a route's ``resolve:`` names each defined resolver once.

    Args:
        route_config: Route configuration.
        resolvers: The config's resolvers by name.

    Raises:
        ValueError: If the list is malformed, names an unknown resolver or
            names one twice.
    """
    names = route_config.get(RESOLVE_KEY, [])
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise ValueError("resolve: must be a list of resolver names")
    for index, name in enumerate(names):
        if name not in resolvers:
            raise ValueError(f"resolve: '{name}' is not a resolver of this database")
        if name in names[:index]:
            raise ValueError(f"resolve: '{name}' is listed twice")


def check_params_declared(
        route_config: Dict[str, Any], resolvers: Mapping[str, Dict[str, Any]],
        path_variables: Iterable[str]) -> None:
    """Check that the params of each listed resolver use only the route's values.

    Args:
        route_config: Route configuration, merged with top-level query_params.
        resolvers: The config's resolvers by name.
        path_variables: The route's path variable names.

    Raises:
        ValueError: If a params template uses a name that is not a path
            variable or a declared query param of the route.
    """
    declared = set(path_variables) | {
        next(iter(item)) for item in route_config.get("query_params", [])
    }
    for name in route_config.get(RESOLVE_KEY, []):
        for param, template in resolvers[name].get(PARAMS_KEY, {}).items():
            for variable in VARIABLE_PATTERN.findall(template):
                if variable not in declared:
                    raise ValueError(
                        f"resolver '{name}' params.{param} uses '{{{{{variable}}}}}', which is "
                        "not a path variable or query param of the route"
                    )


def check_value_uses(
        templates: Iterable[str], route_config: Dict[str, Any],
        resolvers: Mapping[str, Dict[str, Any]]) -> None:
    """Check each resolver value in a route's templates against resolve: and bind:.

    Args:
        templates: The route's SQL and header templates.
        route_config: Route configuration with its ``resolve:`` list.
        resolvers: The config's resolvers by name.

    Raises:
        ValueError: If a value's resolver is not in resolve:, or its column
            is not in the resolver's bind: list.
    """
    listed = route_config.get(RESOLVE_KEY, [])
    for template in templates:
        for resolver, column in resolver_values_in(template, resolvers):
            reference = f"'{{{{{resolver}.{column}}}}}'"
            if resolver not in listed:
                raise ValueError(
                    f"uses {reference} but does not list '{resolver}' in resolve:"
                )
            _check_bound_column(reference, resolver, column, resolvers)


def check_table_value(
        table_def: Dict[str, Any], value_name: str,
        resolvers: Mapping[str, Dict[str, Any]]) -> None:
    """Check that a templated table's URI names a resolver and a bound column.

    Args:
        table_def: The templated table.
        value_name: The ``<resolver>.<column>`` name in its URI.
        resolvers: The config's resolvers by name.

    Raises:
        ValueError: If the resolver is not defined or the column is not bound.
    """
    resolver, _, column = value_name.partition(".")
    reference = f"'{{{{{value_name}}}}}'"
    if resolver not in resolvers:
        raise ValueError(f"{reference} does not name a resolver of this database")
    _check_bound_column(reference, resolver, column, resolvers)


def resolver_values_in(
        template: str, resolvers: Mapping[str, Any]) -> List[Tuple[str, str]]:
    """List the ``{{<resolver>.<column>}}`` values that a template uses.

    A dotted name whose first part is not a resolver of the config is an
    ordinary variable, such as a query param named ``a.b``.

    Args:
        template: A template.
        resolvers: The config's resolvers by name.

    Returns:
        ``(resolver, column)`` pairs, in template order.
    """
    values = []
    for variable in VARIABLE_PATTERN.findall(template):
        resolver, dot, column = variable.partition(".")
        if dot and resolver in resolvers:
            values.append((resolver, column))
    return values


def fill_params(templates: Mapping[str, str], values: Mapping[str, str]) -> Dict[str, str]:
    """Fill resolver params as text; leave out a param whose variable has no value.

    Args:
        templates: Param name to template.
        values: Path and query values, with defaults applied.

    Returns:
        Param name to filled text.
    """
    params = {}
    for name, template in templates.items():
        if all(variable in values for variable in VARIABLE_PATTERN.findall(template)):
            params[name] = VARIABLE_PATTERN.sub(lambda match: values[match.group(1)], template)
    return params


def select_row(
        columns: Sequence[str], rows: Sequence[Sequence[Any]],
        bind: Sequence[str]) -> Dict[str, Optional[str]]:
    """Return the bound columns of the single row as text.

    Args:
        columns: Column names of the target's result.
        rows: Rows of the target's result.
        bind: The resolver's bind: list.

    Returns:
        Column name to text, or None for NULL.

    Raises:
        ResolverError: 404 if there is no row; 500 if there is more than one
            row, a bound column is missing, or a value can't be converted.
    """
    if not rows:
        raise ResolverError(404, "the target returned no rows")
    if len(rows) > 1:
        raise ResolverError(500, f"the target returned {len(rows)} rows, not one")
    row = dict(zip(columns, rows[0]))
    values = {}
    for column in bind:
        if column not in row:
            raise ResolverError(500, f"the target's row has no column '{column}'")
        try:
            values[column] = resolver_text(row[column])
        except ResolverError as error:
            raise ResolverError(500, f"column '{column}': {error.reason}") from error
    return values


def resolver_text(value: Any) -> Optional[str]:
    """Convert a value of the target's row to text.

    Booleans become ``true`` or ``false``; finite numbers are written by
    ``str()``; dates and timestamps become ISO 8601; UUIDs become their text
    form. NULL stays None.

    Args:
        value: A database value.

    Returns:
        The value as text, or None.

    Raises:
        ResolverError: For a non-finite number or an unsupported type, such as
            a list, an object or bytes.
    """
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (float, Decimal)):
        finite = value.is_finite() if isinstance(value, Decimal) else math.isfinite(value)
        if not finite:
            raise ResolverError(500, f"the value {value} is not a finite number")
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    raise ResolverError(500, f"a {type(value).__name__} value can't be used")


def unset_injected_cookies(database_config: Dict[str, Any]) -> Set[str]:
    """List the injected cookies whose environment variable is not set.

    Args:
        database_config: Database configuration, merged with the main config.

    Returns:
        Names of those cookies.
    """
    cookies = database_config.get(COOKIES_PREFIX)
    if not isinstance(cookies, list):
        return set()
    unset = set()
    for entry in cookies:
        if not isinstance(entry, dict) or not isinstance(entry.get("key"), str):
            continue
        value = entry.get("value")
        if value is None:
            variable: Optional[str] = entry["key"]
        elif isinstance(value, str) and value.startswith(ENV_PREFIX):
            variable = value[len(ENV_PREFIX):]
        else:
            variable = None
        if variable is not None and variable not in os.environ:
            unset.add(entry["key"])
    return unset


#
# INTERNAL
#
def _check_resolver_name(name: Any) -> None:
    """Refuse a resolver name that can't be written as ``{{<name>.<column>}}``."""
    if name == COOKIES_PREFIX:
        raise ValueError(f"resolvers: a resolver can't be named '{COOKIES_PREFIX}'")
    if not isinstance(name, str) or not RESOLVER_NAME_PATTERN.fullmatch(name):
        raise ValueError(f"resolvers: '{name}' is not a valid resolver name")


def _check_resolver(resolver: Any, resolver_names: Set[str]) -> None:
    """Check one resolver's keys, via, params and bind."""
    if not isinstance(resolver, dict):
        raise ValueError("must be a mapping")
    for key in resolver:
        if key not in RESOLVER_KEYS:
            raise ValueError(f"unknown key '{key}'")
    via = resolver.get(VIA_KEY)
    if not isinstance(via, str) or not via:
        raise ValueError("via: is required and must be text")
    if "{{" in via:
        raise ValueError("via: can't contain {{variables}}")
    _check_params(resolver.get(PARAMS_KEY, {}), resolver_names)
    _check_bind(resolver.get(BIND_KEY))


def _check_params(params: Any, resolver_names: Set[str]) -> None:
    """Refuse params that are not text templates, or that use cookies or resolver values."""
    if not isinstance(params, dict):
        raise ValueError("params: must be a mapping of names to text")
    for name, template in params.items():
        if not isinstance(template, str):
            raise ValueError(f"params.{name}: must be text")
        for variable in VARIABLE_PATTERN.findall(template):
            prefix, dot, _ = variable.partition(".")
            if dot and prefix == COOKIES_PREFIX:
                raise ValueError(f"params.{name}: can't use '{{{{{variable}}}}}'")
            if dot and prefix in resolver_names:
                raise ValueError(
                    f"params.{name}: can't use the resolver value '{{{{{variable}}}}}'"
                )


def _check_bind(bind: Any) -> None:
    """Refuse a bind: that is not a non-empty list of distinct column names."""
    if not isinstance(bind, list) or not bind or not all(
            isinstance(column, str) and column for column in bind):
        raise ValueError("bind: must be a non-empty list of column names")
    for index, column in enumerate(bind):
        if column in bind[:index]:
            raise ValueError(f"bind: '{column}' is listed twice")


def _check_bound_column(
        reference: str, resolver: str, column: str,
        resolvers: Mapping[str, Dict[str, Any]]) -> None:
    """Refuse a value whose column is not in its resolver's bind: list."""
    if column not in resolvers[resolver].get(BIND_KEY, []):
        raise ValueError(f"{reference} is not in the bind: list of resolver '{resolver}'")

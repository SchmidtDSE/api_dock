"""

Resolver Targets Module for API Dock

Startup checks that need both a resolver's database and its target. A
resolver's ``via`` must name an internal database, an explicit version (for a
versioned database) and a route of that version. The target can't have
resolvers itself. Each ``{{variable}}`` in the target route's SQL must get its
value from the resolver's params, a target query-param default, a target path
variable in the ``via`` path, or a cookie that the target injects.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import logging
from typing import Any, Collection, Dict, Mapping, Optional, Set, Tuple

from api_dock.database_config import find_database_route, merge_query_params, route_sql_leaves
from api_dock.postgres_pools import DatabaseKey, database_label
from api_dock.resolvers import (
    COOKIES_PREFIX,
    PARAMS_KEY,
    RESOLVE_KEY,
    RESOLVERS_KEY,
    VIA_KEY,
    get_resolvers,
)
from api_dock.route_headers import VARIABLE_PATTERN
from api_dock.sql_builder import extract_path_parameters


#
# CONSTANTS
#
logger: logging.Logger = logging.getLogger(__name__)

# Database name to its configs by version (None for an unversioned database).
DatabaseConfigs = Mapping[str, Mapping[Optional[str], Dict[str, Any]]]


#
# PUBLIC
#
def check_resolver_targets(
        key: DatabaseKey,
        database_config: Dict[str, Any],
        databases: DatabaseConfigs,
        internal_names: Collection[str],
        remote_names: Collection[str]) -> None:
    """Check the target of each resolver of one database version.

    A resolver that no route lists is logged as a warning.

    Args:
        key: The resolver's database and version.
        database_config: Its checked config.
        databases: Every configured database's checked configs.
        internal_names: Names of the internal databases.
        remote_names: Names of the configured remotes.

    Raises:
        ValueError: If a target fails a check. The message names the
            database, the version, the resolver and its via.
    """
    for name, resolver in get_resolvers(database_config).items():
        via = resolver[VIA_KEY]
        try:
            _check_target(resolver, databases, internal_names, remote_names)
        except ValueError as error:
            raise ValueError(
                f"{database_label(key)}, resolver '{name}': via '{via}': {error}"
            ) from error
        if not _is_listed(name, database_config):
            logger.warning(
                "%s: resolver '%s' is not listed in any route's resolve:", database_label(key), name
            )


def split_via(via: str, versioned: bool) -> Tuple[str, Optional[str], str]:
    """Split a via into its database, version and route path.

    Args:
        via: ``<database>/<version>/<route>``, or ``<database>/<route>``.
        versioned: Whether the database has versions.

    Returns:
        Tuple of database name, version (None if unversioned) and route path.
    """
    database, _, rest = via.partition("/")
    if not versioned:
        return database, None, rest
    version, _, route_path = rest.partition("/")
    return database, version, route_path


#
# INTERNAL
#
def _check_target(
        resolver: Dict[str, Any],
        databases: DatabaseConfigs,
        internal_names: Collection[str],
        remote_names: Collection[str]) -> None:
    """Check that via names an internal database, a version and a route, and the route's SQL."""
    target_name = resolver[VIA_KEY].partition("/")[0]
    if target_name not in databases:
        if target_name in remote_names:
            raise ValueError(f"'{target_name}' is a remote, not an internal database")
        raise ValueError(f"database '{target_name}' does not exist")
    if target_name not in internal_names:
        raise ValueError(f"database '{target_name}' is not internal")
    configs = databases[target_name]
    _, version, route_path = split_via(resolver[VIA_KEY], None not in configs)
    if version == "latest":
        raise ValueError("uses latest; name a version")
    if version not in configs:
        raise ValueError(f"version '{version}' of database '{target_name}' does not exist")
    target_config = configs[version]
    if RESOLVERS_KEY in target_config:
        raise ValueError(f"database '{target_name}' has resolvers:")
    route = find_database_route(route_path, target_config)
    if route is None:
        raise ValueError(f"no route of database '{target_name}' matches '{route_path}'")
    _check_target_sql(resolver, route_path, merge_query_params(route, target_config), target_config)


def _check_target_sql(
        resolver: Dict[str, Any], route_path: str,
        route: Dict[str, Any], target_config: Dict[str, Any]) -> None:
    """Refuse a variable in the target route's SQL that the call can't supply.

    Caller cookies and caller query values never reach the target, so they
    are not sources.
    """
    sources = set(resolver.get(PARAMS_KEY, {}))
    sources.update(extract_path_parameters(route_path, route.get("route", "")))
    for item in route.get("query_params", []):
        name, param_config = next(iter(item.items()))
        if "default" in param_config:
            sources.add(name)
    injected = _injected_cookie_names(target_config)
    for leaf in route_sql_leaves(route, target_config):
        for variable in VARIABLE_PATTERN.findall(leaf):
            prefix, dot, cookie = variable.partition(".")
            if dot and prefix == COOKIES_PREFIX:
                if cookie not in injected:
                    raise ValueError(
                        f"'{{{{{variable}}}}}' in the target route's sql is not an injected "
                        "cookie of the target"
                    )
            elif variable not in sources:
                raise ValueError(
                    f"'{{{{{variable}}}}}' in the target route's sql has no value; set it in "
                    "params:, as a target query-param default or as a target path variable"
                )


def _injected_cookie_names(database_config: Dict[str, Any]) -> Set[str]:
    """Return the names of the cookies a config injects, without reading their values."""
    cookies = database_config.get(COOKIES_PREFIX)
    if not isinstance(cookies, list):
        return set()
    return {
        entry["key"] for entry in cookies
        if isinstance(entry, dict) and isinstance(entry.get("key"), str)
    }


def _is_listed(name: str, database_config: Dict[str, Any]) -> bool:
    """Return whether any route lists the resolver in resolve:."""
    return any(
        name in route.get(RESOLVE_KEY, [])
        for route in database_config.get("routes", []) if isinstance(route, dict)
    )

"""

Route Mapper Module for API Dock

Standalone route mapping functionality that can be integrated into any web framework.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import base64
import ipaddress
import json
import logging
import os
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union
from uuid import UUID

import httpx

from api_dock.auth import validate_authentication
from api_dock.config import DEFAULT_CONFIG_DIR, filter_cookies_by_config, filter_remote_query_params, find_remote_config, find_route_mapping, get_authentication_config, get_database_names, get_remote_names, get_remote_versions, get_settings, is_route_allowed, is_versioned_remote, load_main_config, merge_inherited_config, resolve_latest_version
from api_dock.database_backends import (
    DatabaseBackend,
    DatabaseLifecycleError,
    DatabaseUnavailableError,
    DuckDBBackend,
)
from api_dock.database_config import (
    BACKEND_KEY,
    DUCKDB_BACKEND,
    POSTGRES_BACKEND,
    check_database_config,
    find_database_route,
    get_backend_name,
    get_database_versions,
    is_versioned_database,
    load_database_config,
    merge_query_params,
    resolve_latest_database_version,
)
from api_dock.listings import build_listing, resolve_listing_specs
from api_dock.postgres_pools import DatabaseKey, PostgresPools, database_label
from api_dock.sql_builder import build_sql_query, extract_path_parameters, process_query_parameters, SqlSelectionError
from api_dock.types import PreparedRequest, ProxyResponse
#
# CONSTANTS
#
DEFAULT_VERSION: str = "latest"

logger: logging.Logger = logging.getLogger(__name__)

# Returned when a database that had only DuckDB versions at startup now has a
# PostgreSQL config, which needs a pool that only a new mapper can open.
RESTART_REQUIRED_MESSAGE: str = "Database configuration changed; restart required"

# Network address types that are written to JSON as their string form.
IP_ADDRESS_TYPES: Tuple[type, ...] = (
    ipaddress.IPv4Address, ipaddress.IPv6Address,
    ipaddress.IPv4Interface, ipaddress.IPv6Interface,
    ipaddress.IPv4Network, ipaddress.IPv6Network,
)

# Default upstream request timeout in seconds. Override with the `timeout`
# setting; set it to null/false to disable the timeout entirely.
DEFAULT_TIMEOUT: float = 10.0

# Headers excluded from upstream→client forwarding.
# Hop-by-hop headers (RFC 7230 §6.1) must not be forwarded by proxies.
# content-type is stored separately in ProxyResponse.content_type.
# content-encoding and content-length are excluded because httpx automatically
# decompresses response bodies before we see them, making upstream values wrong.
HOP_BY_HOP_HEADERS: frozenset = frozenset({
    "connection",
    "content-encoding",
    "content-length",
    "content-type",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
})


#
# PUBLIC
#
def collect_multi_query_params(items: Iterable[Tuple[str, str]]) -> Dict[str, List[str]]:
    """Group repeated query string pairs into a name-to-value-list mapping.

    Preserves every value for keys that appear more than once in the URL
    (e.g. ?id=1&id=2), which a flat dict would collapse to the last value.

    Args:
        items: Iterable of (key, value) pairs, in URL order. FastAPI provides
            these via ``request.query_params.multi_items()`` and Flask via
            ``request.args.items(multi=True)``.

    Returns:
        Dictionary mapping each query parameter name to the list of values
        received for it, in URL order.
    """
    grouped: Dict[str, List[str]] = {}
    for key, value in items:
        grouped.setdefault(key, []).append(value)
    return grouped


class RouteMapper:
    """Standalone route mapper for proxying requests to remote APIs.

    This class handles the core logic of routing requests to remote APIs
    based on configuration files. It can be integrated into any web framework.
    """

    def __init__(self, config_path: Optional[str] = None) -> None:
        """Initialize RouteMapper with configuration.

        Remote and database config files are read from the directory holding
        the main config file. Every listed database config is checked here, so
        a bad config stops startup instead of failing on a live request. A
        database with any PostgreSQL version is kept as loaded here, with all
        its versions, until a new mapper is created. No connection is opened
        until start().

        Args:
            config_path: Path to main config file. If None, uses default.

        Raises:
            ValueError: If a listed database config is missing or fails a check.
        """
        try:
            self.config = load_main_config(config_path)
        except (FileNotFoundError, Exception):
            self.config = {"name": "api-dock", "description": "API Dock wrapper", "authors": []}

        self.config_dir = os.path.dirname(config_path) if config_path else DEFAULT_CONFIG_DIR
        self.remote_names = get_remote_names(self.config, self.config_dir)
        self.database_names = get_database_names(self.config)
        self.settings = get_settings(self.config)
        self.listing_specs, self.listing_warnings = resolve_listing_specs(
            self.config, self.config_dir
        )

        self._snapshots: Dict[str, Dict[Optional[str], Dict[str, Any]]] = {}
        for database_name in self.database_names:
            configs = _load_checked_database(database_name, self.config, self.config_dir)
            if any(get_backend_name(config) == POSTGRES_BACKEND for config in configs.values()):
                self._snapshots[database_name] = configs
        self._postgres = PostgresPools(self._postgres_configs())

    @property
    def postgres_databases(self) -> List[DatabaseKey]:
        """List the database versions that use PostgreSQL.

        Returns:
            ``(database name, version)`` pairs; version is None for an
            unversioned database.
        """
        return list(self._postgres_configs())

    async def start(self) -> None:
        """Open the connection pools of PostgreSQL databases.

        Call this once, on the event loop that will serve requests, before
        serving PostgreSQL routes; the FastAPI app does this in its lifespan.
        Calling it again on that loop does nothing. Configs without PostgreSQL
        don't need it.

        Raises:
            RuntimeError: If the PostgreSQL dependencies are not installed.
            ValueError: If a database's connection settings are invalid.
            DatabaseLifecycleError: If the mapper is closed.
        """
        await self._postgres.start()

    async def aclose(self) -> None:
        """Close the connection pools. A closed mapper can't be started again."""
        await self._postgres.aclose()

    def get_config_metadata(self) -> Dict[str, Any]:
        """Get API metadata from configuration.

        Returns:
            Dictionary containing name, description, authors, endpoints, and remotes.
            Note: Databases are included in remotes list to hide implementation details.
        """
        all_remotes = self.remote_names + self.database_names

        endpoints = list(self.config.get("endpoints", ["/"]))
        for spec in getattr(self, "listing_specs", []):
            endpoint = f"/{spec.route}"
            if endpoint not in endpoints:
                endpoints.append(endpoint)

        metadata = {
            "name": self.config.get("name", "API Dock"),
            "description": self.config.get("description", "API wrapper using configuration files"),
            "authors": self.config.get("authors", []),
            "endpoints": endpoints,
            "remotes": all_remotes
        }
        return metadata

    def get_listing(self, spec: Any) -> List[Any]:
        """Build the response body for a catalog-listing endpoint.

        Args:
            spec: A ListingSpec produced during initialization.

        Returns:
            A list of ``{"model", "version"}`` dicts or ``"model/version"``
            strings, per the spec's format.
        """
        return build_listing(
            spec, self.config, self.config_dir, database_versions=self._listed_versions
        )

    async def prepare_remote_request(
            self,
            remote_name: str,
            path: str,
            method: str,
            headers: Optional[Dict[str, str]] = None,
            body: Optional[bytes] = None,
            query_params: Optional[Dict[str, str]] = None,
            cookies: Optional[Dict[str, str]] = None,
            multi_query_params: Optional[Dict[str, List[str]]] = None
            ) -> Union[ProxyResponse, PreparedRequest]:
        """Validate and resolve a remote request without executing the HTTP call.

        Performs all route validation, version resolution, config loading,
        URL construction, query-parameter filtering, and cookie filtering.
        Returns either an error ProxyResponse (unknown remote, blocked route,
        missing config) or a PreparedRequest ready for execution.

        Callers that need streaming behaviour (FastAPI) should call this method
        directly and issue the httpx request themselves. map_route() calls this
        internally and adds the buffered HTTP call on top.

        Args:
            remote_name: Name of the remote API.
            path: The path to proxy to the remote API.
            method: HTTP method (GET, POST, etc.).
            headers: Request headers dictionary.
            body: Request body bytes.
            query_params: Query parameters dictionary (single value per key).
            cookies: Cookie values from request.
            multi_query_params: Optional mapping of query parameter names to the
                full list of values received, preserving keys repeated in the URL
                (e.g. ?id=1&id=2). When provided, it is used as the source for
                the forwarded params so repeated keys reach the upstream intact.

        Returns:
            ProxyResponse for api_dock-level errors, or PreparedRequest on success.
        """
        if remote_name not in self.remote_names:
            return _error_response(404, f"Remote '{remote_name}' not found")

        is_versioned = is_versioned_remote(remote_name, self.config, self.config_dir)

        path_parts = path.split("/") if path else []
        version = None
        actual_path = path

        if is_versioned and path_parts:
            potential_version = path_parts[0]
            available_versions = get_remote_versions(remote_name, self.config, self.config_dir)

            if potential_version == "latest":
                version = resolve_latest_version(available_versions)
                if version is None:
                    return _error_response(404, f"No versions found for remote '{remote_name}'")
                actual_path = "/".join(path_parts[1:])
            elif potential_version in available_versions:
                version = potential_version
                actual_path = "/".join(path_parts[1:])
            elif not path:
                return _json_response({"versions": available_versions})
            else:
                return _error_response(404, f"Configuration for remote '{remote_name}' not found")
        elif is_versioned and not path:
            available_versions = get_remote_versions(remote_name, self.config, self.config_dir)
            return _json_response({"versions": available_versions})

        if not actual_path:
            actual_path = ""

        allowed = is_route_allowed(
            actual_path, self.config, remote_name, version, method, self.config_dir
        )
        if not allowed:
            return _error_response(403, f"Route '{actual_path}' not allowed for remote '{remote_name}'")

        try:
            remote_config = find_remote_config(
                remote_name, self.config, self.config_dir, version=version
            )
        except FileNotFoundError:
            return _error_response(404, f"Configuration for remote '{remote_name}' not found")

        remote_url = remote_config.get("url")
        if not remote_url:
            return _error_response(500, f"No URL configured for remote '{remote_name}'")

        # Prefer the multivalue mapping so repeated keys (?id=1&id=2) reach the
        # upstream intact; fall back to the collapsed single-value dict.
        source_query_params = multi_query_params if multi_query_params else (query_params or {})
        filtered_query_params = filter_remote_query_params(
            source_query_params, actual_path, method, remote_config
        )

        filtered_cookies = filter_cookies_by_config(cookies or {}, remote_config)

        full_pattern = f"{remote_name}/{actual_path}"
        mapped_route = find_route_mapping(full_pattern, method, remote_config, remote_name, cookies)
        final_path = mapped_route if mapped_route is not None else actual_path

        if final_path:
            if self.settings.get("add_trailing_slash", True):
                path_with_slash = final_path if final_path.endswith('/') else final_path + '/'
                full_url = f"{remote_url.rstrip('/')}/{path_with_slash}"
            else:
                full_url = f"{remote_url.rstrip('/')}/{final_path}"
        else:
            full_url = remote_url.rstrip('/')

        # When follow_redirects is False, httpx returns 3xx responses directly
        # so the client receives the redirect (e.g. Location: <presigned S3 URL>)
        # and fetches the resource itself — avoiding unnecessary data transfer.
        # When True (default), httpx follows the redirect transparently.
        follow_redirects = self.settings.get("follow_redirects", True)
        timeout = _resolve_timeout(self.settings.get("timeout", DEFAULT_TIMEOUT))

        return PreparedRequest(
            url=full_url,
            method=method,
            headers=headers or {},
            params=filtered_query_params,
            cookies=filtered_cookies,
            body=body,
            follow_redirects=follow_redirects,
            timeout=timeout,
        )

    async def map_route(self,
            remote_name: str,
            path: str,
            method: str,
            headers: Optional[Dict[str, str]] = None,
            body: Optional[bytes] = None,
            query_params: Optional[Dict[str, str]] = None,
            cookies: Optional[Dict[str, str]] = None,
            multi_query_params: Optional[Dict[str, List[str]]] = None) -> ProxyResponse:
        """Map a request to a remote API and return the upstream response.

        Delegates route validation and URL resolution to prepare_remote_request(),
        then issues a buffered httpx request. Upstream responses — including 4xx
        and 5xx — are passed through verbatim with their original status code,
        body, and headers. Only api_dock-level errors (unknown remote, blocked
        route, missing config) produce api_dock-generated error responses.

        When follow_redirects is False in settings, 3xx responses are returned
        to the client (with their Location header) rather than followed. This
        is the correct path for S3 presigned URL redirects.

        Note: This method buffers the entire upstream response body in memory.
        For large responses, callers that want streaming should use
        prepare_remote_request() directly and issue the httpx call themselves.

        Args:
            remote_name: Name of the remote API.
            path: The path to proxy to the remote API.
            method: HTTP method (GET, POST, etc.).
            headers: Request headers dictionary.
            body: Request body bytes.
            query_params: Query parameters dictionary.
            cookies: Cookie values from request.

        Returns:
            ProxyResponse with status code, raw content bytes, content type,
            and forwarded upstream headers. error_message is set only for
            api_dock-level errors, not upstream errors.
        """
        prepared = await self.prepare_remote_request(
            remote_name=remote_name,
            path=path,
            method=method,
            headers=headers,
            body=body,
            query_params=query_params,
            cookies=cookies,
            multi_query_params=multi_query_params,
        )

        if isinstance(prepared, ProxyResponse):
            return prepared

        async with httpx.AsyncClient(
            follow_redirects=prepared.follow_redirects, timeout=prepared.timeout
        ) as client:
            try:
                response = await client.request(
                    method=prepared.method,
                    url=prepared.url,
                    headers=prepared.headers,
                    content=prepared.body,
                    params=prepared.params,
                    cookies=prepared.cookies,
                )

                content_type = response.headers.get("content-type", "application/octet-stream")
                forwarded_headers = _filter_response_headers(dict(response.headers))

                return ProxyResponse(
                    status_code=response.status_code,
                    content=response.content,
                    content_type=content_type,
                    headers=forwarded_headers,
                )

            except httpx.RequestError as e:
                return _error_response(502, f"Error connecting to remote API: {str(e)}")
            except Exception as e:
                return _error_response(500, f"Internal server error: {str(e)}")

    async def map_database_route(
            self,
            database_name: str,
            path: str,
            query_params: Optional[Dict[str, str]] = None,
            cookies: Optional[Dict[str, str]] = None,
            multi_query_params: Optional[Dict[str, List[str]]] = None) -> ProxyResponse:
        """Execute a SQL query for a database route and return results as JSON.

        Args:
            database_name: Name of the database.
            path: The path to match against database routes.
            query_params: Optional dictionary of query parameters from URL
                (single value per key; last value wins for repeated keys).
            cookies: Optional dictionary of cookie values from request.
            multi_query_params: Optional dictionary mapping query parameter names
                to the full list of values received, preserving keys repeated in
                the URL (e.g. ?id=1&id=2). Enables ``multivalue_sql`` templates.

        Returns:
            ProxyResponse with JSON content. error_message is set on failure.
        """
        if query_params is None:
            query_params = {}
        if cookies is None:
            cookies = {}
        if multi_query_params is None:
            multi_query_params = {}

        if database_name not in self.database_names:
            return _error_response(404, f"Database '{database_name}' not found")

        located = self._locate_database_request(database_name, path)
        if isinstance(located, ProxyResponse):
            return located
        version, actual_path, database_config = located

        filtered_cookies = filter_cookies_by_config(cookies, database_config)
        auth_error = _check_database_auth(filtered_cookies, database_config)
        if auth_error is not None:
            return auth_error

        if not actual_path:
            routes = database_config.get("routes", [])
            route_list = [r.get("route", "") for r in routes if isinstance(r, dict)]
            return _json_response({"routes": route_list})

        route_config = find_database_route(actual_path, database_config)
        if route_config is None:
            return _error_response(
                404, f"Route '{actual_path}' not found in database '{database_name}'"
            )
        route_config = merge_query_params(route_config, database_config)
        path_params = extract_path_parameters(actual_path, route_config.get("route", ""))

        early_response = _early_query_response(route_config, query_params, path_params, cookies)
        if early_response is not None:
            return early_response

        backend = self._select_backend(database_name, version, database_config)
        query = _build_query(
            route_config, database_config, path_params, query_params,
            filtered_cookies, multi_query_params, backend.marker
        )
        if isinstance(query, ProxyResponse):
            return query
        sql, values = query
        return await _run_query(backend, sql, values, (database_name, version))

    def is_remote_name(self, name: str) -> bool:
        """Check if a given name is a configured remote name.

        Args:
            name: The name to check.

        Returns:
            True if name is a remote name, False otherwise.
        """
        return name in self.remote_names

    def is_database_name(self, name: str) -> bool:
        """Check if a given name is a configured database name.

        Args:
            name: The name to check.

        Returns:
            True if name is a database name, False otherwise.
        """
        return name in self.database_names

    def get_remote_names(self) -> List[str]:
        """Get list of configured remote names.

        Returns:
            List of remote names.
        """
        return self.remote_names.copy()

    def get_database_names(self) -> List[str]:
        """Get list of configured database names.

        Returns:
            List of database names.
        """
        return self.database_names.copy()

    def map_route_sync(self, remote_name: str, path: str, method: str,
                      headers: Optional[Dict[str, str]] = None,
                      body: Optional[bytes] = None,
                      query_params: Optional[Dict[str, str]] = None,
                      cookies: Optional[Dict[str, str]] = None,
                      multi_query_params: Optional[Dict[str, List[str]]] = None) -> ProxyResponse:
        """Synchronous version of map_route for frameworks that don't support async.

        Args:
            remote_name: Name of the remote API.
            path: The path to proxy to the remote API.
            method: HTTP method (GET, POST, etc.).
            headers: Request headers dictionary.
            body: Request body bytes.
            query_params: Query parameters dictionary.
            cookies: Cookie values from request.
            multi_query_params: Optional mapping of query parameter names to the
                full list of values received, preserving repeated keys.

        Returns:
            ProxyResponse — same contract as map_route.
        """
        import asyncio

        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            result = loop.run_until_complete(
                self.map_route(
                    remote_name, path, method, headers, body, query_params, cookies,
                    multi_query_params
                )
            )
            loop.close()
            return result
        except Exception as e:
            return _error_response(500, f"Sync wrapper error: {str(e)}")

    def _postgres_configs(self) -> Dict[DatabaseKey, Dict[str, Any]]:
        """Collect PostgreSQL configs by (database name, version)."""
        return {
            (name, version): config
            for name, configs in self._snapshots.items()
            for version, config in configs.items()
            if get_backend_name(config) == POSTGRES_BACKEND
        }

    def _locate_database_request(
            self, database_name: str,
            path: str) -> Union[ProxyResponse, Tuple[Optional[str], str, Dict[str, Any]]]:
        """Return (version, route path, config), or a listing/error response.

        A database with a PostgreSQL version uses the configs loaded at startup.
        Other databases are read from their files on each request. If one of
        those files now uses PostgreSQL, the request returns a restart-required
        error.
        """
        versions = self._database_versions(database_name)
        if self._latest_needs_restart(database_name, path, versions):
            return _error_response(500, RESTART_REQUIRED_MESSAGE)
        if versions is not None and not path:
            return _json_response({"versions": versions})

        located = _split_version(database_name, path, versions)
        if isinstance(located, ProxyResponse):
            return located
        version, actual_path = located
        snapshot = self._snapshot(database_name)
        if snapshot is not None:
            return version, actual_path, snapshot[version]
        database_config = self._load_live_config(database_name, version)
        if isinstance(database_config, ProxyResponse):
            return database_config
        return version, actual_path, database_config

    def _load_live_config(
            self, database_name: str,
            version: Optional[str]) -> Union[ProxyResponse, Dict[str, Any]]:
        """Load and merge one DuckDB config.

        Return 404 for a missing file or a restart-required error for a changed backend.
        """
        try:
            database_config = load_database_config(database_name, self.config_dir, version=version)
        except FileNotFoundError:
            return _error_response(404, f"Configuration for database '{database_name}' not found")
        if database_config.get(BACKEND_KEY, DUCKDB_BACKEND) != DUCKDB_BACKEND:
            return _error_response(500, RESTART_REQUIRED_MESSAGE)
        return merge_inherited_config(database_config, self.config)

    def _requires_restart(self, database_name: str, version: Optional[str]) -> bool:
        """Return whether a live config loads successfully and names a non-DuckDB backend."""
        try:
            database_config = load_database_config(database_name, self.config_dir, version=version)
        except Exception:
            return False
        return database_config.get(BACKEND_KEY, DUCKDB_BACKEND) != DUCKDB_BACKEND

    def _listed_versions(self, database_name: str) -> List[Optional[str]]:
        """Return versions for catalog listings; [None] denotes an unversioned database.

        Use startup versions for snapshotted databases. For live databases, omit
        versions that require a restart.
        """
        versions = self._database_versions(database_name)
        listed: List[Optional[str]] = [None] if versions is None else list(versions)
        if self._snapshot(database_name) is not None:
            return listed
        return [v for v in listed if not self._requires_restart(database_name, v)]

    def _snapshot(self, database_name: str) -> Optional[Dict[Optional[str], Dict[str, Any]]]:
        """Return the configs loaded at startup, or None if the database is read live."""
        # getattr, as in get_config_metadata, for mappers built without __init__.
        return getattr(self, "_snapshots", {}).get(database_name)

    def _database_versions(self, database_name: str) -> Optional[List[str]]:
        """Return the database's versions, or None if it is unversioned.

        A snapshotted database uses its startup versions. Other databases are
        read from the config folder.
        """
        snapshot = self._snapshot(database_name)
        if snapshot is not None:
            return None if None in snapshot else sorted(snapshot)
        if is_versioned_database(database_name, self.config_dir):
            return list(get_database_versions(database_name, self.config_dir))
        return None

    def _latest_needs_restart(
            self, database_name: str, path: str, versions: Optional[List[str]]) -> bool:
        """Return whether a version listing or a latest request must wait for a restart.

        Both requests depend on every version of a live database. If any of
        those version files now uses PostgreSQL, the answer would be wrong
        until restart. Snapshotted and unversioned databases never need this.
        """
        if versions is None or self._snapshot(database_name) is not None:
            return False
        if path and path.partition("/")[0] != "latest":
            return False
        return any(self._requires_restart(database_name, v) for v in versions)

    def _select_backend(
            self, database_name: str, version: Optional[str],
            database_config: Dict[str, Any]) -> DatabaseBackend:
        """Return the started PostgreSQL backend or a fresh DuckDB backend.

        PostgreSQL access outside its owning lifecycle raises DatabaseLifecycleError.
        """
        if get_backend_name(database_config) == POSTGRES_BACKEND:
            return self._postgres.backend((database_name, version))
        return DuckDBBackend(database_config)

    def _is_remote_filename(self, filename: str) -> bool:
        """Check if a filename corresponds to a remote config file.

        Args:
            filename: Potential remote filename.

        Returns:
            True if filename matches a remote config file.
        """
        remotes = self.config.get("remotes", [])
        for remote in remotes:
            if isinstance(remote, str) and remote == filename:
                return True
        return False

    def _get_remote_name_by_filename(self, filename: str) -> Optional[str]:
        """Get the actual remote name for a given filename.

        Args:
            filename: Remote config filename.

        Returns:
            Actual remote name or None if not found.
        """
        from api_dock.config import get_remote_mapping

        mapping = get_remote_mapping(self.config, self.config_dir)
        for remote_name, config_path in mapping.items():
            if config_path and filename in config_path:
                return remote_name
        return None


#
# INTERNAL
#
def _load_checked_database(
        database_name: str,
        main_config: Dict[str, Any],
        config_dir: str) -> Dict[Optional[str], Dict[str, Any]]:
    """Load, merge and validate every version of a configured database.

    Return configs keyed by version (None if unversioned). Missing or invalid
    configs raise ValueError naming the database and version.
    """
    if is_versioned_database(database_name, config_dir):
        versions: List[Optional[str]] = list(get_database_versions(database_name, config_dir))
    else:
        versions = [None]

    configs: Dict[Optional[str], Dict[str, Any]] = {}
    for version in versions:
        label = database_label((database_name, version))
        try:
            database_config = load_database_config(database_name, config_dir, version=version)
        except FileNotFoundError as error:
            raise ValueError(
                f"{label} is listed in the main config but has no config file"
            ) from error
        merged = merge_inherited_config(database_config, main_config)
        try:
            check_database_config(merged)
        except ValueError as error:
            raise ValueError(f"{label}, {error}") from error
        configs[version] = merged
    return configs


def _split_version(
        database_name: str,
        path: str,
        versions: Optional[List[str]]) -> Union[ProxyResponse, Tuple[Optional[str], str]]:
    """Resolve the leading version, including latest, and return the remaining path.

    None means unversioned. Unknown versions return a 404 response.
    """
    if versions is None:
        return None, path
    first, _, rest = path.partition("/")
    if first == "latest":
        version = resolve_latest_database_version(versions)
        if version is None:
            return _error_response(404, f"No versions found for database '{database_name}'")
        return version, rest
    if first in versions:
        return first, rest
    return _error_response(404, f"Configuration for database '{database_name}' not found")


def _check_database_auth(
        cookies: Dict[str, str], database_config: Dict[str, Any]) -> Optional[ProxyResponse]:
    """Return an authentication error response, or None when authentication succeeds."""
    auth_config = get_authentication_config(database_config)
    if not auth_config:
        return None
    try:
        is_valid, status_code, response_body = validate_authentication(cookies, auth_config)
    except Exception:
        return _error_response(500, "Authentication error")
    if is_valid:
        return None
    if response_body:
        content = json.dumps(response_body).encode()
    else:
        content = b'{"error": "Authentication failed"}'
    return ProxyResponse(
        status_code=status_code,
        content=content,
        content_type="application/json",
        error_message="Authentication failed",
    )


def _early_query_response(
        route_config: Dict[str, Any],
        query_params: Dict[str, str],
        path_params: Dict[str, str],
        cookies: Dict[str, str]) -> Optional[ProxyResponse]:
    """Return a parameter-driven response, or None when SQL should run."""
    try:
        should_return_early, response_data, status_code, error_message = process_query_parameters(
            route_config, query_params, path_params, cookies
        )
    except Exception:
        return _error_response(500, "Query parameter processing error")
    if not should_return_early:
        return None
    content = json.dumps(response_data).encode() if response_data is not None else b""
    return ProxyResponse(
        status_code=status_code,
        content=content,
        content_type="application/json",
        error_message=error_message,
    )


def _build_query(
        route_config: Dict[str, Any],
        database_config: Dict[str, Any],
        path_params: Dict[str, str],
        query_params: Dict[str, str],
        cookies: Dict[str, str],
        multi_query_params: Dict[str, List[str]],
        marker: str) -> Union[ProxyResponse, Tuple[str, List[str]]]:
    """Build SQL and bound values for the backend, or return a selection/build error."""
    try:
        return build_sql_query(
            route_config, database_config, path_params, query_params,
            cookies, multi_query_params, marker=marker
        )
    except SqlSelectionError as e:
        return ProxyResponse(
            status_code=e.status_code,
            content=json.dumps(e.response).encode(),
            content_type="application/json",
            error_message=str(e.response.get("error")) if e.response.get("error") else None,
        )
    except ValueError:
        return _error_response(500, "SQL query error")


async def _run_query(
        backend: DatabaseBackend, sql: str, values: List[str], key: DatabaseKey) -> ProxyResponse:
    """Return query rows as JSON, 503 for unavailability or 500 for other errors.

    Error responses exclude driver diagnostics. Unexpected pool closure is logged.
    """
    try:
        columns, rows = await backend.execute(sql, values)
        return _json_response([
            {column: _make_json_safe(value) for column, value in zip(columns, row)}
            for row in rows
        ])
    except DatabaseUnavailableError:
        return _error_response(503, "Database unavailable")
    except DatabaseLifecycleError:
        logger.exception("%s: connection pool closed unexpectedly", database_label(key))
        return _error_response(500, "Database query error")
    except Exception:
        return _error_response(500, "Database query error")


def _resolve_timeout(value: Any) -> Optional[float]:
    """Resolve the configured timeout to seconds, or None to disable it.

    A null/false value (YAML `null`/`false`) means no timeout. Any other value
    is coerced to a float number of seconds.

    Args:
        value: Raw `timeout` setting value (defaults to DEFAULT_TIMEOUT upstream).

    Returns:
        Float seconds, or None for no timeout.
    """
    if value is None or value is False:
        return None
    return float(value)


def _error_response(status_code: int, message: str) -> ProxyResponse:
    """Build a JSON ProxyResponse for an api_dock-level error.

    Args:
        status_code: HTTP status code (e.g. 404, 403, 500).
        message: Human-readable error description.

    Returns:
        ProxyResponse with JSON body {"error": message} and error_message set.
    """
    return ProxyResponse(
        status_code=status_code,
        content=json.dumps({"error": message}).encode(),
        content_type="application/json",
        error_message=message,
    )


def _json_response(data: Any, status_code: int = 200) -> ProxyResponse:
    """Build a JSON ProxyResponse for a successful api_dock-generated result.

    Args:
        data: JSON-serializable data to return.
        status_code: HTTP status code (default 200).

    Returns:
        ProxyResponse with JSON body and no error_message.
    """
    return ProxyResponse(
        status_code=status_code,
        content=json.dumps(data).encode(),
        content_type="application/json",
    )


def _filter_response_headers(headers: Dict[str, str]) -> Dict[str, str]:
    """Strip headers that must not be forwarded from upstream to client.

    Removes hop-by-hop headers (RFC 7230 §6.1) and headers whose values
    would be incorrect after httpx's automatic decompression. Content-Type
    is also excluded because it is stored separately in ProxyResponse.content_type.

    Args:
        headers: Raw headers from the upstream httpx response.

    Returns:
        Filtered dict containing only headers safe to forward.
    """
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS
    }


def _make_json_safe(value: Any) -> Any:
    """Convert a database value to a value json can write.

    Dictionaries, lists and tuples are converted item by item; tuples become
    lists. Dates and times become ISO strings, decimals become floats (which
    may lose precision), bytes become base64 text, UUIDs and network addresses
    become strings, and intervals become a number of seconds. Other values are
    returned unchanged, so an unsupported type still fails JSON encoding.

    Args:
        value: Value to convert.

    Returns:
        JSON-safe version of the value.
    """
    if isinstance(value, dict):
        return {key: _make_json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_make_json_safe(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return base64.b64encode(value).decode('utf-8')
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, (UUID,) + IP_ADDRESS_TYPES):
        return str(value)
    return value

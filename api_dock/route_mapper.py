"""

Route Mapper Module for API Dock

Standalone route mapping functionality that can be integrated into any web framework.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import asyncio
import base64
import ipaddress
import json
import logging
import os
import re
import threading
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union
from urllib.parse import parse_qsl
from uuid import UUID

import httpx
import yaml

from api_dock.auth import validate_authentication
from api_dock.config import (
    DEFAULT_CONFIG_DIR,
    filter_cookies_by_config,
    filter_remote_query_params,
    find_remote_config,
    find_route_mapping,
    get_authentication_config,
    get_database_names,
    get_inline_remote_configs,
    get_remote_names,
    get_remote_versions,
    get_settings,
    is_versioned_remote,
    load_main_config,
    merge_inherited_config,
    resolve_latest_version,
    route_allowed_by_config,
)
from api_dock.database_config import (
    apply_shared_definitions,
    check_database_config,
    check_table_definitions,
    DATABASE_SCHEMA_KEY,
    find_database_route,
    get_database_versions,
    get_local_table_references,
    get_schema_sources,
    is_versioned_database,
    load_database_config,
    load_shared_config,
    merge_query_params,
    SCHEMA_GROUPS_KEY,
    SHARED_CONFIG_KEY,
    SHARED_CONNECTIONS_KEY,
    SHARED_SCHEMA_KEY,
)
from api_dock.database_backends import (
    DatabaseLifecycleError,
    DatabaseUnavailableError,
    DUCKDB_SETTINGS_KEY,
    DuckDBBackend,
)
from api_dock.listings import build_listing, resolve_listing_specs
from api_dock.lookups import (
    check_endpoint_token,
    get_store,
    load_lookup_specs,
    LookupContext,
    parse_lookup_settings,
)
from api_dock.sql_builder import (
    build_sql_query_with_tables,
    check_table_references,
    extract_path_parameters,
    process_query_parameters,
    route_engine,
    route_tables,
    SOURCE_COLUMNS_KEY,
    SqlSelectionError,
)
from api_dock.types import PreparedRequest, ProxyResponse, SqlContext


#
# CONSTANTS
#
DEFAULT_VERSION: str = "latest"

# settings.allow_nested_names: whether names may contain "/".
ALLOW_NESTED_NAMES_KEY: str = "allow_nested_names"

# A route segment that is a variable ({{name}}) matches any request segment.
ROUTE_VARIABLE_PATTERN = re.compile(r"^\{\{[^{}]+\}\}$")

# Upstream Set-Cookie headers are kept apart (one value per cookie), since a
# headers dict would merge several into one.
SET_COOKIE_HEADER: str = "set-cookie"

# settings.lookups: the manual refresh endpoint.
LOOKUP_SETTINGS_KEY: str = "lookups"

# Bounds on how long the background refresh sleeps between checks (seconds).
MIN_REFRESH_WAIT: float = 1.0
MAX_REFRESH_WAIT: float = 3600.0

logger = logging.getLogger(__name__)

# Default upstream request timeout in seconds. Override with the `timeout`
# setting; set it to null/false to disable the timeout entirely.
DEFAULT_TIMEOUT: float = 10.0

# Network address types that are written to JSON as their string form.
IP_ADDRESS_TYPES: Tuple[type, ...] = (
    ipaddress.IPv4Address, ipaddress.IPv6Address,
    ipaddress.IPv4Interface, ipaddress.IPv6Interface,
    ipaddress.IPv4Network, ipaddress.IPv6Network,
)

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


# Request headers never forwarded to an upstream remote. Host must be the
# upstream's own (httpx sets it): forwarding the client's Host makes the upstream
# build redirects and absolute URLs that point back at the proxy. The rest are
# hop-by-hop headers, or (content-length) recomputed by httpx from the body.
EXCLUDED_REQUEST_HEADERS: frozenset = frozenset({
    "connection",
    "content-length",
    # The client's cookies never pass through as-is: the Cookie header sent
    # upstream is built from the remote's `cookies` setting (see _cookie_header).
    "cookie",
    "host",
    "keep-alive",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
})

# `settings.base_path`: an optional URL prefix (e.g. "/dock") the API is also
# served under, for when a proxy/CDN forwards a path prefix unchanged.
BASE_PATH_KEY: str = "base_path"


#
# PUBLIC
#
def normalize_base_path(base_path: Any) -> Optional[str]:
    """Normalize a ``base_path`` setting to ``/prefix`` form.

    Args:
        base_path: Configured value (e.g. "dock", "/dock/"), or None/empty.

    Returns:
        "/prefix" without a trailing slash, or None if no prefix is set.
    """
    if not base_path:
        return None
    stripped = str(base_path).strip().strip("/")
    return f"/{stripped}" if stripped else None


def strip_base_path(path: str, base_path: Optional[str]) -> str:
    """Remove a base path prefix from a request path, if present.

    Paths without the prefix are returned unchanged, so the API answers both
    with and without it (e.g. direct calls and health checks still work).

    Args:
        path: Request path, e.g. "/dock/birdnet/latest/detections/".
        base_path: Normalized prefix (see normalize_base_path), or None.

    Returns:
        The path without the prefix ("/birdnet/latest/detections/"), "/" for
        the prefix itself, or the original path.
    """
    if not base_path:
        return path
    if path == base_path or path == f"{base_path}/":
        return "/"
    if path.startswith(f"{base_path}/"):
        return path[len(base_path):]
    return path


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
        a bad config stops startup instead of failing on a live request.

        Args:
            config_path: Path to main config file. If None, uses default.

        Raises:
            ValueError: If the main config can't be read (a missing file passed
                explicitly, invalid YAML, not a mapping), or a listed database
                config is missing or fails a check.
        """
        self.config = _load_main_config_or_raise(config_path)

        self.config_dir = os.path.dirname(config_path) if config_path else DEFAULT_CONFIG_DIR
        self.settings = get_settings(self.config)
        self.lookup_endpoint = parse_lookup_settings(self.settings.get(LOOKUP_SETTINGS_KEY))
        self._start_lookups()
        self.remote_names, self.database_names = self._check_configs()
        _warn_ignored_remote_authentication(self.remote_names, self.config, self.config_dir)
        self.listing_specs, self.listing_warnings = resolve_listing_specs(
            self.config, self.config_dir
        )
        self.base_path = normalize_base_path(self.settings.get(BASE_PATH_KEY))
        shared_file = load_shared_config(self.config_dir)

        # PostgreSQL connections are fixed at startup; their pools open in start().
        self.connections: Dict[str, Any] = (
            shared_file.get(SHARED_CONFIG_KEY, {}).get(SHARED_CONNECTIONS_KEY) or {}
        )
        self._postgres: Any = None
        self._refresh_task: Optional[asyncio.Task] = None
        self._refresh_thread: Optional[threading.Thread] = None
        self.duckdb_backend = DuckDBBackend(
            self.settings.get(DUCKDB_SETTINGS_KEY), self.connections
        )

    async def start(self) -> None:
        """Open PostgreSQL connection pools and start refreshing lookups.

        Pools are opened for each ``database.connections`` entry; lookups with a
        ``refresh`` interval are re-run in the background. The FastAPI app calls
        this (and aclose) in its lifespan. Use the mapper on the event loop that
        started it.

        Raises:
            RuntimeError: If the PostgreSQL packages aren't installed.
            ValueError: If a connection's settings are invalid (e.g. an unset
                ``env:`` variable).
        """
        if self._refresh_task is None and self.lookups.seconds_until_due() is not None:
            self._refresh_task = asyncio.create_task(self._refresh_lookups_forever())
        if not self.connections or self._postgres is not None:
            return
        try:
            from api_dock.postgres_backend import PostgresPools
        except ImportError as error:
            raise RuntimeError(
                "PostgreSQL connections are configured; install them with "
                "`pip install 'api_dock[postgres]'`"
            ) from error
        pools = PostgresPools(self.connections)
        await pools.start()
        self._postgres = pools

    async def aclose(self) -> None:
        """Stop refreshing lookups and close the PostgreSQL pools. Safe to call more than once."""
        task = getattr(self, "_refresh_task", None)
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            self._refresh_task = None
        if self._postgres is not None:
            await self._postgres.aclose()

    def refresh_lookups(self, names: Optional[List[str]] = None) -> Dict[str, Any]:
        """Run lookups now and keep their rows if the config they produce is valid.

        The new rows are checked with the startup checks first; if they fail,
        every lookup keeps its previous rows (see LookupStore.run). Blocks while
        lookups run, so call it from a worker thread in async code.

        Args:
            names: Lookups to run (default: all).

        Returns:
            The run report: refreshed, failed, rejected and skipped.

        Raises:
            ValueError: If a name isn't a defined lookup.
        """
        report = self.lookups.run(names, validate=self._check_configs)
        if report["refreshed"]:
            self.remote_names = get_remote_names(self.config, self.config_dir)
            self.database_names = get_database_names(self.config, self.config_dir)
        return report

    def refresh_due_lookups(self) -> Optional[Dict[str, Any]]:
        """Refresh the lookups whose ``refresh`` interval has passed.

        Returns:
            The run report, or None if none was due.
        """
        due = self.lookups.due()
        return self.refresh_lookups(due) if due else None

    def refresh_due_lookups_in_background(self) -> None:
        """Start refreshing due lookups in a thread (for servers without a lifespan, e.g. Flask).

        Does nothing if none is due or a refresh thread is already running.
        """
        thread = getattr(self, "_refresh_thread", None)
        lookups = getattr(self, "lookups", None)
        if lookups is None or (thread is not None and thread.is_alive()) or not lookups.due():
            return
        self._refresh_thread = threading.Thread(
            target=self._refresh_quietly, name="api-dock-lookups", daemon=True
        )
        self._refresh_thread.start()

    def lookup_endpoint_response(
            self,
            method: str,
            authorization: Optional[str],
            names: Optional[List[str]] = None) -> ProxyResponse:
        """Handle a request to the ``settings.lookups.refresh_route`` endpoint.

        GET returns each lookup's status; POST refreshes lookups (all, or the
        given names) and returns the report with the new status. Requests need
        ``Authorization: Bearer <token>``. Blocks during a refresh.

        Args:
            method: HTTP method.
            authorization: The Authorization header, or None.
            names: Lookups to refresh (POST), or None for all.

        Returns:
            ProxyResponse: 200 with JSON, 401 without the right token, 404 for
            an unknown lookup, 405 for other methods.
        """
        if self.lookup_endpoint is None:
            return _error_response(404, "Not found")
        if not check_endpoint_token(authorization, self.lookup_endpoint.token):
            return _error_response(401, "Unauthorized")
        method = method.upper()
        if method == "GET":
            return _json_response({"lookups": self.lookups.status()})
        if method != "POST":
            return _error_response(405, "Method not allowed")
        try:
            report = self.refresh_lookups(names or None)
        except ValueError as error:
            return _error_response(404, str(error))
        return _json_response({**report, "lookups": self.lookups.status()})

    def _start_lookups(self) -> None:
        """Load lookup definitions and run every lookup once (at startup).

        Raises:
            ValueError: If a lookup definition is invalid or a ``required``
                lookup fails.
        """
        specs = load_lookup_specs(self.config_dir)
        self.lookups = get_store(self.config_dir)
        self.lookups.configure(specs, LookupContext(
            self.config_dir, self.config, self.settings.get(DUCKDB_SETTINGS_KEY)
        ))
        if not specs:
            return
        report = self.lookups.run()
        for name, message in report["failed"].items():
            if specs[name].required:
                raise ValueError(f"Required lookup '{name}' failed: {message}")
            logger.warning("Lookup '%s' failed at startup; starting without its rows: %s",
                           name, message)

    def _check_configs(self) -> Tuple[List[str], List[str]]:
        """Check every served remote and database config (the startup checks).

        Returns:
            (remote names, database names) as served.

        Raises:
            ValueError: If a config fails a check.
        """
        remote_names = get_remote_names(self.config, self.config_dir)
        _check_inline_remotes(remote_names, self.config_dir)
        try:
            database_names = get_database_names(self.config, self.config_dir)
            shared_file = load_shared_config(self.config_dir)
        except (ValueError, yaml.YAMLError) as error:
            raise ValueError(f"Shared database config (databases/config.yaml): {error}") from error
        for database_name in database_names:
            _check_database(database_name, self.config, self.config_dir, shared_file)
        _check_names(remote_names, database_names,
                     bool(self.settings.get(ALLOW_NESTED_NAMES_KEY, True)))
        _check_nested_shadowing(remote_names, database_names, self.config, self.config_dir,
                                shared_file)
        return remote_names, database_names

    def split_name(self, full_path: str) -> Optional[Tuple[str, str]]:
        """Find the remote or database a request path is for.

        Names may contain "/" (``birdnet/2.4/bullfrog``), so the longest served
        name the path starts with wins; the rest is the version and route.

        Args:
            full_path: The request path without its leading "/" (e.g.
                ``birdnet/2.4/bullfrog/0.5/recordings/3/detections/``).

        Returns:
            ``(name, rest)``, or None if no served name matches.
        """
        for name in sorted(set(self.remote_names) | set(self.database_names), key=len,
                           reverse=True):
            if full_path == name or full_path == f"{name}/":
                return name, ""
            if full_path.startswith(f"{name}/"):
                return name, full_path[len(name) + 1:]
        return None

    async def _refresh_lookups_forever(self) -> None:
        """Refresh lookups as they come due, until cancelled."""
        while True:
            wait = self.lookups.seconds_until_due()
            if wait is None:
                return
            await asyncio.sleep(min(max(wait, MIN_REFRESH_WAIT), MAX_REFRESH_WAIT))
            await asyncio.to_thread(self._refresh_quietly)

    def _refresh_quietly(self) -> None:
        """Refresh due lookups, logging instead of raising."""
        try:
            self.refresh_due_lookups()
        except Exception:
            logger.exception("Refreshing lookups failed")

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
        return build_listing(spec, self.config, self.config_dir)

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

        versions = (
            get_remote_versions(remote_name, self.config, self.config_dir)
            if is_versioned_remote(remote_name, self.config, self.config_dir) else None
        )
        resolved = _split_version("remote", remote_name, path, versions)
        if isinstance(resolved, ProxyResponse):
            return resolved
        version, actual_path = resolved

        # Load the remote's config once; the allow-list check uses it too.
        try:
            remote_config: Optional[Dict[str, Any]] = find_remote_config(
                remote_name, self.config, self.config_dir, version=version
            )
        except FileNotFoundError:
            remote_config = None
        if not route_allowed_by_config(actual_path, self.config, remote_config, method):
            return _error_response(
                403, f"Route '{actual_path}' not allowed for remote '{remote_name}'"
            )
        if remote_config is None:
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
        # A query string in a mapped remote_route is sent as query params
        # (before the forwarded ones), and the trailing slash goes on the path.
        final_path, _, mapped_query = final_path.partition("?")
        if mapped_query:
            filtered_query_params = _merge_query_params(
                parse_qsl(mapped_query, keep_blank_values=True), filtered_query_params
            )

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

        upstream_headers = _filter_request_headers(headers or {})
        if filtered_cookies:
            upstream_headers["Cookie"] = _cookie_header(filtered_cookies)

        return PreparedRequest(
            url=full_url,
            method=method,
            headers=upstream_headers,
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
                )

                content_type = response.headers.get("content-type", "application/octet-stream")
                forwarded_headers = {
                    key: value for key, value in _filter_response_headers(
                        dict(response.headers)
                    ).items() if key.lower() != SET_COOKIE_HEADER
                }

                return ProxyResponse(
                    status_code=response.status_code,
                    content=response.content,
                    content_type=content_type,
                    headers=forwarded_headers,
                    set_cookies=response.headers.get_list(SET_COOKIE_HEADER),
                )

            except httpx.RequestError as error:
                logger.warning("Error connecting to %s: %s", prepared.url, error)
                return _error_response(502, "Error connecting to remote API")
            except Exception:
                logger.exception("Proxying to %s failed", prepared.url)
                return _error_response(500, "Internal server error")

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
        query_params = query_params or {}
        cookies = cookies or {}
        multi_query_params = multi_query_params or {}

        if database_name not in self.database_names:
            return _error_response(404, f"Database '{database_name}' not found")

        resolved = self._resolve_database_version(database_name, path)
        if isinstance(resolved, ProxyResponse):
            return resolved
        version, actual_path = resolved

        loaded = self._load_database(database_name, version)
        if isinstance(loaded, ProxyResponse):
            return loaded
        database_config, shared_file = loaded

        filtered_cookies = filter_cookies_by_config(cookies, database_config)
        denied = _check_database_authentication(database_config, filtered_cookies)
        if denied is not None:
            return denied

        if not actual_path:
            routes = database_config.get("routes", [])
            return _json_response({"routes": [
                r.get("route", "") for r in routes if isinstance(r, dict)
            ]})

        route_config = find_database_route(actual_path, database_config)
        if route_config is None:
            return _error_response(
                404, f"Route '{actual_path}' not found in database '{database_name}'"
            )
        route_config = merge_query_params(route_config, database_config)
        path_params = extract_path_parameters(actual_path, route_config.get("route", ""))

        early = _early_query_param_response(route_config, query_params, path_params, cookies)
        if early is not None:
            return early

        built = await self._build_database_query(
            database_name, version, route_config, database_config, shared_file, path_params,
            query_params, filtered_cookies, multi_query_params,
        )
        if isinstance(built, ProxyResponse):
            return built
        return await _run_database_query(*built, database_config, shared_file)

    def _resolve_database_version(
            self, database_name: str, path: str) -> Union[ProxyResponse, Tuple[Optional[str], str]]:
        """Split a database path into its version and the route path.

        Args:
            database_name: Database name.
            path: Path after the database name.

        Returns:
            ``(version, route path)``, or a ProxyResponse (the version listing,
            a 404, or a 500 for a malformed shared config).
        """
        # Versions come from version files and the shared config's `slugs`, so
        # a malformed shared config surfaces here.
        try:
            versions = (
                get_database_versions(database_name, self.config_dir)
                if is_versioned_database(database_name, self.config_dir) else None
            )
        except (ValueError, yaml.YAMLError):
            return _error_response(500, "Shared database configuration error")
        return _split_version("database", database_name, path, versions)

    def _load_database(
            self, database_name: str, version: Optional[str]
    ) -> Union[ProxyResponse, Tuple[Dict[str, Any], Dict[str, Any]]]:
        """Load a database/version's config as requests see it.

        Merged with the main config (inherited cookies/authentication) and the
        shared routes/query params that apply to it.

        Args:
            database_name: Database name.
            version: Version, or None.

        Returns:
            ``(database config, shared config file)``, or an error ProxyResponse.
        """
        try:
            database_config = load_database_config(
                database_name, self.config_dir, version=version
            )
        except FileNotFoundError:
            return _error_response(404, f"Configuration for database '{database_name}' not found")
        except (ValueError, yaml.YAMLError):
            return _error_response(500, "Shared database configuration error")

        database_config = merge_inherited_config(database_config, self.config)
        try:
            shared_file = load_shared_config(self.config_dir)
            database_config = apply_shared_definitions(
                database_config, shared_file, database_name, version
            )
        except Exception:
            return _error_response(500, "Shared database configuration error")
        return database_config, shared_file

    async def _build_database_query(
            self,
            database_name: str,
            version: Optional[str],
            route_config: Dict[str, Any],
            database_config: Dict[str, Any],
            shared_file: Dict[str, Any],
            path_params: Dict[str, str],
            query_params: Dict[str, str],
            cookies: Dict[str, str],
            multi_query_params: Dict[str, List[str]],
    ) -> Union[ProxyResponse, Tuple[Any, str, List[Optional[str]], List[Any]]]:
        """Choose the engine and build the SQL for a database route.

        Args:
            database_name: Database name.
            version: Version, or None.
            route_config: The route, merged with top-level query params.
            database_config: The version's config.
            shared_file: The shared config file.
            path_params: Values from the path.
            query_params: Query values (one per key).
            cookies: Cookies allowed by the config.
            multi_query_params: Every value of repeated query keys.

        Returns:
            ``(backend, sql, values, table references)``, or an error ProxyResponse.
        """
        shared_config = shared_file.get(SHARED_CONFIG_KEY, {})
        try:
            # Schema -> name/version lookups load every database config, so only
            # do them when the route asks for source columns.
            context = SqlContext(
                name=database_name,
                version=version,
                schema_groups=shared_file.get(SCHEMA_GROUPS_KEY) or {},
                schema_sources=(
                    get_schema_sources(self.database_names, self.config_dir)
                    if route_config.get(SOURCE_COLUMNS_KEY) else {}
                ),
            )
            context.connection = route_engine(
                route_config, database_config, shared_config, context.schema_groups
            )
            backend = self._backend(context.connection)
            if context.connection is not None:
                # Native PostgreSQL unions list every column, so they need each
                # table's columns (cached after the first request per table).
                tables = route_tables(
                    route_config, database_config, shared_config, context.schema_groups
                )
                context.columns = await self._postgres.columns(
                    context.connection, sorted({table.uri for table in tables})
                )
            sql_query, sql_values, table_refs = build_sql_query_with_tables(
                route_config, database_config, path_params, query_params,
                cookies, multi_query_params, shared_config, context, backend.marker
            )
        except SqlSelectionError as e:
            return ProxyResponse(
                status_code=e.status_code,
                content=json.dumps(e.response).encode(),
                content_type="application/json",
                error_message=str(e.response.get("error")) if e.response.get("error") else None,
            )
        except DatabaseLifecycleError as error:
            return _error_response(500, str(error))
        except DatabaseUnavailableError:
            return _error_response(503, "Database unavailable")
        except (ValueError, yaml.YAMLError):
            return _error_response(500, "SQL query error")
        return backend, sql_query, sql_values, table_refs

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
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            logger.error("map_route_sync was called from a running event loop; "
                         "use `await map_route(...)` there")
            return _error_response(500, "Internal server error")
        try:
            # asyncio.run creates and always closes its own loop.
            return asyncio.run(self.map_route(
                remote_name, path, method, headers, body, query_params, cookies,
                multi_query_params
            ))
        except Exception:
            logger.exception("map_route_sync failed")
            return _error_response(500, "Internal server error")

    def _backend(self, connection: Optional[str]) -> Any:
        """The backend a route runs on: DuckDB, or a PostgreSQL connection's pool.

        Args:
            connection: PostgreSQL connection name (from route_engine), or None.

        Returns:
            A DatabaseBackend.

        Raises:
            DatabaseLifecycleError: For a PostgreSQL connection before start()
                (or after aclose(), or from another event loop).
        """
        # getattr: RouteMappers built without __init__ (e.g. in tests) have neither.
        if connection is None:
            return getattr(self, "duckdb_backend", None) or DuckDBBackend()
        if getattr(self, "_postgres", None) is None:
            raise DatabaseLifecycleError(
                "PostgreSQL connections are configured but the route mapper wasn't started; "
                "call `await mapper.start()` (the FastAPI app does this in its lifespan)"
            )
        return self._postgres.backend(connection)


#
# INTERNAL
#
def _check_names(remote_names: List[str], database_names: List[str],
                 allow_nested: bool) -> None:
    """Check served names: no empty or ``latest`` segments; "/" only if allowed.

    Args:
        remote_names: Served remote names.
        database_names: Served database names.
        allow_nested: settings.allow_nested_names.

    Raises:
        ValueError: If a name is invalid.
    """
    for name in list(remote_names) + list(database_names):
        if "/" in name and not allow_nested:
            raise ValueError(
                f"Name '{name}' contains '/', but settings.{ALLOW_NESTED_NAMES_KEY} is false"
            )
        segments = name.split("/")
        if any(not segment for segment in segments):
            raise ValueError(f"Name '{name}' has an empty segment (leading, trailing or '//')")
        if DEFAULT_VERSION in segments:
            raise ValueError(f"Name '{name}' can't contain a '{DEFAULT_VERSION}' segment")


def _check_nested_shadowing(
        remote_names: List[str],
        database_names: List[str],
        main_config: Dict[str, Any],
        config_dir: str,
        shared_file: Dict[str, Any]) -> None:
    """Refuse nested names that would hide part of the name they sit under.

    A request goes to the longest name its path starts with, so a name such as
    ``birdnet/2.4/bullfrog`` takes every ``birdnet/2.4/bullfrog/...`` path. That
    is an error if the outer name could serve such a path: when the outer name
    is a remote (remotes forward any path), when the nested name covers a whole
    version (``birdnet/2.4``), or when one of that version's routes could match
    (``bullfrog/...`` or ``{{var}}/...``).

    Args:
        remote_names: Served remote names.
        database_names: Served database names.
        main_config: The main config.
        config_dir: Base config directory.
        shared_file: The shared database config.

    Raises:
        ValueError: Naming the nested name and what it would hide.
    """
    names = set(remote_names) | set(database_names)
    for inner in names:
        for outer in names:
            if inner == outer or not inner.startswith(f"{outer}/"):
                continue
            rest = inner[len(outer) + 1:].split("/")
            if outer not in database_names:
                raise ValueError(
                    f"'{inner}' is nested under remote '{outer}', which forwards every path, "
                    f"so it would hide part of '{outer}'"
                )
            version: Optional[str] = None
            if is_versioned_database(outer, config_dir):
                if rest[0] not in get_database_versions(outer, config_dir):
                    continue  # not a version of the outer name: nothing to hide
                version, rest = rest[0], rest[1:]
            label = outer if version is None else f"{outer}/{version}"
            if not rest:
                raise ValueError(f"'{inner}' would hide every route of '{label}'")
            for route in _database_route_patterns(outer, version, main_config, config_dir,
                                                  shared_file):
                segments = route.strip("/").split("/")
                if len(segments) >= len(rest) and all(
                        segment == part or ROUTE_VARIABLE_PATTERN.match(segment)
                        for segment, part in zip(segments, rest)):
                    raise ValueError(f"'{inner}' would hide route '{route}' of '{label}'")


def _database_route_patterns(
        name: str, version: Optional[str], main_config: Dict[str, Any], config_dir: str,
        shared_file: Dict[str, Any]) -> List[str]:
    """The route patterns a database/version serves (its own plus shared routes).

    Args:
        name: Database name.
        version: Version, or None.
        main_config: The main config.
        config_dir: Base config directory.
        shared_file: The shared database config.

    Returns:
        Route patterns.
    """
    config = load_database_config(name, config_dir, version=version)
    config = apply_shared_definitions(merge_inherited_config(config, main_config), shared_file,
                                      name, version)
    return [str(route.get("route", "")) for route in config.get("routes") or []
            if isinstance(route, dict)]


def _split_version(
        kind: str, name: str, path: str,
        versions: Optional[List[str]]) -> Union[ProxyResponse, Tuple[Optional[str], str]]:
    """Split ``<version or latest>/<route path>`` for a versioned remote or database.

    Args:
        kind: "remote" or "database" (for messages).
        name: Remote or database name.
        path: Path after the name.
        versions: Available versions, or None if it isn't versioned.

    Returns:
        ``(version, route path)`` (version None if unversioned), or a
        ProxyResponse: the version listing for the bare name, or a 404 for an
        unknown version.
    """
    if versions is None:
        return None, path or ""
    if not path:
        return _json_response({"versions": versions})
    first, _, rest = path.partition("/")
    if first == DEFAULT_VERSION:
        version = resolve_latest_version(versions)
        if version is None:
            return _error_response(404, f"No versions found for {kind} '{name}'")
        return version, rest
    if first in versions:
        return first, rest
    return _error_response(404, f"Configuration for {kind} '{name}' not found")


def _check_database_authentication(
        database_config: Dict[str, Any], cookies: Dict[str, str]) -> Optional[ProxyResponse]:
    """Check a database's ``authentication`` against the request's cookies.

    Args:
        database_config: The database/version config.
        cookies: Cookies allowed by its config.

    Returns:
        None if allowed (or no authentication is configured), else the
        failure ProxyResponse.
    """
    auth_config = get_authentication_config(database_config)
    if not auth_config:
        return None
    try:
        is_valid, status_code, response_body = validate_authentication(cookies, auth_config)
    except Exception:
        return _error_response(500, "Authentication error")
    if is_valid:
        return None
    content = (
        json.dumps(response_body).encode() if response_body
        else b'{"error": "Authentication failed"}'
    )
    return ProxyResponse(status_code=status_code, content=content,
                         content_type="application/json", error_message="Authentication failed")


def _early_query_param_response(
        route_config: Dict[str, Any], query_params: Dict[str, str],
        path_params: Dict[str, str], cookies: Dict[str, str]) -> Optional[ProxyResponse]:
    """A response decided by query params alone (``response``, ``required``, ...).

    Args:
        route_config: The route, merged with top-level query params.
        query_params: Query values.
        path_params: Path values.
        cookies: Request cookies.

    Returns:
        The response, or None to go on and run the query.
    """
    try:
        returns_early, data, status_code, error_message = process_query_parameters(
            route_config, query_params, path_params, cookies
        )
    except Exception:
        return _error_response(500, "Query parameter processing error")
    if not returns_early:
        return None
    return ProxyResponse(
        status_code=status_code,
        content=json.dumps(data).encode() if data is not None else b"",
        content_type="application/json",
        error_message=error_message,
    )


async def _run_database_query(
        backend: Any, sql: str, values: List[Optional[str]], table_refs: List[Any],
        database_config: Dict[str, Any], shared_file: Dict[str, Any]) -> ProxyResponse:
    """Run a built query and return its rows as JSON.

    Args:
        backend: The DatabaseBackend to run on.
        sql: SQL with markers.
        values: Bound values.
        table_refs: Tables the query references.
        database_config: The version's config (its tables get storage access).
        shared_file: The shared config file.

    Returns:
        200 with the rows, 503 if the database is unavailable, 500 on errors.
    """
    shared_config = shared_file.get(SHARED_CONFIG_KEY, {})
    # Authenticate every local table plus any shared tables the query
    # references, then expose [[schema.table]] refs as views.
    auth_tables = get_local_table_references(database_config, shared_config)
    local_names = {table.sql_name for table in auth_tables}
    auth_tables += [ref for ref in table_refs if ref.sql_name not in local_names]
    try:
        columns, rows = await backend.execute(sql, values, auth_tables)
    except DatabaseUnavailableError:
        return _error_response(503, "Database unavailable")
    except Exception:
        return _error_response(500, "Database query error")
    return _json_response([
        {column: _make_json_safe(value) for column, value in zip(columns, row)}
        for row in rows
    ])


def _load_main_config_or_raise(config_path: Optional[str]) -> Dict[str, Any]:
    """Load the main config, failing loudly instead of serving an empty API.

    Args:
        config_path: Path to the main config, or None for the default.

    Returns:
        The main config. Without a path and without a default config file, a
        minimal config (no remotes or databases) is used and a warning logged.

    Raises:
        ValueError: If an explicitly given file is missing, or the file isn't
            valid YAML or isn't a mapping.
    """
    label = config_path or os.path.join(DEFAULT_CONFIG_DIR, "config.yaml")
    try:
        config = load_main_config(config_path)
    except FileNotFoundError as error:
        if config_path is not None:
            raise ValueError(f"Main config not found: {config_path}") from error
        logger.warning("No main config at %s; serving an API with no remotes or databases", label)
        return {"name": "api-dock", "description": "API Dock wrapper", "authors": []}
    except yaml.YAMLError as error:
        raise ValueError(f"Main config {label} isn't valid YAML: {error}") from error
    if config is None:
        return {}
    if not isinstance(config, dict):
        raise ValueError(f"Main config {label} must be a mapping")
    return config


def _warn_ignored_remote_authentication(
        remote_names: List[str], main_config: Dict[str, Any], config_dir: str) -> None:
    """Warn when an ``authentication`` setting would be ignored by remote routes.

    Remote routes aren't gated by api_dock: the upstream API checks credentials
    (the client's cookies reach it through the remote's ``cookies`` setting).
    An ``authentication`` block in a remote's config, or in the main config
    while remotes are served, is easy to mistake for protection, so it is
    logged once at startup.

    Args:
        remote_names: Served remote names.
        main_config: The main config.
        config_dir: Base config directory.
    """
    if main_config.get("authentication") and remote_names:
        logger.warning(
            "The main config's `authentication` applies to database routes only; remote "
            "routes (%s) aren't checked by api_dock (the upstream API must check credentials)",
            ", ".join(remote_names),
        )
    for name in remote_names:
        try:
            versions: List[Optional[str]] = (
                list(get_remote_versions(name, main_config, config_dir))
                if is_versioned_remote(name, main_config, config_dir) else [None]
            )
            for version in versions:
                remote_config = find_remote_config(name, main_config, config_dir, version=version)
                if isinstance(remote_config, dict) and remote_config.get("authentication"):
                    label = name if version is None else f"{name} version {version}"
                    logger.warning(
                        "Remote '%s' has an `authentication` setting, which api_dock doesn't "
                        "apply to remote routes (the upstream API must check credentials)", label
                    )
        except (FileNotFoundError, ValueError, yaml.YAMLError):
            continue


def _check_inline_remotes(remote_names: List[str], config_dir: str) -> None:
    """Check ``remotes/config.yaml`` at startup.

    Args:
        remote_names: Remote names listed in the main config.
        config_dir: Base config directory.

    Raises:
        ValueError: If the file is malformed or a listed remote's entry has no
            ``url``.
    """
    try:
        inline = get_inline_remote_configs(config_dir)
    except (ValueError, yaml.YAMLError) as error:
        raise ValueError(f"Shared remote config (remotes/config.yaml): {error}") from error
    for name, versions in inline.items():
        if name not in remote_names:
            logger.warning(
                "remotes/config.yaml defines '%s', which isn't listed in the main config's "
                "remotes, so it isn't served", name
            )
            continue
        for version, remote_config in versions.items():
            if not remote_config.get("url"):
                label = name if version is None else f"{name} version {version}"
                raise ValueError(f"remotes/config.yaml: '{label}' has no url")


def _check_database(
        database_name: str,
        main_config: Dict[str, Any],
        config_dir: str,
        shared_file: Dict[str, Any]) -> None:
    """Load and check every version of a database listed in the main config.

    Each version (from a config file or the shared config's ``slugs``) is checked
    as a request would see it: merged with the main config and with the shared
    routes/query_params that apply to it (after include/exclude). Besides the
    template checks in check_database_config, every ``[[table]]``,
    ``[[schema.table]]`` and union reference must resolve.

    Args:
        database_name: Name of the database.
        main_config: Main configuration dictionary, for inheritance.
        config_dir: Directory holding the config files.
        shared_file: The loaded shared config (see load_shared_config).

    Raises:
        ValueError: If a config is missing or fails a check. The message names
            the database and version.
    """
    try:
        if is_versioned_database(database_name, config_dir):
            versions: List[Optional[str]] = list(get_database_versions(database_name, config_dir))
        else:
            versions = [None]
    except ValueError as error:
        raise ValueError(f"Database '{database_name}': {error}") from error

    shared_database = shared_file.get(SHARED_CONFIG_KEY, {})
    schema_groups = shared_file.get(SCHEMA_GROUPS_KEY, {})

    for version in versions:
        label = f"Database '{database_name}'"
        if version is not None:
            label += f" version '{version}'"
        try:
            database_config = load_database_config(database_name, config_dir, version=version)
        except FileNotFoundError as error:
            raise ValueError(
                f"{label} is listed in the main config but has no config file"
            ) from error

        database_config = merge_inherited_config(database_config, main_config)
        database_config = apply_shared_definitions(
            database_config, shared_file, database_name, version
        )

        def check_tables(template: str, route_config: Dict[str, Any]) -> None:
            """Fail if a template's [[...]] references don't resolve for this version."""
            check_table_references(
                template, database_config, shared_database, schema_groups,
                route_config.get(SOURCE_COLUMNS_KEY),
            )

        try:
            schema_name = database_config.get(DATABASE_SCHEMA_KEY)
            if schema_name and schema_name not in (shared_database.get(SHARED_SCHEMA_KEY) or {}):
                raise ValueError(
                    f"schema: '{schema_name}' isn't a schema in databases/config.yaml"
                )
            check_table_definitions(database_config.get("tables") or {}, shared_database, "tables")
            check_database_config(database_config, check_tables)
            for index, route_config in enumerate(database_config.get("routes") or []):
                merged = merge_query_params(route_config, database_config)
                try:
                    route_engine(merged, database_config, shared_database, schema_groups)
                except ValueError as error:
                    route_name = route_config.get("route", f"routes[{index}]")
                    raise ValueError(f"route '{route_name}': {error}") from error
        except ValueError as error:
            raise ValueError(f"{label}, {error}") from error


def _merge_query_params(first: List[Tuple[str, str]], params: Dict[str, Any]) -> Dict[str, Any]:
    """Combine (name, value) pairs with a params mapping; repeated names become lists.

    Args:
        first: Pairs that come first (e.g. from a mapped route's query string).
        params: Mapping of name -> value or list of values.

    Returns:
        Mapping of name -> value, or a list for names with several values.
    """
    merged: Dict[str, List[Any]] = {}
    for name, value in first:
        merged.setdefault(name, []).append(value)
    for name, value in (params or {}).items():
        merged.setdefault(name, []).extend(value if isinstance(value, list) else [value])
    return {name: values[0] if len(values) == 1 else values for name, values in merged.items()}


def _cookie_header(cookies: Dict[str, str]) -> str:
    """Write cookies as a Cookie request header value.

    Args:
        cookies: Cookie name -> value (already filtered by the remote's config).

    Returns:
        ``"name1=value1; name2=value2"``.
    """
    return "; ".join(f"{name}={value}" for name, value in cookies.items())


def _filter_request_headers(headers: Dict[str, str]) -> Dict[str, str]:
    """Drop request headers that must not be forwarded upstream.

    Args:
        headers: Incoming client request headers.

    Returns:
        Headers safe to forward (see EXCLUDED_REQUEST_HEADERS).
    """
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in EXCLUDED_REQUEST_HEADERS
    }


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

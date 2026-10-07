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
import os
import re
import threading
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union
from uuid import UUID

import httpx
import yaml

from api_dock.auth import validate_authentication
from api_dock.config import DEFAULT_CONFIG_DIR, filter_cookies_by_config, filter_remote_query_params, find_remote_config, find_route_mapping, get_authentication_config, get_database_names, get_remote_names, get_remote_versions, get_settings, is_route_allowed, is_versioned_remote, load_main_config, merge_inherited_config, resolve_latest_version
from api_dock.database_config import apply_shared_definitions, check_database_config, find_database_route, get_database_versions, get_local_table_references, get_schema_sources, is_versioned_database, load_database_config, load_shared_config, merge_query_params, resolve_latest_database_version, SCHEMA_GROUPS_KEY, SHARED_CONFIG_KEY
from api_dock.listings import build_listing, resolve_listing_specs
from api_dock.sql_builder import build_schema_view_statements, build_sql_query_with_tables, check_table_references, extract_path_parameters, process_query_parameters, SOURCE_COLUMNS_KEY, SqlSelectionError
from api_dock.storage_auth import setup_table_storage_authentication
from api_dock.types import PreparedRequest, ProxyResponse, SqlContext


#
# CONSTANTS
#
DEFAULT_VERSION: str = "latest"

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
    "host",
    "keep-alive",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
})

# `settings.duckdb` options. Every key is applied to each query's DuckDB
# connection as `SET <key> = <value>` (memory_limit, threads, temp_directory,
# ...), except max_concurrent_queries, which caps how many database queries run
# at once in this process (others wait their turn).
DUCKDB_SETTINGS_KEY: str = "duckdb"
MAX_CONCURRENT_QUERIES_KEY: str = "max_concurrent_queries"

# `settings.base_path`: an optional URL prefix (e.g. "/dock") the API is also
# served under, for when a proxy/CDN forwards a path prefix unchanged.
BASE_PATH_KEY: str = "base_path"

DUCKDB_OPTION_PATTERN: re.Pattern = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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
        self.base_path = normalize_base_path(self.settings.get(BASE_PATH_KEY))
        self.duckdb_statements, max_queries = _duckdb_settings(
            self.settings.get(DUCKDB_SETTINGS_KEY)
        )
        self._query_slots = threading.BoundedSemaphore(max_queries) if max_queries else None

        try:
            shared_file = load_shared_config(self.config_dir)
        except (ValueError, yaml.YAMLError) as error:
            raise ValueError(f"Shared database config (databases/config.yaml): {error}") from error
        for database_name in self.database_names:
            _check_database(database_name, self.config, self.config_dir, shared_file)

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
            headers=_filter_request_headers(headers or {}),
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

        # Versions come from version files and the shared config's `slugs`, so
        # a malformed shared config surfaces here.
        try:
            is_versioned = is_versioned_database(database_name, self.config_dir)
            available_versions = (
                get_database_versions(database_name, self.config_dir) if is_versioned else []
            )
        except (ValueError, yaml.YAMLError):
            return _error_response(500, "Shared database configuration error")

        path_parts = path.split("/") if path else []
        version = None
        actual_path = path

        if is_versioned and path_parts:
            potential_version = path_parts[0]

            if potential_version == "latest":
                version = resolve_latest_database_version(available_versions)
                if version is None:
                    return _error_response(404, f"No versions found for database '{database_name}'")
                actual_path = "/".join(path_parts[1:])
            elif potential_version in available_versions:
                version = potential_version
                actual_path = "/".join(path_parts[1:])
            elif not path:
                return _json_response({"versions": available_versions})
            else:
                return _error_response(404, f"Configuration for database '{database_name}' not found")
        elif is_versioned and not path:
            return _json_response({"versions": available_versions})

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
            shared_config = shared_file.get(SHARED_CONFIG_KEY, {})
            database_config = apply_shared_definitions(
                database_config, shared_file, database_name, version
            )
        except Exception:
            return _error_response(500, "Shared database configuration error")

        filtered_cookies = filter_cookies_by_config(cookies, database_config)

        auth_config = get_authentication_config(database_config)
        if auth_config:
            try:
                is_valid, status_code, response_body = validate_authentication(filtered_cookies, auth_config)
                if not is_valid:
                    content = json.dumps(response_body).encode() if response_body else b'{"error": "Authentication failed"}'
                    return ProxyResponse(
                        status_code=status_code,
                        content=content,
                        content_type="application/json",
                        error_message="Authentication failed",
                    )
            except Exception:
                return _error_response(500, "Authentication error")

        if not actual_path or actual_path == "":
            routes = database_config.get("routes", [])
            route_list = [r.get("route", "") for r in routes if isinstance(r, dict)]
            return _json_response({"routes": route_list})

        route_config = find_database_route(actual_path, database_config)
        if route_config is None:
            return _error_response(404, f"Route '{actual_path}' not found in database '{database_name}'")

        route_config = merge_query_params(route_config, database_config)

        route_pattern = route_config.get("route", "")
        path_params = extract_path_parameters(actual_path, route_pattern)

        try:
            should_return_early, response_data, status_code, error_message = process_query_parameters(
                route_config, query_params, path_params, cookies
            )
            if should_return_early:
                content = json.dumps(response_data).encode() if response_data is not None else b""
                return ProxyResponse(
                    status_code=status_code,
                    content=content,
                    content_type="application/json",
                    error_message=error_message,
                )
        except Exception:
            return _error_response(500, "Query parameter processing error")

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
            sql_query, sql_values, table_refs = build_sql_query_with_tables(
                route_config, database_config, path_params, query_params,
                filtered_cookies, multi_query_params, shared_config, context
            )
        except SqlSelectionError as e:
            return ProxyResponse(
                status_code=e.status_code,
                content=json.dumps(e.response).encode(),
                content_type="application/json",
                error_message=str(e.response.get("error")) if e.response.get("error") else None,
            )
        except (ValueError, yaml.YAMLError):
            return _error_response(500, "SQL query error")

        # Authenticate every local table plus any shared tables the query
        # references, then expose [[schema.table]] refs as views.
        auth_tables = get_local_table_references(database_config, shared_config)
        local_names = {table.sql_name for table in auth_tables}
        auth_tables += [ref for ref in table_refs if ref.sql_name not in local_names]

        try:
            # DuckDB calls block; run them in a worker thread so one slow query
            # doesn't stall every other request (and health checks) meanwhile.
            response_data = await asyncio.to_thread(
                self._run_query, sql_query, sql_values, auth_tables,
                build_schema_view_statements(table_refs),
            )
        except Exception:
            return _error_response(500, "Database query error")
        return _json_response(response_data)

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

    def _run_query(
            self,
            sql_query: str,
            sql_values: List[Optional[str]],
            auth_tables: List[Any],
            view_statements: List[str]) -> List[Dict[str, Any]]:
        """Execute a database query on a fresh DuckDB connection (blocking).

        Applies the ``settings.duckdb`` options, storage authentication and
        schema views, then runs the query. Honors max_concurrent_queries.

        Args:
            sql_query: The SQL to run, with a ``?`` marker per bound value.
            sql_values: Values for the markers, in order (sent separately from
                the SQL, so they are never parsed as SQL).
            auth_tables: TableReferences to set up storage authentication for.
            view_statements: CREATE SCHEMA/VIEW statements to run first.

        Returns:
            Result rows as JSON-safe dicts.
        """
        import duckdb

        # getattr: RouteMappers built without __init__ (e.g. in tests) have neither.
        slots = getattr(self, '_query_slots', None)
        if slots is not None:
            slots.acquire()
        try:
            conn = duckdb.connect(database=':memory:')
            try:
                for statement in getattr(self, 'duckdb_statements', []):
                    conn.execute(statement)
                setup_table_storage_authentication(conn, auth_tables)
                for statement in view_statements:
                    conn.execute(statement)
                result = conn.execute(sql_query, sql_values).fetchall()
                columns = [desc[0] for desc in conn.description] if conn.description else []
            finally:
                conn.close()
        finally:
            if slots is not None:
                slots.release()

        return [
            {column: _make_json_safe(value) for column, value in zip(columns, row)}
            for row in result
        ]

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
            check_database_config(database_config, check_tables)
        except ValueError as error:
            raise ValueError(f"{label}, {error}") from error


def _duckdb_settings(options: Any) -> Tuple[List[str], Optional[int]]:
    """Turn ``settings.duckdb`` into SET statements and a concurrency cap.

    Args:
        options: Mapping of DuckDB option -> value, plus optional
            max_concurrent_queries; or None.

    Returns:
        Tuple of (SET statements, max concurrent queries or None).

    Raises:
        ValueError: If options isn't a mapping, an option name isn't a plain
            identifier, or max_concurrent_queries isn't a positive integer.
    """
    if not options:
        return ([], None)
    if not isinstance(options, dict):
        raise ValueError(f"settings.{DUCKDB_SETTINGS_KEY} must be a mapping")

    max_queries = options.get(MAX_CONCURRENT_QUERIES_KEY)
    if max_queries is not None and (not isinstance(max_queries, int) or max_queries < 1):
        raise ValueError(f"settings.{DUCKDB_SETTINGS_KEY}.{MAX_CONCURRENT_QUERIES_KEY} "
                         "must be a positive integer")

    statements = []
    for name, value in options.items():
        if name == MAX_CONCURRENT_QUERIES_KEY:
            continue
        if not DUCKDB_OPTION_PATTERN.match(str(name)):
            raise ValueError(f"settings.{DUCKDB_SETTINGS_KEY}: invalid option name '{name}'")
        if isinstance(value, bool):
            literal = "true" if value else "false"
        elif isinstance(value, (int, float)):
            literal = str(value)
        else:
            literal = "'" + str(value).replace("'", "''") + "'"
        statements.append(f"SET {name} = {literal}")
    return (statements, max_queries)


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

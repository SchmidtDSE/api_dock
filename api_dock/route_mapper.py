"""

Route Mapper Module for API Dock

Standalone route mapping functionality that can be integrated into any web framework.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import json
import httpx
from typing import Any, Dict, List, Optional, Union

from api_dock.auth import validate_authentication
from api_dock.config import filter_cookies_by_config, filter_remote_query_params, find_remote_config, find_route_mapping, get_authentication_config, get_database_names, get_remote_names, get_remote_versions, get_settings, is_route_allowed, is_versioned_remote, load_main_config, merge_inherited_config, resolve_latest_version
from api_dock.database_config import find_database_route, get_database_versions, is_versioned_database, load_database_config, merge_query_params, resolve_latest_database_version
from api_dock.sql_builder import build_sql_query, extract_path_parameters, process_query_parameters
from api_dock.storage_auth import detect_required_backends, extract_table_metadata_by_backend, extract_table_uris, setup_storage_authentication
from api_dock.types import PreparedRequest, ProxyResponse


#
# CONSTANTS
#
DEFAULT_VERSION: str = "latest"

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
class RouteMapper:
    """Standalone route mapper for proxying requests to remote APIs.

    This class handles the core logic of routing requests to remote APIs
    based on configuration files. It can be integrated into any web framework.
    """

    def __init__(self, config_path: Optional[str] = None) -> None:
        """Initialize RouteMapper with configuration.

        Args:
            config_path: Path to main config file. If None, uses default.
        """
        try:
            self.config = load_main_config(config_path)
        except (FileNotFoundError, Exception):
            self.config = {"name": "api-dock", "description": "API Dock wrapper", "authors": []}

        self.remote_names = get_remote_names(self.config)
        self.database_names = get_database_names(self.config)
        self.settings = get_settings(self.config)

    def get_config_metadata(self) -> Dict[str, Any]:
        """Get API metadata from configuration.

        Returns:
            Dictionary containing name, description, authors, endpoints, and remotes.
            Note: Databases are included in remotes list to hide implementation details.
        """
        all_remotes = self.remote_names + self.database_names

        metadata = {
            "name": self.config.get("name", "API Dock"),
            "description": self.config.get("description", "API wrapper using configuration files"),
            "authors": self.config.get("authors", []),
            "endpoints": self.config.get("endpoints", ["/"]),
            "remotes": all_remotes
        }
        return metadata

    async def prepare_remote_request(
            self,
            remote_name: str,
            path: str,
            method: str,
            headers: Optional[Dict[str, str]] = None,
            body: Optional[bytes] = None,
            query_params: Optional[Dict[str, str]] = None,
            cookies: Optional[Dict[str, str]] = None) -> Union[ProxyResponse, PreparedRequest]:
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
            query_params: Query parameters dictionary.
            cookies: Cookie values from request.

        Returns:
            ProxyResponse for api_dock-level errors, or PreparedRequest on success.
        """
        if remote_name not in self.remote_names:
            return _error_response(404, f"Remote '{remote_name}' not found")

        is_versioned = is_versioned_remote(remote_name, self.config)

        path_parts = path.split("/") if path else []
        version = None
        actual_path = path

        if is_versioned and path_parts:
            potential_version = path_parts[0]
            available_versions = get_remote_versions(remote_name, self.config)

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
            available_versions = get_remote_versions(remote_name, self.config)
            return _json_response({"versions": available_versions})

        if not actual_path:
            actual_path = ""

        if not is_route_allowed(actual_path, self.config, remote_name, version, method):
            return _error_response(403, f"Route '{actual_path}' not allowed for remote '{remote_name}'")

        try:
            remote_config = find_remote_config(remote_name, self.config, version=version)
        except FileNotFoundError:
            return _error_response(404, f"Configuration for remote '{remote_name}' not found")

        remote_url = remote_config.get("url")
        if not remote_url:
            return _error_response(500, f"No URL configured for remote '{remote_name}'")

        filtered_query_params = filter_remote_query_params(
            query_params or {}, actual_path, method, remote_config
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

        return PreparedRequest(
            url=full_url,
            method=method,
            headers=headers or {},
            params=filtered_query_params,
            cookies=filtered_cookies,
            body=body,
            follow_redirects=follow_redirects,
        )

    async def map_route(self,
            remote_name: str,
            path: str,
            method: str,
            headers: Optional[Dict[str, str]] = None,
            body: Optional[bytes] = None,
            query_params: Optional[Dict[str, str]] = None,
            cookies: Optional[Dict[str, str]] = None) -> ProxyResponse:
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
        )

        if isinstance(prepared, ProxyResponse):
            return prepared

        async with httpx.AsyncClient(follow_redirects=prepared.follow_redirects) as client:
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
            cookies: Optional[Dict[str, str]] = None) -> ProxyResponse:
        """Execute a SQL query for a database route and return results as JSON.

        Args:
            database_name: Name of the database.
            path: The path to match against database routes.
            query_params: Optional dictionary of query parameters from URL.
            cookies: Optional dictionary of cookie values from request.

        Returns:
            ProxyResponse with JSON content. error_message is set on failure.
        """
        if query_params is None:
            query_params = {}
        if cookies is None:
            cookies = {}

        if database_name not in self.database_names:
            return _error_response(404, f"Database '{database_name}' not found")

        is_versioned = is_versioned_database(database_name)

        path_parts = path.split("/") if path else []
        version = None
        actual_path = path

        if is_versioned and path_parts:
            potential_version = path_parts[0]
            available_versions = get_database_versions(database_name)

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
            available_versions = get_database_versions(database_name)
            return _json_response({"versions": available_versions})

        try:
            database_config = load_database_config(database_name, version=version)
        except FileNotFoundError:
            return _error_response(404, f"Configuration for database '{database_name}' not found")

        database_config = merge_inherited_config(database_config, self.config)

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
            sql_query = build_sql_query(route_config, database_config, path_params, query_params, filtered_cookies)
        except ValueError:
            return _error_response(500, "SQL query error")

        try:
            import duckdb

            conn = duckdb.connect(database=':memory:')

            table_uris = extract_table_uris(database_config)
            required_backends = detect_required_backends(table_uris)
            backend_metadata = extract_table_metadata_by_backend(database_config)
            setup_storage_authentication(conn, required_backends, backend_metadata)

            result = conn.execute(sql_query).fetchall()
            columns = [desc[0] for desc in conn.description] if conn.description else []
            conn.close()

            response_data = []
            for row in result:
                row_dict = {}
                for col, val in zip(columns, row):
                    row_dict[col] = _make_json_safe(val)
                response_data.append(row_dict)

            return _json_response(response_data)

        except Exception:
            return _error_response(500, "Database query error")

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
                      cookies: Optional[Dict[str, str]] = None) -> ProxyResponse:
        """Synchronous version of map_route for frameworks that don't support async.

        Args:
            remote_name: Name of the remote API.
            path: The path to proxy to the remote API.
            method: HTTP method (GET, POST, etc.).
            headers: Request headers dictionary.
            body: Request body bytes.
            query_params: Query parameters dictionary.
            cookies: Cookie values from request.

        Returns:
            ProxyResponse — same contract as map_route.
        """
        import asyncio

        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            result = loop.run_until_complete(
                self.map_route(remote_name, path, method, headers, body, query_params, cookies)
            )
            loop.close()
            return result
        except Exception as e:
            return _error_response(500, f"Sync wrapper error: {str(e)}")

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

        mapping = get_remote_mapping(self.config)
        for remote_name, config_path in mapping.items():
            if config_path and filename in config_path:
                return remote_name
        return None


#
# INTERNAL
#
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
    """Convert non-JSON-serializable values to JSON-safe types.

    Handles datetime objects, dates, decimals, and other common types
    that DuckDB returns but aren't directly JSON serializable.

    Args:
        value: Value to convert.

    Returns:
        JSON-safe version of the value.
    """
    from datetime import date, datetime
    from decimal import Decimal

    if value is None:
        return None
    elif isinstance(value, (datetime, date)):
        return value.isoformat()
    elif isinstance(value, Decimal):
        return float(value)
    elif isinstance(value, bytes):
        import base64
        return base64.b64encode(value).decode('utf-8')
    else:
        return value

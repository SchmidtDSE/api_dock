"""

Route Mapper Module for API Dock

Standalone route mapping functionality that can be integrated into any web framework.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import httpx
from typing import Any, Dict, List, Optional, Tuple

from api_dock.config import find_remote_config, find_route_mapping, get_database_names, get_remote_names, get_remote_versions, get_settings, is_route_allowed, is_versioned_remote, load_main_config, resolve_latest_version
from api_dock.database_config import find_database_route, get_database_versions, is_versioned_database, load_database_config, resolve_latest_database_version
from api_dock.sql_builder import build_sql_query, extract_path_parameters
from api_dock.storage_auth import detect_required_backends, extract_table_metadata_by_backend, extract_table_uris, setup_storage_authentication


#
# CONSTANTS
#
DEFAULT_VERSION: str = "latest"


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
            # For first pass, keep error handling simple
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
        # Merge databases into remotes list (hide implementation from users)
        all_remotes = self.remote_names + self.database_names

        metadata = {
            "name": self.config.get("name", "API Dock"),
            "description": self.config.get("description", "API wrapper using configuration files"),
            "authors": self.config.get("authors", []),
            "endpoints": self.config.get("endpoints", ["/"]),
            "remotes": all_remotes
        }
        return metadata




    async def map_route(self,
            remote_name: str,
            path: str,
            method: str,
            headers: Optional[Dict[str, str]] = None,
            body: Optional[bytes] = None,
            query_params: Optional[Dict[str, str]] = None) -> Tuple[bool, Any, int, Optional[str]]:
        """Map a request to a remote API.

        Args:
            remote_name: Name of the remote API.
            path: The path to proxy to the remote API.
            method: HTTP method (GET, POST, etc.).
            headers: Request headers dictionary.
            body: Request body bytes.
            query_params: Query parameters dictionary.

        Returns:
            Tuple of (success, response_data, status_code, error_message).
            If success is False, error_message contains the reason.
        """
        # Validate remote exists
        if remote_name not in self.remote_names:
            return (False, None, 404, f"Remote '{remote_name}' not found")

        # Check if this is a versioned remote
        is_versioned = is_versioned_remote(remote_name, self.config)

        # Parse version from path if remote is versioned
        path_parts = path.split("/") if path else []
        version = None
        actual_path = path

        if is_versioned and path_parts:
            # First part should be the version for versioned remotes
            potential_version = path_parts[0]
            available_versions = get_remote_versions(remote_name, self.config)

            if potential_version == "latest":
                # Resolve latest to actual version
                version = resolve_latest_version(available_versions)
                if version is None:
                    return (False, None, 404, f"No versions found for remote '{remote_name}'")
                actual_path = "/".join(path_parts[1:])
            elif potential_version in available_versions:
                version = potential_version
                actual_path = "/".join(path_parts[1:])
            elif not path:
                # Empty path on versioned remote - list versions
                return (True, {"versions": available_versions}, 200, None)
            else:
                # Path provided but no valid version - error
                return (
                    False,
                    None,
                    404,
                    f"Configuration for remote '{remote_name}' not found"
                )
        elif is_versioned and not path:
            # Empty path on versioned remote - list versions
            available_versions = get_remote_versions(remote_name, self.config)
            return (True, {"versions": available_versions}, 200, None)

        # Handle empty actual_path - should be allowed as root route
        if not actual_path:
            actual_path = ""

        # Check if route is allowed
        if not is_route_allowed(actual_path, self.config, remote_name, version, method):
            return (
                False,
                None,
                403,
                f"Route '{actual_path}' not allowed for remote '{remote_name}'"
            )

        # Load remote configuration
        try:
            remote_config = find_remote_config(remote_name, self.config, version=version)
        except FileNotFoundError:
            return (
                False,
                None,
                404,
                f"Configuration for remote '{remote_name}' not found"
            )

        remote_url = remote_config.get("url")
        if not remote_url:
            return (
                False,
                None,
                500,
                f"No URL configured for remote '{remote_name}'"
            )

        # Check for custom route mapping
        # Build the full pattern including remote name for matching
        full_pattern = f"{remote_name}/{actual_path}"
        mapped_route = find_route_mapping(full_pattern, method, remote_config, remote_name)
        if mapped_route is not None:
            final_path = mapped_route
        else:
            final_path = actual_path

        # Construct full URL
        if final_path:
            # Optionally add trailing slash to avoid redirects from APIs that require it
            if self.settings.get("add_trailing_slash", True):
                path_with_slash = final_path if final_path.endswith('/') else final_path + '/'
                full_url = f"{remote_url.rstrip('/')}/{path_with_slash}"
            else:
                full_url = f"{remote_url.rstrip('/')}/{final_path}"
        else:
            full_url = remote_url.rstrip('/')

        # Forward the request
        # Configure redirect behavior based on settings
        # Note: httpx automatically follows redirects but blocks HTTPS->HTTP downgrades for security
        # The follow_protocol_downgrades setting is documented but may require manual redirect handling
        follow_redirects = True if self.settings.get("add_trailing_slash", True) else False

        async with httpx.AsyncClient(follow_redirects=follow_redirects) as client:
            try:
                # Forward request
                response = await client.request(
                    method=method,
                    url=full_url,
                    headers=headers or {},
                    content=body,
                    params=query_params or {}
                )

                # Parse response content
                try:
                    if response.headers.get("content-type", "").startswith("application/json"):
                        response_data = response.json()
                    else:
                        response_data = response.text
                except Exception:
                    response_data = response.text

                # Check if the remote API returned an error status
                if response.status_code >= 400:
                    # Treat HTTP errors from remote as failures with JSON error message
                    error_msg = f"Remote API returned {response.status_code}"
                    if isinstance(response_data, str) and len(response_data) < 200:
                        error_msg += f": {response_data}"
                    return (False, None, response.status_code, error_msg)

                return (True, response_data, response.status_code, None)

            except httpx.RequestError as e:
                return (False, None, 502, f"Error connecting to remote API: {str(e)}")
            except Exception as e:
                return (False, None, 500, f"Internal server error: {str(e)}")


    async def map_database_route(
            self,
            database_name: str,
            path: str) -> Tuple[bool, Any, int, Optional[str]]:
        """Execute a SQL query for a database route.

        Args:
            database_name: Name of the database.
            path: The path to match against database routes.

        Returns:
            Tuple of (success, response_data, status_code, error_message).
            If success is False, error_message contains the reason.
        """
        # Validate database exists
        if database_name not in self.database_names:
            return (False, None, 404, f"Database '{database_name}' not found")

        # Check if this is a versioned database
        is_versioned = is_versioned_database(database_name)

        # Parse version from path if database is versioned
        path_parts = path.split("/") if path else []
        version = None
        actual_path = path

        if is_versioned and path_parts:
            # First part should be the version for versioned databases
            potential_version = path_parts[0]
            available_versions = get_database_versions(database_name)

            if potential_version == "latest":
                # Resolve latest to actual version
                version = resolve_latest_database_version(available_versions)
                if version is None:
                    return (False, None, 404, f"No versions found for database '{database_name}'")
                actual_path = "/".join(path_parts[1:])
            elif potential_version in available_versions:
                version = potential_version
                actual_path = "/".join(path_parts[1:])
            elif not path:
                # Empty path on versioned database - list versions
                return (True, {"versions": available_versions}, 200, None)
            else:
                # Path provided but no valid version - error
                return (
                    False,
                    None,
                    404,
                    f"Configuration for database '{database_name}' not found"
                )
        elif is_versioned and not path:
            # Empty path on versioned database - list versions
            available_versions = get_database_versions(database_name)
            return (True, {"versions": available_versions}, 200, None)

        # Load database configuration
        try:
            database_config = load_database_config(database_name, version=version)
        except FileNotFoundError:
            return (
                False,
                None,
                404,
                f"Configuration for database '{database_name}' not found"
            )

        # Handle empty path - return list of available routes
        if not actual_path or actual_path == "":
            routes = database_config.get("routes", [])
            route_list = [r.get("route", "") for r in routes if isinstance(r, dict)]
            return (True, {"routes": route_list}, 200, None)

        # Find matching route in database config
        route_config = find_database_route(actual_path, database_config)
        if route_config is None:
            return (
                False,
                None,
                404,
                f"Route '{actual_path}' not found in database '{database_name}'"
            )

        # Extract path parameters
        route_pattern = route_config.get("route", "")
        path_params = extract_path_parameters(actual_path, route_pattern)

        # Build SQL query
        sql_template = route_config.get("sql", "")
        try:
            sql_query = build_sql_query(sql_template, database_config, path_params)
        except ValueError as e:
            return (False, None, 500, f"SQL query error: {str(e)}")

        # Execute SQL query using DuckDB
        try:
            import duckdb

            # Execute query and fetch results
            conn = duckdb.connect(database=':memory:')

            # Setup authentication for required storage backends
            # Detects backends from table URIs (s3://, gs://, azure://, http://, local)
            # and configures authentication using credential chains
            table_uris = extract_table_uris(database_config)
            required_backends = detect_required_backends(table_uris)

            # Extract table metadata (region, auth headers, etc.) grouped by backend
            backend_metadata = extract_table_metadata_by_backend(database_config)

            # Setup authentication for each backend
            # This automatically discovers credentials from:
            # - S3: AWS env vars, config files, IAM roles, SSO (+ region from config or env)
            # - GCS: GCS env vars, service account files, HMAC keys
            # - Azure: Azure env vars, managed identity, CLI credentials
            # - HTTP/HTTPS: httpfs extension (+ custom headers/bearer token from config)
            #
            # Metadata from config takes precedence over environment variables
            # Note: Authentication setup failures are graceful - public files will still work
            auth_results = setup_storage_authentication(conn, required_backends, backend_metadata)

            result = conn.execute(sql_query).fetchall()
            columns = [desc[0] for desc in conn.description] if conn.description else []
            conn.close()

            # Convert to list of dictionaries with JSON-safe values
            response_data = []
            for row in result:
                row_dict = {}
                for col, val in zip(columns, row):
                    # Convert non-JSON-serializable types to strings
                    row_dict[col] = _make_json_safe(val)
                response_data.append(row_dict)

            return (True, response_data, 200, None)

        except Exception as e:
            return (False, None, 500, f"Database query error: {str(e)}")


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
                      query_params: Optional[Dict[str, str]] = None) -> Tuple[bool, Any, int, Optional[str]]:
        """Synchronous version of map_route for frameworks that don't support async.

        Args:
            remote_name: Name of the remote API.
            path: The path to proxy to the remote API.
            method: HTTP method (GET, POST, etc.).
            headers: Request headers dictionary.
            body: Request body bytes.
            query_params: Query parameters dictionary.

        Returns:
            Tuple of (success, response_data, status_code, error_message).
            If success is False, error_message contains the reason.
        """
        import asyncio

        # Run the async version in a new event loop
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            result = loop.run_until_complete(
                self.map_route(remote_name, path, method, headers, body, query_params)
            )
            loop.close()
            return result
        except Exception as e:
            return (False, None, 500, f"Sync wrapper error: {str(e)}")


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
        # Find the remote name that corresponds to this filename
        for remote_name, config_path in mapping.items():
            if config_path and filename in config_path:
                return remote_name
        return None


#
# INTERNAL
#
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
        # Convert datetime/date to ISO format string
        return value.isoformat()
    elif isinstance(value, Decimal):
        # Convert Decimal to float
        return float(value)
    elif isinstance(value, bytes):
        # Convert bytes to base64 string
        import base64
        return base64.b64encode(value).decode('utf-8')
    else:
        # Return as-is for JSON-safe types (str, int, float, bool, list, dict)
        return value
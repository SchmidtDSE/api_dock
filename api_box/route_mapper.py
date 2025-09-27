"""

Route Mapper Module for API Box

Standalone route mapping functionality that can be integrated into any web framework.

License: CC-BY-4.0

"""

#
# IMPORTS
#
import httpx
from typing import Any, Dict, List, Optional, Tuple

from api_box.config import find_remote_config, get_remote_names, is_route_allowed, load_main_config


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
            self.config = {"name": "api-box", "description": "API Box wrapper", "authors": []}

        self.remote_names = get_remote_names(self.config)


    def get_config_metadata(self) -> Dict[str, Any]:
        """Get API metadata from configuration.

        Returns:
            Dictionary containing name, description, and authors.
        """
        description_keys = ["name", "description", "authors"]
        return {k: self.config.get(k, None) for k in description_keys}


    def get_config_value(self, key: str) -> Tuple[bool, Any, Optional[str]]:
        """Get a configuration value by key.

        Args:
            key: Configuration key to retrieve.

        Returns:
            Tuple of (success, value, error_message).
            If success is False, error_message contains the reason.
        """
        # Check if key is a remote name (remotes take precedence)
        if key in self.remote_names:
            return (
                False,
                None,
                f"'{key}' is a remote name. Use /{key}/latest/ for remote API access."
            )

        # Check if key might be a remote filename that maps to a different name
        if self._is_remote_filename(key):
            actual_name = self._get_remote_name_by_filename(key)
            return (
                False,
                None,
                f"'{key}' refers to remote '{actual_name}'. Use /{actual_name}/latest/ for remote API access."
            )

        if key not in self.config:
            return (False, None, f"Configuration key '{key}' not found")

        return (True, self.config[key], None)


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

        # Parse version from path (optional)
        path_parts = path.split("/") if path else []
        version = DEFAULT_VERSION
        actual_path = path

        # Check if first part of path is a version
        if path_parts and (path_parts[0] == "latest" or path_parts[0].isdigit()):
            version = path_parts[0]
            actual_path = "/".join(path_parts[1:])

        # Check if route is allowed
        if not is_route_allowed(actual_path, self.config, remote_name):
            return (
                False,
                None,
                403,
                f"Route '{actual_path}' not allowed for remote '{remote_name}'"
            )

        # Load remote configuration
        try:
            remote_config = find_remote_config(remote_name, self.config)
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

        # Construct full URL
        if actual_path:
            full_url = f"{remote_url.rstrip('/')}/{actual_path}"
        else:
            full_url = remote_url.rstrip('/')

        # Forward the request
        async with httpx.AsyncClient() as client:
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

                return (True, response_data, response.status_code, None)

            except httpx.RequestError as e:
                return (False, None, 502, f"Error connecting to remote API: {str(e)}")
            except Exception as e:
                return (False, None, 500, f"Internal server error: {str(e)}")


    def is_remote_name(self, name: str) -> bool:
        """Check if a given name is a configured remote name.

        Args:
            name: The name to check.

        Returns:
            True if name is a remote name, False otherwise.
        """
        return name in self.remote_names


    def get_remote_names(self) -> List[str]:
        """Get list of configured remote names.

        Returns:
            List of remote names.
        """
        return self.remote_names.copy()


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
        from api_box.config import get_remote_mapping

        mapping = get_remote_mapping(self.config)
        # Find the remote name that corresponds to this filename
        for remote_name, config_path in mapping.items():
            if config_path and filename in config_path:
                return remote_name
        return None


#
# INTERNAL
#
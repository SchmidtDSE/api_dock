"""

Main FastAPI Application for API Box

Core FastAPI application that handles routing to remote APIs and serves config data.

License: CC-BY-4.0

"""

#
# IMPORTS
#
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from typing import Any, Dict, Optional

from .config import find_remote_config, get_remote_names, is_route_allowed, load_main_config


#
# CONSTANTS
#
DESCRIPTION_KEYS: list[str] = ["name", "description", "authors"]
DEFAULT_VERSION: str = "latest"


#
# PUBLIC
#
def create_app(config_path: Optional[str] = None) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        config_path: Path to main config file. If None, uses default.

    Returns:
        Configured FastAPI application.
    """
    # Load main configuration
    try:
        config = load_main_config(config_path)
    except (FileNotFoundError, Exception) as e:
        # For first pass, keep error handling simple
        config = {"name": "api-box", "description": "API Box wrapper", "authors": []}

    app = FastAPI(
        title=config.get("name", "API Box"),
        description=config.get("description", "API wrapper using configuration files"),
        version="0.1.0"
    )

    # Store config in app state
    app.state.config = config

    # Add routes
    _add_main_routes(app, config)
    _add_remote_routes(app, config)

    return app


#
# INTERNAL
#
def _add_main_routes(app: FastAPI, config: Dict[str, Any]) -> None:
    """Add main API routes to the FastAPI app.

    Args:
        app: FastAPI application instance.
        config: Main configuration dictionary.
    """

    @app.get("/")
    async def get_meta() -> Dict[str, Any]:
        """Return metadata from main config."""
        return {k: config.get(k, None) for k in DESCRIPTION_KEYS}

    @app.get("/{key}")
    async def get_config_value(key: str) -> Any:
        """Get a top-level config value by key.

        Args:
            key: Configuration key to retrieve.

        Returns:
            Configuration value.

        Raises:
            HTTPException: If key not found or conflicts with remote name.
        """
        # Check if key is a remote name (remotes take precedence)
        remote_names = get_remote_names(config)
        if key in remote_names:
            raise HTTPException(
                status_code=400,
                detail=f"'{key}' is a remote name. Use /{key}/latest/ for remote API access."
            )

        if key not in config:
            raise HTTPException(status_code=404, detail=f"Configuration key '{key}' not found")

        return config[key]


def _add_remote_routes(app: FastAPI, config: Dict[str, Any]) -> None:
    """Add remote API proxy routes to the FastAPI app.

    Args:
        app: FastAPI application instance.
        config: Main configuration dictionary.
    """
    remote_names = get_remote_names(config)

    @app.api_route("/{remote_name}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def proxy_to_remote(remote_name: str, path: str, request: Request) -> JSONResponse:
        """Proxy requests to remote APIs.

        Args:
            remote_name: Name of the remote API.
            path: The path to proxy to the remote API.
            request: The incoming request.

        Returns:
            Response from the remote API.

        Raises:
            HTTPException: If remote not found or route not allowed.
        """
        if remote_name not in remote_names:
            raise HTTPException(status_code=404, detail=f"Remote '{remote_name}' not found")

        # Parse version from path (optional)
        path_parts = path.split("/")
        version = DEFAULT_VERSION
        actual_path = path

        # Check if first part of path is a version
        if path_parts and (path_parts[0] == "latest" or path_parts[0].isdigit()):
            version = path_parts[0]
            actual_path = "/".join(path_parts[1:])

        # Check if route is allowed
        if not is_route_allowed(actual_path, config, remote_name):
            raise HTTPException(status_code=403, detail=f"Route '{actual_path}' not allowed for remote '{remote_name}'")

        # Load remote configuration
        try:
            remote_config = find_remote_config(remote_name, config)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Configuration for remote '{remote_name}' not found")

        remote_url = remote_config.get("url")
        if not remote_url:
            raise HTTPException(status_code=500, detail=f"No URL configured for remote '{remote_name}'")

        # Construct full URL
        if actual_path:
            full_url = f"{remote_url.rstrip('/')}/{actual_path}"
        else:
            full_url = remote_url.rstrip('/')

        # Forward the request
        async with httpx.AsyncClient() as client:
            try:
                # Get request body if present
                body = None
                if request.method in ["POST", "PUT", "PATCH"]:
                    body = await request.body()

                # Forward request
                response = await client.request(
                    method=request.method,
                    url=full_url,
                    headers=dict(request.headers),
                    content=body,
                    params=dict(request.query_params)
                )

                # Return response
                return JSONResponse(
                    content=response.json() if response.headers.get("content-type", "").startswith("application/json") else response.text,
                    status_code=response.status_code
                )

            except httpx.RequestError as e:
                raise HTTPException(status_code=502, detail=f"Error connecting to remote API: {str(e)}")
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


# Default app instance
app = create_app()
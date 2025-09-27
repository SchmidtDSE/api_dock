"""

Main FastAPI Application for API Box

Core FastAPI application that handles routing to remote APIs and serves config data.

License: CC-BY-4.0

"""

#
# IMPORTS
#
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from typing import Any, Dict, Optional

from api_box.route_mapper import RouteMapper


#
# CONSTANTS
#


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
    # Initialize route mapper
    route_mapper = RouteMapper(config_path)

    # Get metadata for FastAPI
    metadata = route_mapper.get_config_metadata()

    app = FastAPI(
        title=metadata.get("name", "API Box"),
        description=metadata.get("description", "API wrapper using configuration files"),
        version="0.1.0"
    )

    # Store route mapper in app state
    app.state.route_mapper = route_mapper

    # Add routes
    _add_main_routes(app, route_mapper)
    _add_remote_routes(app, route_mapper)

    # Add error handlers for JSON responses
    _add_error_handlers(app)

    return app


#
# INTERNAL
#
def _add_main_routes(app: FastAPI, route_mapper: RouteMapper) -> None:
    """Add main API routes to the FastAPI app.

    Args:
        app: FastAPI application instance.
        route_mapper: RouteMapper instance.
    """

    @app.get("/")
    async def get_meta() -> Dict[str, Any]:
        """Return metadata from main config."""
        return route_mapper.get_config_metadata()

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
        success, value, error_message = route_mapper.get_config_value(key)

        if not success:
            if "is a remote name" in error_message:
                raise HTTPException(status_code=400, detail=error_message)
            else:
                raise HTTPException(status_code=404, detail=error_message)

        return value


def _add_remote_routes(app: FastAPI, route_mapper: RouteMapper) -> None:
    """Add remote API proxy routes to the FastAPI app.

    Args:
        app: FastAPI application instance.
        route_mapper: RouteMapper instance.
    """

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
        # Get request body if present
        body = None
        if request.method in ["POST", "PUT", "PATCH"]:
            body = await request.body()

        # Use RouteMapper to handle the request
        success, response_data, status_code, error_message = await route_mapper.map_route(
            remote_name=remote_name,
            path=path,
            method=request.method,
            headers=dict(request.headers),
            body=body,
            query_params=dict(request.query_params)
        )

        if not success:
            raise HTTPException(status_code=status_code, detail=error_message)

        return JSONResponse(content=response_data, status_code=status_code)


def _add_error_handlers(app: FastAPI) -> None:
    """Add custom error handlers to return JSON responses.

    Args:
        app: FastAPI application instance.
    """
    from fastapi import Request
    from fastapi.responses import JSONResponse

    @app.exception_handler(404)
    async def not_found_handler(request: Request, exc):
        """Return JSON response for 404 errors."""
        return JSONResponse(content={"error": "Not found"}, status_code=404)

    @app.exception_handler(405)
    async def method_not_allowed_handler(request: Request, exc):
        """Return JSON response for 405 errors."""
        return JSONResponse(content={"error": "Method not allowed"}, status_code=405)

    @app.exception_handler(500)
    async def internal_error_handler(request: Request, exc):
        """Return JSON response for 500 errors."""
        return JSONResponse(content={"error": "Internal server error"}, status_code=500)


# Default app instance
app = create_app()
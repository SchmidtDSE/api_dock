"""

Main FastAPI Application for API Dock

Core FastAPI application that handles routing to remote APIs and serves config data.

License: BSD 3-Clause

"""

#
# IMPORTS
#
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from typing import Any, Dict, Optional

from api_dock.route_mapper import RouteMapper


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
    route_mapper = RouteMapper(config_path)

    metadata = route_mapper.get_config_metadata()

    app = FastAPI(
        title=metadata.get("name", "API Dock"),
        description=metadata.get("description", "API wrapper using configuration files"),
        version="0.1.0"
    )

    app.state.route_mapper = route_mapper

    _add_main_routes(app, route_mapper)
    _add_remote_routes(app, route_mapper)
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


def _add_remote_routes(app: FastAPI, route_mapper: RouteMapper) -> None:
    """Add remote API proxy routes to the FastAPI app.

    Args:
        app: FastAPI application instance.
        route_mapper: RouteMapper instance.
    """

    @app.api_route("/{remote_name}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def proxy_to_remote(remote_name: str, path: str, request: Request) -> Response:
        """Proxy requests to remote APIs or databases.

        Upstream responses — including binary content, 3xx redirects, and
        4xx/5xx errors — are returned verbatim with their original status
        code, content type, and headers. Cache-Control, ETag, Location, and
        other upstream headers are forwarded to the client.

        Args:
            remote_name: Name of the remote API or database.
            path: The path to proxy to the remote API or query from database.
            request: The incoming request.

        Returns:
            Response from the upstream with original status, headers, and body.
        """
        cookies = dict(request.cookies) if request.cookies else {}

        if remote_name in route_mapper.database_names:
            proxy_resp = await route_mapper.map_database_route(
                database_name=remote_name,
                path=path,
                query_params=dict(request.query_params),
                cookies=cookies,
            )
        else:
            body = None
            if request.method in ["POST", "PUT", "PATCH"]:
                body = await request.body()

            proxy_resp = await route_mapper.map_route(
                remote_name=remote_name,
                path=path,
                method=request.method,
                headers=dict(request.headers),
                body=body,
                query_params=dict(request.query_params),
                cookies=cookies,
            )

        return Response(
            content=proxy_resp.content,
            status_code=proxy_resp.status_code,
            headers=proxy_resp.headers,
            media_type=proxy_resp.content_type,
        )


def _add_error_handlers(app: FastAPI) -> None:
    """Add custom error handlers to return JSON responses.

    Args:
        app: FastAPI application instance.
    """

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

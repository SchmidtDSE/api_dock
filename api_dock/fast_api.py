"""

Main FastAPI Application for API Dock

Core FastAPI application that handles routing to remote APIs and serves config data.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import asyncio
import json
import warnings
from contextlib import asynccontextmanager
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from typing import Any, AsyncIterator, Callable, Dict, Optional

from api_dock.route_mapper import (
    collect_multi_query_params,
    HOP_BY_HOP_HEADERS,
    RouteMapper,
    strip_base_path,
)
from api_dock.types import PreparedRequest, ProxyResponse


#
# CONSTANTS
#
# The default app, built on first access to ``app`` (see __getattr__).
_DEFAULT_APP: Any = None

# Headers excluded when forwarding a streaming (raw-byte) upstream response.
# Derived from route_mapper.HOP_BY_HOP_HEADERS but keeps content-encoding:
# aiter_raw() yields the upstream's compressed bytes unchanged, so the
# Content-Encoding header still describes the body correctly and must reach the
# client so it can decompress. (The buffered map_route() path strips it instead,
# because httpx decompresses there and the header would otherwise be wrong.)
_STREAMING_EXCLUDED_HEADERS: frozenset = HOP_BY_HOP_HEADERS - frozenset({"content-encoding"})


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

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Open PostgreSQL connection pools at startup and close them at shutdown."""
        await route_mapper.start()
        try:
            yield
        finally:
            await route_mapper.aclose()

    app = FastAPI(
        title=metadata.get("name", "API Dock"),
        description=metadata.get("description", "API wrapper using configuration files"),
        version="0.1.0",
        lifespan=lifespan,
    )

    app.state.route_mapper = route_mapper

    for message in route_mapper.listing_warnings:
        warnings.warn(message, stacklevel=2)

    _add_main_routes(app, route_mapper)
    _add_lookup_routes(app, route_mapper)
    _add_listing_routes(app, route_mapper)
    _add_remote_routes(app, route_mapper)
    _add_error_handlers(app)

    if route_mapper.base_path:
        app.add_middleware(_StripBasePath, base_path=route_mapper.base_path)

    return app


def __getattr__(name: str) -> Any:
    """Build the default ``app`` on first use instead of at import (PEP 562).

    ``from api_dock.fast_api import app`` and server import strings
    such as ``api_dock.fast_api:app`` still work. Building the app
    at import time ran the startup config check on whatever config sits in the
    current directory, so any import of api_dock (even ``api-dock --help``)
    failed when that config was invalid.

    Args:
        name: The attribute being looked up.

    Returns:
        The default app, built from the default config on first access.

    Raises:
        AttributeError: For any other missing attribute.
    """
    if name == "app":
        return _default_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


#
# INTERNAL
#
class _StripBasePath:
    """ASGI middleware serving the app under ``settings.base_path`` as well.

    Requests whose path starts with the base path (e.g. ``/dock/birdnet/...``)
    are routed as if it weren't there; other paths pass through unchanged.
    """

    def __init__(self, app: Callable, base_path: str) -> None:
        self.app = app
        self.base_path = base_path

    async def __call__(self, scope: Dict[str, Any], receive: Callable, send: Callable) -> None:
        """Rewrite the request path, then call the wrapped app."""
        if scope.get("type") in ("http", "websocket"):
            path = strip_base_path(scope.get("path", ""), self.base_path)
            if path != scope.get("path"):
                scope = {**scope, "path": path, "raw_path": path.encode()}
        await self.app(scope, receive, send)



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


def _add_lookup_routes(app: FastAPI, route_mapper: RouteMapper) -> None:
    """Add the ``settings.lookups.refresh_route`` endpoint, if configured.

    Args:
        app: FastAPI application instance.
        route_mapper: RouteMapper instance.
    """
    endpoint = route_mapper.lookup_endpoint
    if endpoint is None:
        return

    @app.api_route(f"/{endpoint.route}", methods=["GET", "POST"])
    async def lookups(request: Request) -> Response:
        """GET: lookup status. POST: refresh lookups (all, or ?name=...)."""
        result = await asyncio.to_thread(
            route_mapper.lookup_endpoint_response,
            request.method,
            request.headers.get("authorization"),
            request.query_params.getlist("name"),
        )
        return Response(
            content=result.content, status_code=result.status_code,
            media_type=result.content_type,
        )


def _add_listing_routes(app: FastAPI, route_mapper: RouteMapper) -> None:
    """Add optional catalog-listing routes to the FastAPI app.

    Registered before the remote proxy catch-all so custom multi-segment routes
    (e.g. /list/databases) win over /{remote_name}/{path}.

    Args:
        app: FastAPI application instance.
        route_mapper: RouteMapper instance.
    """

    def _make_endpoint(spec: Any):
        async def _listing() -> Response:
            """Return the models/versions for this listing."""
            return JSONResponse(content=route_mapper.get_listing(spec))
        return _listing

    for spec in route_mapper.listing_specs:
        endpoint = _make_endpoint(spec)
        base = spec.route.rstrip("/")
        # Register both /route and /route/ so the listing works regardless of the
        # client's trailing-slash convention. Without the slash alias the proxy
        # catch-all (/{remote_name}/{path}) would swallow the trailing-slash form.
        # The alias is hidden from the OpenAPI schema to keep the docs clean.
        app.add_api_route(f"/{base}", endpoint, methods=["GET"])
        app.add_api_route(f"/{base}/", endpoint, methods=["GET"], include_in_schema=False)


def _add_remote_routes(app: FastAPI, route_mapper: RouteMapper) -> None:
    """Add remote API proxy routes to the FastAPI app.

    Args:
        app: FastAPI application instance.
        route_mapper: RouteMapper instance.
    """

    @app.api_route("/{remote_name}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def proxy_to_remote(remote_name: str, path: str, request: Request) -> Response:
        """Proxy requests to remote APIs or databases.

        Database routes are handled with a buffered response. Remote API routes
        are streamed: upstream bytes are piped to the client as they arrive via
        StreamingResponse, avoiding buffering large bodies in memory. Raw bytes
        are forwarded via aiter_raw() so any upstream content-encoding (gzip,
        br, deflate) is preserved end-to-end with the correct Content-Encoding
        header.

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
                multi_query_params=collect_multi_query_params(
                    request.query_params.multi_items()
                ),
            )
            return Response(
                content=proxy_resp.content,
                status_code=proxy_resp.status_code,
                headers=proxy_resp.headers,
                media_type=proxy_resp.content_type,
            )

        body = None
        if request.method in ["POST", "PUT", "PATCH"]:
            body = await request.body()

        prepared = await route_mapper.prepare_remote_request(
            remote_name=remote_name,
            path=path,
            method=request.method,
            headers=dict(request.headers),
            body=body,
            query_params=dict(request.query_params),
            cookies=cookies,
            multi_query_params=collect_multi_query_params(
                request.query_params.multi_items()
            ),
        )

        if isinstance(prepared, ProxyResponse):
            return Response(
                content=prepared.content,
                status_code=prepared.status_code,
                headers=prepared.headers,
                media_type=prepared.content_type,
            )

        return await _stream_upstream(prepared)


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


def _filter_streaming_response_headers(headers: Dict[str, str]) -> Dict[str, str]:
    """Strip headers that must not be forwarded in a streaming response.

    Like route_mapper._filter_response_headers but preserves content-encoding.
    When streaming raw bytes via aiter_raw(), compressed bytes pass through
    unchanged, so Content-Encoding correctly describes the response body.

    Args:
        headers: Raw headers from the upstream httpx response.

    Returns:
        Filtered dict containing only headers safe to forward.
    """
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in _STREAMING_EXCLUDED_HEADERS
    }


async def _stream_upstream(prepared: PreparedRequest) -> Response:
    """Stream a prepared upstream request back to the client.

    Opens an httpx streaming connection, reads response status and headers,
    then returns a StreamingResponse that pipes raw bytes to the client as
    they arrive. Uses aiter_raw() so compressed content (gzip, br, deflate)
    passes through unchanged with its Content-Encoding header intact.

    If the upstream connection itself fails (before any bytes are received),
    a plain 502 or 500 Response is returned instead.

    Args:
        prepared: Resolved upstream request from RouteMapper.prepare_remote_request().

    Returns:
        StreamingResponse on successful upstream connection,
        or plain Response on 502/500 if the connection cannot be established.
    """
    client = httpx.AsyncClient(
        follow_redirects=prepared.follow_redirects, timeout=prepared.timeout
    )
    req = client.build_request(
        method=prepared.method,
        url=prepared.url,
        headers=prepared.headers,
        params=prepared.params,
        content=prepared.body,
    )
    try:
        upstream = await client.send(req, stream=True)
    except httpx.RequestError as exc:
        await client.aclose()
        return Response(
            content=json.dumps({"error": f"Error connecting to remote API: {str(exc)}"}).encode(),
            status_code=502,
            media_type="application/json",
        )
    except Exception as exc:
        await client.aclose()
        return Response(
            content=json.dumps({"error": f"Internal server error: {str(exc)}"}).encode(),
            status_code=500,
            media_type="application/json",
        )

    async def _generate():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    headers = _filter_streaming_response_headers(dict(upstream.headers))
    return StreamingResponse(
        _generate(),
        status_code=upstream.status_code,
        headers=headers,
        media_type=upstream.headers.get("content-type", "application/octet-stream"),
    )


def _default_app() -> Any:
    """Build the default FastAPI app once, from the default config.

    Returns:
        The cached app.
    """
    global _DEFAULT_APP
    if _DEFAULT_APP is None:
        _DEFAULT_APP = create_app()
    return _DEFAULT_APP

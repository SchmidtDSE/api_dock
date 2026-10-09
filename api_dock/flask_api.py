"""

Flask Application for API Dock

Flask-based application that handles routing to remote APIs and serves config data.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import asyncio
import warnings
from flask import Flask, jsonify, request, Response as FlaskResponse
from typing import Any, Callable, Dict, Optional

from api_dock.route_mapper import collect_multi_query_params, RouteMapper, strip_base_path


#
# CONSTANTS
#
# The default app, built on first access to ``app`` (see __getattr__).
_DEFAULT_APP: Any = None


#
# PUBLIC
#
def create_app(config_path: Optional[str] = None) -> Flask:
    """Create and configure the Flask application.

    Args:
        config_path: Path to main config file. If None, uses default.

    Returns:
        Configured Flask application.

    Raises:
        ValueError: If PostgreSQL connections are configured; they need the
            FastAPI server, whose single event loop can share connection pools.
    """
    route_mapper = RouteMapper(config_path)
    if route_mapper.connections:
        raise ValueError(
            "PostgreSQL connections (database.connections) need the FastAPI server; "
            "start api-dock without --backbone flask"
        )

    app = Flask(__name__)

    app.url_map.strict_slashes = False

    app.config['route_mapper'] = route_mapper

    for message in route_mapper.listing_warnings:
        warnings.warn(message, stacklevel=2)

    _add_lookup_routes(app, route_mapper)
    _add_listing_routes(app, route_mapper)
    _add_remote_routes(app, route_mapper)
    _add_main_routes(app, route_mapper)
    _add_error_handlers(app)

    if route_mapper.base_path:
        app.wsgi_app = _strip_base_path(app.wsgi_app, route_mapper.base_path)

    return app


def __getattr__(name: str) -> Any:
    """Build the default ``app`` on first use instead of at import (PEP 562).

    ``from api_dock.flask_api import app`` and server import strings
    such as ``api_dock.flask_api:app`` still work. Building the app
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
def _strip_base_path(wsgi_app: Callable, base_path: str) -> Callable:
    """Wrap a WSGI app so it is also served under ``settings.base_path``.

    Args:
        wsgi_app: The Flask WSGI app.
        base_path: Normalized prefix, e.g. "/dock".

    Returns:
        WSGI app that routes ``/dock/...`` as ``/...`` (other paths unchanged).
    """
    def middleware(environ: Dict[str, Any], start_response: Callable) -> Any:
        """Strip the base path from PATH_INFO, then call the Flask app."""
        path = environ.get("PATH_INFO", "")
        stripped = strip_base_path(path, base_path)
        if stripped != path:
            environ["PATH_INFO"] = stripped
        return wsgi_app(environ, start_response)
    return middleware


def _add_lookup_routes(app: Flask, route_mapper: RouteMapper) -> None:
    """Refresh due lookups in the background, and add the refresh endpoint if configured.

    Flask has no lifespan to run a refresh loop in, so each request checks
    whether a lookup is due and, if so, refreshes it in a thread.

    Args:
        app: Flask application instance.
        route_mapper: RouteMapper instance.
    """

    @app.before_request
    def refresh_due_lookups() -> None:
        route_mapper.refresh_due_lookups_in_background()

    endpoint = route_mapper.lookup_endpoint
    if endpoint is None:
        return

    @app.route(f"/{endpoint.route}", methods=["GET", "POST"], endpoint="api_dock_lookups")
    def lookups() -> FlaskResponse:
        """GET: lookup status. POST: refresh lookups (all, or ?name=...)."""
        result = route_mapper.lookup_endpoint_response(
            request.method, request.headers.get("Authorization"), request.args.getlist("name")
        )
        return FlaskResponse(
            result.content, status=result.status_code, content_type=result.content_type
        )


def _add_main_routes(app: Flask, route_mapper: RouteMapper) -> None:
    """Add main API routes to the Flask app.

    Args:
        app: Flask application instance.
        route_mapper: RouteMapper instance.
    """

    @app.route("/")
    def get_meta() -> Dict[str, Any]:
        """Return metadata from main config."""
        return jsonify(route_mapper.get_config_metadata())


def _add_listing_routes(app: Flask, route_mapper: RouteMapper) -> None:
    """Add optional catalog-listing routes to the Flask app.

    Flask prefers static rules over the dynamic proxy rule, so custom
    multi-segment routes (e.g. /list/databases) win over /<remote_name>/<path>.

    Args:
        app: Flask application instance.
        route_mapper: RouteMapper instance.
    """

    def _make_view(spec: Any):
        def _listing():
            """Return the models/versions for this listing."""
            return jsonify(route_mapper.get_listing(spec))
        return _listing

    for index, spec in enumerate(route_mapper.listing_specs):
        app.add_url_rule(
            f"/{spec.route}",
            endpoint=f"listing_{index}_{spec.kind}",
            view_func=_make_view(spec),
            methods=["GET"],
        )


def _add_remote_routes(app: Flask, route_mapper: RouteMapper) -> None:
    """Add remote API proxy routes to the Flask app.

    Args:
        app: Flask application instance.
        route_mapper: RouteMapper instance.
    """

    @app.route("/<path:full_path>", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    def proxy_to_remote(full_path: str):
        """Proxy requests to remote APIs or databases.

        The served name is the longest one the path starts with (names may
        contain "/"); the rest of the path is the version and route.

        Args:
            full_path: The request path after the leading "/".

        Returns:
            Response from the upstream with original status, headers, and body.
        """
        # Flask drops a trailing slash from <path:...>; keep it for routes that end in "/".
        if request.path.endswith("/") and not full_path.endswith("/"):
            full_path += "/"
        match = route_mapper.split_name(full_path)
        if match is None:
            first = full_path.split("/", 1)[0]
            return jsonify({"error": f"Remote '{first}' not found"}), 404
        return _handle_proxy(route_mapper, *match)


def _handle_proxy(route_mapper: RouteMapper, remote_name: str, path: str) -> FlaskResponse:
    """Shared proxy logic for both Flask route handlers.

    Upstream responses — including binary content, 3xx redirects, and
    4xx/5xx errors — are returned verbatim with their original status
    code, content type, and headers.

    Args:
        route_mapper: RouteMapper instance.
        remote_name: Name of the remote API or database.
        path: The path to proxy.

    Returns:
        Flask Response with upstream status, headers, and body.
    """
    cookies = dict(request.cookies) if request.cookies else {}

    if remote_name in route_mapper.database_names:
        proxy_resp = asyncio.run(
            route_mapper.map_database_route(
                database_name=remote_name,
                path=path,
                query_params=dict(request.args),
                cookies=cookies,
                multi_query_params=collect_multi_query_params(
                    request.args.items(multi=True)
                ),
            )
        )
    else:
        body = None
        if request.method in ["POST", "PUT", "PATCH"]:
            body = request.get_data()

        proxy_resp = route_mapper.map_route_sync(
            remote_name=remote_name,
            path=path,
            method=request.method,
            headers=dict(request.headers),
            body=body,
            query_params=dict(request.args),
            cookies=cookies,
            multi_query_params=collect_multi_query_params(
                request.args.items(multi=True)
            ),
        )

    response = FlaskResponse(
        response=proxy_resp.content,
        status=proxy_resp.status_code,
        content_type=proxy_resp.content_type,
    )
    for key, value in proxy_resp.headers.items():
        response.headers[key] = value
    for cookie in proxy_resp.set_cookies:
        response.headers.add("Set-Cookie", cookie)

    return response


def _add_error_handlers(app: Flask) -> None:
    """Add custom error handlers to return JSON responses.

    Args:
        app: Flask application instance.
    """

    @app.errorhandler(404)
    def not_found(error):
        """Return JSON response for 404 errors."""
        return jsonify({"error": "Not found"}), 404

    @app.errorhandler(405)
    def method_not_allowed(error):
        """Return JSON response for 405 errors."""
        return jsonify({"error": "Method not allowed"}), 405

    @app.errorhandler(500)
    def internal_error(error):
        """Return JSON response for 500 errors."""
        return jsonify({"error": "Internal server error"}), 500


def _default_app() -> Any:
    """Build the default Flask app once, from the default config.

    Returns:
        The cached app.
    """
    global _DEFAULT_APP
    if _DEFAULT_APP is None:
        _DEFAULT_APP = create_app()
    return _DEFAULT_APP

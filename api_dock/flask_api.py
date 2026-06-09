"""

Flask Application for API Dock

Flask-based application that handles routing to remote APIs and serves config data.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import asyncio
from flask import Flask, jsonify, request, Response as FlaskResponse
from typing import Any, Dict, Optional

from api_dock.route_mapper import RouteMapper


#
# CONSTANTS
#


#
# PUBLIC
#
def create_app(config_path: Optional[str] = None) -> Flask:
    """Create and configure the Flask application.

    Args:
        config_path: Path to main config file. If None, uses default.

    Returns:
        Configured Flask application.
    """
    route_mapper = RouteMapper(config_path)

    app = Flask(__name__)

    app.url_map.strict_slashes = False

    app.config['route_mapper'] = route_mapper

    _add_remote_routes(app, route_mapper)
    _add_main_routes(app, route_mapper)
    _add_error_handlers(app)

    return app


#
# INTERNAL
#
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


def _add_remote_routes(app: Flask, route_mapper: RouteMapper) -> None:
    """Add remote API proxy routes to the Flask app.

    Args:
        app: Flask application instance.
        route_mapper: RouteMapper instance.
    """

    @app.route("/<remote_name>/", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    def proxy_to_remote_root(remote_name: str):
        """Proxy requests to remote APIs or databases (root path).

        Args:
            remote_name: Name of the remote API or database.

        Returns:
            Response from the upstream with original status, headers, and body.
        """
        return _handle_proxy(route_mapper, remote_name, "")


    @app.route("/<remote_name>/<path:path>", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    def proxy_to_remote(remote_name: str, path: str):
        """Proxy requests to remote APIs or databases.

        Args:
            remote_name: Name of the remote API or database.
            path: The path to proxy to the remote API or query from database.

        Returns:
            Response from the upstream with original status, headers, and body.
        """
        return _handle_proxy(route_mapper, remote_name, path)


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
        )

    response = FlaskResponse(
        response=proxy_resp.content,
        status=proxy_resp.status_code,
        content_type=proxy_resp.content_type,
    )
    for key, value in proxy_resp.headers.items():
        response.headers[key] = value

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


# Default app instance
app = create_app()

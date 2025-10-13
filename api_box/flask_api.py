"""

Flask Application for API Box

Flask-based application that handles routing to remote APIs and serves config data.

License: BSD 3-Clause

"""

#
# IMPORTS
#
from flask import Flask, jsonify, request
from typing import Any, Dict, Optional

from api_box.route_mapper import RouteMapper


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
    # Initialize route mapper
    route_mapper = RouteMapper(config_path)

    # Get metadata for Flask
    metadata = route_mapper.get_config_metadata()

    app = Flask(__name__)

    # Disable automatic redirects for trailing slashes to avoid HTML responses
    app.url_map.strict_slashes = False

    # Store route mapper in app config
    app.config['route_mapper'] = route_mapper

    # Add routes (remote routes first to avoid conflicts)
    _add_remote_routes(app, route_mapper)
    _add_main_routes(app, route_mapper)

    # Add error handlers for JSON responses
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
            Response from the remote API/database or error response.
        """
        # Check if remote_name is a database first
        if remote_name in route_mapper.database_names:
            # Handle as database route (using async with asyncio.run)
            import asyncio
            success, response_data, status_code, error_message = asyncio.run(
                route_mapper.map_database_route(
                    database_name=remote_name,
                    path=""
                )
            )
        else:
            # Handle as remote API route
            # Get request body if present
            body = None
            if request.method in ["POST", "PUT", "PATCH"]:
                body = request.get_data()

            # Use RouteMapper to handle the request (synchronous version)
            success, response_data, status_code, error_message = route_mapper.map_route_sync(
                remote_name=remote_name,
                path="",
                method=request.method,
                headers=dict(request.headers),
                body=body,
                query_params=dict(request.args)
            )

        if not success:
            return jsonify({"error": error_message}), status_code

        return jsonify(response_data), status_code


    @app.route("/<remote_name>/<path:path>", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    def proxy_to_remote(remote_name: str, path: str):
        """Proxy requests to remote APIs or databases.

        Args:
            remote_name: Name of the remote API or database.
            path: The path to proxy to the remote API or query from database.

        Returns:
            Response from the remote API/database or error response.
        """
        # Check if remote_name is a database first
        if remote_name in route_mapper.database_names:
            # Handle as database route (using async with asyncio.run)
            import asyncio
            success, response_data, status_code, error_message = asyncio.run(
                route_mapper.map_database_route(
                    database_name=remote_name,
                    path=path
                )
            )
        else:
            # Handle as remote API route
            # Get request body if present
            body = None
            if request.method in ["POST", "PUT", "PATCH"]:
                body = request.get_data()

            # Use RouteMapper to handle the request (synchronous version)
            success, response_data, status_code, error_message = route_mapper.map_route_sync(
                remote_name=remote_name,
                path=path,
                method=request.method,
                headers=dict(request.headers),
                body=body,
                query_params=dict(request.args)
            )

        if not success:
            return jsonify({"error": error_message}), status_code

        return jsonify(response_data), status_code


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
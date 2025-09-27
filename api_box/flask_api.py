"""

Flask Application for API Box

Flask-based application that handles routing to remote APIs and serves config data.

License: CC-BY-4.0

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

    # Store route mapper in app config
    app.config['route_mapper'] = route_mapper

    # Add routes
    _add_main_routes(app, route_mapper)
    _add_remote_routes(app, route_mapper)

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

    @app.route("/<key>")
    def get_config_value(key: str) -> Any:
        """Get a top-level config value by key.

        Args:
            key: Configuration key to retrieve.

        Returns:
            Configuration value or error response.
        """
        success, value, error_message = route_mapper.get_config_value(key)

        if not success:
            if "is a remote name" in error_message:
                return jsonify({"error": error_message}), 400
            else:
                return jsonify({"error": error_message}), 404

        return jsonify(value)


def _add_remote_routes(app: Flask, route_mapper: RouteMapper) -> None:
    """Add remote API proxy routes to the Flask app.

    Args:
        app: Flask application instance.
        route_mapper: RouteMapper instance.
    """

    @app.route("/<remote_name>/<path:path>", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def proxy_to_remote(remote_name: str, path: str):
        """Proxy requests to remote APIs.

        Args:
            remote_name: Name of the remote API.
            path: The path to proxy to the remote API.

        Returns:
            Response from the remote API or error response.
        """
        # Get request body if present
        body = None
        if request.method in ["POST", "PUT", "PATCH"]:
            body = request.get_data()

        # Use RouteMapper to handle the request
        success, response_data, status_code, error_message = await route_mapper.map_route(
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


# Default app instance
app = create_app()
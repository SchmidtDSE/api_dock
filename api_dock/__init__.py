"""

API Dock Core Module

Core functionality for API Dock wrapper.

License: BSD 3-Clause

"""

from typing import Any

from api_dock import fast_api, flask_api
from api_dock.fast_api import create_app as create_fastapi_app
from api_dock.flask_api import create_app as create_flask_app
from api_dock.config import load_main_config, find_remote_config, get_remote_names, get_cookies_config, get_authentication_config, merge_inherited_config, validate_authentication_config, validate_cookies_config, filter_cookies_by_config
from api_dock.route_mapper import RouteMapper

# For backward compatibility, default to FastAPI
create_app = create_fastapi_app

__all__ = [
    "app", "create_app",
    "fastapi_app", "create_fastapi_app",
    "flask_app", "create_flask_app",
    "load_main_config", "find_remote_config", "get_remote_names",
    "RouteMapper"
]


def __getattr__(name: str) -> Any:
    """Return a default app, building it on first read.

    ``app`` and ``fastapi_app`` are the default FastAPI app; ``flask_app`` is
    the default Flask app. Importing api_dock builds neither.

    Args:
        name: Attribute name not otherwise defined in this package.

    Returns:
        The requested default app.

    Raises:
        AttributeError: If name is not one of the default app names.
    """
    if name in ("app", "fastapi_app"):
        return fast_api.app
    if name == "flask_app":
        return flask_api.app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

"""

API Dock Core Module

Core functionality for API Dock wrapper.

License: BSD 3-Clause

"""
#
# IMPORTS
#
from typing import Any

from api_dock.config import (
    filter_cookies_by_config,
    find_remote_config,
    get_authentication_config,
    get_cookies_config,
    get_remote_names,
    load_main_config,
    merge_inherited_config,
    validate_authentication_config,
    validate_cookies_config,
)
from api_dock.fast_api import create_app as create_fastapi_app
from api_dock.flask_api import create_app as create_flask_app
from api_dock.route_mapper import RouteMapper


#
# CONSTANTS
#
# For backward compatibility, default to FastAPI
create_app = create_fastapi_app

__all__ = [
    "app", "create_app",
    "fastapi_app", "create_fastapi_app",
    "flask_app", "create_flask_app",
    "load_main_config", "find_remote_config", "get_remote_names",
    "RouteMapper"
]


#
# PUBLIC
#
def __getattr__(name: str) -> Any:
    """Return the default apps on first use instead of building them at import.

    ``api_dock.app`` / ``api_dock.fastapi_app`` (FastAPI) and
    ``api_dock.flask_app`` are built from the default config the first time
    they are accessed (see ``fast_api.__getattr__``), so importing api_dock no
    longer requires a valid config in the current directory.

    Args:
        name: The attribute being looked up.

    Returns:
        The requested default app.

    Raises:
        AttributeError: For any other missing attribute.
    """
    if name in ("app", "fastapi_app"):
        from api_dock import fast_api
        return fast_api.app
    if name == "flask_app":
        from api_dock import flask_api
        return flask_api.app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

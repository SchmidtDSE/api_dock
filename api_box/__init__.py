"""

API Box Core Module

Core functionality for API Box wrapper.

License: CC-BY-4.0

"""

from api_box.fast_api import app as fastapi_app, create_app as create_fastapi_app
from api_box.flask_api import app as flask_app, create_app as create_flask_app
from api_box.config import load_main_config, find_remote_config, get_remote_names
from api_box.route_mapper import RouteMapper

# For backward compatibility, default to FastAPI
app = fastapi_app
create_app = create_fastapi_app

__all__ = [
    "app", "create_app",
    "fastapi_app", "create_fastapi_app",
    "flask_app", "create_flask_app",
    "load_main_config", "find_remote_config", "get_remote_names",
    "RouteMapper"
]

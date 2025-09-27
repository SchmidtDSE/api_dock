"""

API Box

API wrapper that uses configuration files to pass requests to different APIs.

License: CC-BY-4.0

"""

from .api_box.main import app, create_app
from .api_box.config import load_main_config, find_remote_config

__version__ = "0.1.0"
__all__ = ["app", "create_app", "load_main_config", "find_remote_config"]

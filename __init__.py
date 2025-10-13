"""

API Base

API wrapper that uses configuration files to pass requests to different APIs.

License: BSD 3-Clause

"""

from .api_base.main import app, create_app
from .api_base.config import load_main_config, find_remote_config

__version__ = "0.1.0"
__all__ = ["app", "create_app", "load_main_config", "find_remote_config"]

"""

Configuration Discovery Module for API Dock

Handles discovery and initialization of configuration files.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import os
import shutil
from pathlib import Path
from typing import List, Optional


#
# CONSTANTS
#
LOCAL_CONFIG_DIR: str = "api_dock_config"
DEFAULT_CONFIG_NAME: str = "config"


#
# PUBLIC
#
def find_config(config_name: Optional[str] = None) -> Optional[str]:
    """Find configuration file by name.

    Search order:
    1. api_dock_config/<config_name>.yaml  (local user config)
    2. api_dock/example_api_dock_config/<config_name>.yaml  (bundled package default)

    Args:
        config_name: Config name without .yaml extension (default: "config").

    Returns:
        Path to config file, or None if not found.
    """
    if config_name is None:
        config_name = DEFAULT_CONFIG_NAME

    # Remove .yaml extension if provided
    if config_name.endswith(".yaml"):
        config_name = config_name[:-5]

    # Check local config directory first
    local_path = Path(f"{LOCAL_CONFIG_DIR}/{config_name}.yaml")
    if local_path.exists():
        return str(local_path)

    # Fall back to bundled package examples
    try:
        import importlib.resources as pkg_resources
        package_config = Path(
            pkg_resources.files("api_dock") / "example_api_dock_config" / f"{config_name}.yaml"
        )
        if package_config.exists():
            return str(package_config)
    except Exception:
        pass

    return None


def init_config(overwrite: bool = False) -> Optional[List[str]]:
    """Initialize the local configuration directory from the bundled example.

    Creates api_dock_config/ (with remotes/ and databases/) and copies every
    ``.yaml`` file of the bundled example config into it, keeping its folder
    layout (so versioned folders are copied too).

    Args:
        overwrite: Replace files that already exist (``init --force``). Without
            it, existing files are left alone.

    Returns:
        The paths written (relative to api_dock_config/), or None on error.
    """
    try:
        local_dir = Path(LOCAL_CONFIG_DIR)
        for folder in (local_dir, local_dir / "remotes", local_dir / "databases"):
            folder.mkdir(parents=True, exist_ok=True)

        package_dir = _get_package_config_dir()
        if not package_dir:
            return None

        written = []
        for source in sorted(package_dir.rglob("*.yaml")):
            relative = source.relative_to(package_dir)
            target = local_dir / relative
            if target.exists() and not overwrite:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(source, target)
            written.append(str(relative))
        return written
    except OSError:
        return None


#
# INTERNAL
#
def _get_package_config_dir() -> Optional[Path]:
    """Get the path to the bundled example config directory inside the package.

    Returns:
        Path to example_api_dock_config directory, or None if not found.
    """
    try:
        import importlib.resources as pkg_resources
        config_dir = Path(pkg_resources.files("api_dock") / "example_api_dock_config")
        return config_dir if config_dir.exists() else None
    except Exception:
        return None

"""

Configuration Discovery Module for API Box

Handles discovery and initialization of configuration files.

License: CC-BY-4.0

"""

#
# IMPORTS
#
import os
import shutil
from pathlib import Path
from typing import Optional


#
# CONSTANTS
#
LOCAL_CONFIG_DIR: str = "api_box_config"
DEFAULT_CONFIG_NAME: str = "config"


#
# PUBLIC
#
def find_config(config_name: Optional[str] = None) -> Optional[str]:
    """Find configuration file by name.

    Search order:
    1. api_box_config/<config_name>.yaml
    2. config/<config_name>.yaml
    3. api_box/config/<config_name>.yaml (package default)

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

    # Check local config directory
    local_path = Path(f"{LOCAL_CONFIG_DIR}/{config_name}.yaml")
    if local_path.exists():
        return str(local_path)

    # Check config/ directory
    config_path = Path(f"config/{config_name}.yaml")
    if config_path.exists():
        return str(config_path)

    # Check package config directory
    try:
        import importlib.resources as pkg_resources
        package_config = Path(pkg_resources.files("api_box") / "config" / f"{config_name}.yaml")
        if package_config.exists():
            return str(package_config)
    except Exception:
        pass

    return None


def init_config() -> bool:
    """Initialize local configuration directory.

    This function:
    1. Creates api_box_config/ directory
    2. Creates api_box_config/remotes/ subdirectory
    3. Creates api_box_config/databases/ subdirectory
    4. Copies package default config files to api_box_config/

    Returns:
        True if successful, False on error.
    """
    try:
        # Step 1: Create main config directory
        local_dir = Path(LOCAL_CONFIG_DIR)
        local_dir.mkdir(exist_ok=True)

        # Step 2: Create subdirectories
        (local_dir / "remotes").mkdir(exist_ok=True)
        (local_dir / "databases").mkdir(exist_ok=True)

        # Step 3: Copy default configs from package
        package_dir = _get_package_config_dir()
        if not package_dir:
            return False

        # Copy main config.yaml
        src_config = package_dir / "config.yaml"
        dst_config = local_dir / "config.yaml"
        if src_config.exists() and not dst_config.exists():
            shutil.copy(src_config, dst_config)

        # Copy remotes directory
        src_remotes = package_dir / "remotes"
        dst_remotes = local_dir / "remotes"
        if src_remotes.exists():
            for remote_file in src_remotes.glob("*.yaml"):
                dst_file = dst_remotes / remote_file.name
                if not dst_file.exists():
                    shutil.copy(remote_file, dst_file)

        # Copy databases directory
        src_databases = package_dir / "databases"
        dst_databases = local_dir / "databases"
        if src_databases.exists():
            for db_file in src_databases.glob("*.yaml"):
                dst_file = dst_databases / db_file.name
                if not dst_file.exists():
                    shutil.copy(db_file, dst_file)

        return True

    except Exception:
        return False


#
# INTERNAL
#
def _get_package_config_dir() -> Optional[Path]:
    """Get the path to the package's config directory.

    Returns:
        Path to package config directory, or None if not found.
    """
    try:
        import importlib.resources as pkg_resources
        config_dir = Path(pkg_resources.files("api_box") / "config")
        return config_dir if config_dir.exists() else None
    except Exception:
        return None

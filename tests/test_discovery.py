"""

Tests for auto-discovery of remotes and databases

Without a ``remotes`` / ``databases`` key in the main config, every remote /
database in the config folder is served: top-level files, folders of version
files (at any depth, so names may contain "/"), slugs and remotes/config.yaml
entries, and lookup-generated names. ``excluded_remotes`` /
``excluded_databases`` leave names out. Explicit lists still win.

License: BSD 3-Clause

"""
#
# IMPORTS
#
from pathlib import Path
from typing import Any

import pytest
import yaml

from api_dock.route_mapper import RouteMapper


#
# PUBLIC
#
class TestDiscovery:
    """What is found when the main config doesn't list names."""

    def test_databases_and_remotes(self, tmp_path: Path) -> None:
        _layout(tmp_path)
        mapper = RouteMapper(_main(tmp_path, {}))
        assert mapper.database_names == [
            "birdnet", "birdnet/2.4/bullfrog", "flat", "owl",
        ]
        assert mapper.remote_names == ["inline", "renamed", "versioned"]

    def test_exclusions(self, tmp_path: Path) -> None:
        _layout(tmp_path)
        mapper = RouteMapper(_main(tmp_path, {
            "excluded_databases": ["owl", "birdnet/2.4/bullfrog"],
            "excluded_remotes": ["inline"],
        }))
        assert mapper.database_names == ["birdnet", "flat"]
        assert mapper.remote_names == ["renamed", "versioned"]

    def test_explicit_lists_still_win(self, tmp_path: Path) -> None:
        _layout(tmp_path)
        mapper = RouteMapper(_main(tmp_path, {"databases": ["flat"], "remotes": []}))
        assert mapper.database_names == ["flat"]
        assert mapper.remote_names == []

    def test_exclusions_apply_to_explicit_lists(self, tmp_path: Path) -> None:
        _layout(tmp_path)
        mapper = RouteMapper(_main(tmp_path, {"databases": ["flat", "owl"],
                                              "excluded_databases": ["owl"]}))
        assert mapper.database_names == ["flat"]

    def test_empty_folder(self, tmp_path: Path) -> None:
        mapper = RouteMapper(_main(tmp_path, {}))
        assert mapper.database_names == [] and mapper.remote_names == []

    @pytest.mark.parametrize("value", ["owl", [1], {"a": 1}])
    def test_invalid_exclusions(self, tmp_path: Path, value: Any) -> None:
        with pytest.raises(ValueError, match="excluded_databases must be a list of names"):
            RouteMapper(_main(tmp_path, {"excluded_databases": value}))


#
# INTERNAL
#
def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data))


def _layout(root: Path) -> None:
    route = {"routes": [{"route": "r", "sql": "SELECT 1"}]}
    _write(root / "databases" / "flat.yaml", route)                        # unversioned file
    _write(root / "databases" / "birdnet" / "2.4.yaml", route)             # version folder
    _write(root / "databases" / "birdnet" / "2.4" / "bullfrog" / "0.5.yaml", route)  # nested
    _write(root / "databases" / "config.yaml", {"slugs": [                 # slug
        {"name": "owl", "version": "5.0", "routes": [{"route": "r", "sql": "SELECT 1"}]}]})
    _write(root / "remotes" / "file_stem.yaml", {"name": "renamed", "url": "https://a.org"})
    _write(root / "remotes" / "versioned" / "1.0.yaml", {"url": "https://b.org"})
    _write(root / "remotes" / "config.yaml", {"remotes": {"inline": {"url": "https://c.org"}}})


def _main(root: Path, extra: dict) -> str:
    _write(root / "config.yaml", {"name": "x", **extra})
    return str(root / "config.yaml")

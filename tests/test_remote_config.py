"""

Tests for the shared remote config (remotes/config.yaml) and version ordering

Covers inline remote definitions (unversioned, ``version``, ``versions`` with
defaults), mixing them with version files (files win), ``latest`` resolution
for remotes and databases, and the startup checks.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from api_dock.config import (
    find_remote_config,
    get_inline_remote_configs,
    get_remote_versions,
    is_versioned_remote,
    resolve_latest_version,
    sort_versions,
    versions_equal,
)
from api_dock.database_config import get_database_versions, resolve_latest_database_version
from api_dock.route_mapper import RouteMapper
from api_dock.types import PreparedRequest, ProxyResponse


#
# CONSTANTS
#
INLINE_REMOTES: Dict[str, Any] = {
    "core": {
        "description": "Soundhub API includes birdnet model",
        "version": "0.5.0",
        "authors": ["https://gif.berkeley.edu/"],
        "url": "https://api.dev.wildlifesoundhub.org",
    },
    "wolf": {
        "description": "Soundhub Wolves",
        "authors": ["https://gif.berkeley.edu/"],
        "versions": [
            {
                "url": "https://api.dev.wolvessoundhub.org",
                "version": 1.0,
                "description": "Version Specific Description Soundhub Wolves",
            },
            {
                "url": "https://beta.api.dev.wolvessoundhub.org",
                "version": 0.1,
                "description": "Dev Soundhub Wolves",
            },
        ],
    },
    "plain": {"url": "https://plain.example.org"},
}


#
# PUBLIC
#
class TestVersionOrdering:
    """``latest`` and version lists order versions numerically."""

    @pytest.mark.parametrize("versions, latest", [
        (["0.9", "0.10"], "0.10"),
        (["0.9.0", "0.10.0", "0.5.0"], "0.10.0"),
        (["1.0", "1.0.1"], "1.0.1"),
        (["2.0rc1", "2.0"], "2.0"),
        (["v9", "v10"], "v10"),
        (["2.4", "3.0", "10.0"], "10.0"),
        (["alpha", "beta"], "beta"),
    ])
    def test_latest(self, versions: List[str], latest: str) -> None:
        assert resolve_latest_version(versions) == latest
        assert resolve_latest_database_version(versions) == latest

    def test_empty(self) -> None:
        assert resolve_latest_version([]) is None

    def test_sort(self) -> None:
        assert sort_versions(["0.10", "0.9", "0.1"]) == ["0.1", "0.9", "0.10"]

    def test_database_versions_sorted(self, tmp_path: Path) -> None:
        for version in ("0.9", "0.10", "0.2"):
            _write(tmp_path / "databases" / "db" / f"{version}.yaml", {"routes": []})
        assert get_database_versions("db", str(tmp_path)) == ["0.2", "0.9", "0.10"]


class TestVersionsEqual:
    """Include/exclude and listing filters match versions part by part."""

    @pytest.mark.parametrize("version, spec, equal", [
        ("3.0", 3.0, True), ("3", "3.0", True), ("4.0", 4, True), ("2.4", "2.4.0", True),
        ("1.10", "1.1", False), ("1.1", 1.1, True), ("2.4v0.5", "2.4v0.5", True),
        ("0.10", 0.1, False), ("a", "b", False),
    ])
    def test_versions_equal(self, version: str, spec: Any, equal: bool) -> None:
        assert versions_equal(version, spec) is equal


class TestInlineRemotes:
    """Parsing ``remotes`` in remotes/config.yaml."""

    def test_forms(self, tmp_path: Path) -> None:
        _write_inline(tmp_path)
        configs = get_inline_remote_configs(str(tmp_path))
        assert list(configs["core"]) == ["0.5.0"]
        assert configs["core"]["0.5.0"]["url"] == "https://api.dev.wildlifesoundhub.org"
        assert set(configs["wolf"]) == {"1.0", "0.1"}
        assert configs["wolf"]["0.1"]["description"] == "Dev Soundhub Wolves"
        assert configs["wolf"]["0.1"]["authors"] == ["https://gif.berkeley.edu/"]
        assert configs["wolf"]["1.0"]["name"] == "wolf"
        assert list(configs["plain"]) == [None]

    def test_missing_file(self, tmp_path: Path) -> None:
        assert get_inline_remote_configs(str(tmp_path)) == {}

    def test_empty_file(self, tmp_path: Path) -> None:
        path = tmp_path / "remotes" / "config.yaml"
        path.parent.mkdir(parents=True)
        path.write_text("# nothing yet\n")
        assert get_inline_remote_configs(str(tmp_path)) == {}

    @pytest.mark.parametrize("remotes, message", [
        (["core"], "must be a mapping of name"),
        ({"x": "https://x"}, "must be a mapping"),
        ({"x": {"url": "u", "version": 1, "versions": [{"version": 2}]}}, "uses both"),
        ({"x": {"url": "u", "versions": []}}, "non-empty list"),
        ({"x": {"url": "u", "versions": [{"url": "v"}]}}, "without a version"),
        ({"x": {"url": "u", "versions": [{"version": 1}, {"version": "1"}]}}, "defined twice"),
    ])
    def test_malformed(self, tmp_path: Path, remotes: Any, message: str) -> None:
        _write(tmp_path / "remotes" / "config.yaml", {"remotes": remotes})
        with pytest.raises(ValueError, match=message):
            get_inline_remote_configs(str(tmp_path))


class TestMixingWithFiles:
    """Version files and inline entries combine; files win."""

    def test_versions_combine(self, tmp_path: Path) -> None:
        _write_inline(tmp_path)
        _write(tmp_path / "remotes" / "wolf" / "2.0.yaml", {"url": "https://file.example.org"})
        assert is_versioned_remote("wolf", {}, str(tmp_path))
        assert get_remote_versions("wolf", {}, str(tmp_path)) == ["0.1", "1.0", "2.0"]
        config = find_remote_config("wolf", {}, str(tmp_path), version="0.1")
        assert config["url"] == "https://beta.api.dev.wolvessoundhub.org"

    def test_file_wins(self, tmp_path: Path) -> None:
        _write_inline(tmp_path)
        _write(tmp_path / "remotes" / "wolf" / "1.0.yaml", {"url": "https://file.example.org"})
        config = find_remote_config("wolf", {}, str(tmp_path), version="1.0")
        assert config["url"] == "https://file.example.org"

    def test_unversioned_file_wins(self, tmp_path: Path) -> None:
        _write_inline(tmp_path)
        _write(tmp_path / "remotes" / "plain.yaml", {"name": "plain", "url": "https://file"})
        main = {"remotes": ["plain"]}
        assert not is_versioned_remote("plain", main, str(tmp_path))
        assert find_remote_config("plain", main, str(tmp_path))["url"] == "https://file"

    def test_unversioned_inline_only(self, tmp_path: Path) -> None:
        _write_inline(tmp_path)
        main = {"remotes": ["plain"]}
        assert find_remote_config("plain", main, str(tmp_path))["url"] == "https://plain.example.org"

    def test_unknown_version(self, tmp_path: Path) -> None:
        _write_inline(tmp_path)
        with pytest.raises(FileNotFoundError):
            find_remote_config("wolf", {}, str(tmp_path), version="9.9")


class TestRequests:
    """Requests through RouteMapper use inline remotes and pick ``latest`` correctly."""

    def test_inline_versions_and_latest(self, tmp_path: Path) -> None:
        _write_inline(tmp_path)
        _write(tmp_path / "remotes" / "wolf" / "0.10.yaml", {"url": "https://ten.example.org"})
        mapper = _mapper(tmp_path, ["core", "wolf", "plain"])

        assert (_url(mapper, "core", "0.5.0/items")).startswith(
            "https://api.dev.wildlifesoundhub.org/items"
        )
        assert (_url(mapper, "core", "latest/items")).startswith(
            "https://api.dev.wildlifesoundhub.org/items"
        )
        assert (_url(mapper, "wolf", "0.1/x")).startswith("https://beta.api.dev")
        # 0.10 (a file) is newer than 0.1 and older than 1.0 (inline)
        assert (_url(mapper, "wolf", "0.10/x")).startswith("https://ten.example.org")
        assert (_url(mapper, "wolf", "latest/x")).startswith(
            "https://api.dev.wolvessoundhub.org"
        )
        assert (_url(mapper, "plain", "x")).startswith("https://plain.example.org/x")

    def test_version_listing(self, tmp_path: Path) -> None:
        _write_inline(tmp_path)
        mapper = _mapper(tmp_path, ["wolf"])
        result = asyncio.run(mapper.prepare_remote_request("wolf", "", "GET"))
        assert isinstance(result, ProxyResponse)
        assert b'"0.1", "1.0"' in result.content

    def test_unknown_version_404(self, tmp_path: Path) -> None:
        _write_inline(tmp_path)
        mapper = _mapper(tmp_path, ["wolf"])
        result = asyncio.run(mapper.prepare_remote_request("wolf", "3.0/x", "GET"))
        assert isinstance(result, ProxyResponse) and result.status_code == 404


class TestStartupChecks:
    """A bad remotes/config.yaml stops startup."""

    def test_malformed_stops_startup(self, tmp_path: Path) -> None:
        _write(tmp_path / "remotes" / "config.yaml", {"remotes": ["core"]})
        with pytest.raises(ValueError, match="remotes/config.yaml"):
            _mapper(tmp_path, ["core"])

    def test_missing_url_stops_startup(self, tmp_path: Path) -> None:
        _write(tmp_path / "remotes" / "config.yaml", {"remotes": {"core": {"version": 1}}})
        with pytest.raises(ValueError, match="'core version 1' has no url"):
            _mapper(tmp_path, ["core"])

    def test_unlisted_remote_warns(
            self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        _write_inline(tmp_path)
        with caplog.at_level("WARNING"):
            mapper = _mapper(tmp_path, ["core"])
        assert "'wolf'" in caplog.text
        assert mapper.remote_names == ["core"]


#
# INTERNAL
#
def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data))


def _write_inline(root: Path) -> None:
    _write(root / "remotes" / "config.yaml", {"remotes": INLINE_REMOTES})


def _mapper(root: Path, remotes: List[str]) -> RouteMapper:
    _write(root / "config.yaml", {
        "name": "test",
        "remotes": remotes,
        "settings": {"add_trailing_slash": False},
    })
    return RouteMapper(str(root / "config.yaml"))


def _url(mapper: RouteMapper, remote: str, path: str) -> Optional[str]:
    result = asyncio.run(mapper.prepare_remote_request(remote, path, "GET"))
    assert isinstance(result, PreparedRequest), getattr(result, "content", result)
    return result.url

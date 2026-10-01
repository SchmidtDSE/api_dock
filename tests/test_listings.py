"""

Tests for the optional catalog-listing endpoints (the ``expose`` config).

Covers ``resolve_listing_specs`` (normalizing the ``expose`` section into specs
plus warnings), ``build_listing`` (the response bodies, including version
filtering and dict/string formats), and route registration in both the FastAPI
and Flask adapters.

License: BSD 3-Clause

"""

#
# IMPORTS
#
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from api_dock.fast_api import create_app as create_fastapi_app
from api_dock.flask_api import create_app as create_flask_app
from api_dock.listings import build_listing, resolve_listing_specs
from api_dock.route_mapper import RouteMapper
from api_dock.types import ListingSpec


#
# CONSTANTS
#
DBS = {"birdnet": ["2.4", "3.0"], "owl": ["0.5"]}
REMOTES = {"core": ["0.5.0"]}
DATABASE_CONFIG = {
    "name": "catalog",
    "tables": {"items": "items.parquet"},
    "routes": [{"route": "items", "sql": "SELECT * FROM [[items]]"}],
}


#
# PUBLIC
#
def _patch_sources():
    """Patch name/version discovery so build_listing is deterministic offline."""
    return [
        patch("api_dock.listings.get_database_names", return_value=list(DBS)),
        patch("api_dock.listings.is_versioned_database", return_value=True),
        patch("api_dock.listings.get_database_versions", side_effect=lambda n, *a, **k: DBS[n]),
        patch("api_dock.listings.get_remote_names", return_value=list(REMOTES)),
        patch("api_dock.listings.is_versioned_remote", return_value=True),
        patch("api_dock.listings.get_remote_versions", side_effect=lambda n, *a, **k: REMOTES[n]),
    ]


class TestResolveListingSpecs:
    """Normalization of the ``expose`` config section."""

    def test_absent_exposes_nothing(self) -> None:
        specs, warns = resolve_listing_specs({})
        assert specs == []
        assert warns == []

    def test_false_exposes_nothing(self) -> None:
        specs, _ = resolve_listing_specs({"expose": False})
        assert specs == []

    def test_true_exposes_all_defaults(self) -> None:
        specs, _ = resolve_listing_specs({"expose": True})
        by_kind = {s.kind: s for s in specs}
        assert set(by_kind) == {"databases", "remotes", "sources"}
        assert by_kind["databases"].route == "databases"
        assert all(s.as_dict is True and s.include is True for s in specs)

    def test_empty_mapping_exposes_nothing(self) -> None:
        specs, _ = resolve_listing_specs({"expose": {}})
        assert specs == []

    def test_selective_enable(self) -> None:
        specs, _ = resolve_listing_specs({"expose": {"databases": True}})
        assert [s.kind for s in specs] == ["databases"]

    def test_dict_false_switches_string_format(self) -> None:
        specs, _ = resolve_listing_specs({"expose": {"dict": False, "remotes": True}})
        assert specs[0].as_dict is False

    def test_string_value_sets_custom_route(self) -> None:
        specs, _ = resolve_listing_specs({"expose": {"databases": "list/databases"}})
        assert specs[0].route == "list/databases"
        assert specs[0].include is True

    def test_dict_form_route_and_include(self) -> None:
        specs, _ = resolve_listing_specs(
            {"expose": {"databases": {"route": "list/dbs", "include": ["birdnet"]}}}
        )
        assert specs[0].route == "list/dbs"
        assert specs[0].include == ["birdnet"]

    def test_list_value_is_include(self) -> None:
        specs, _ = resolve_listing_specs(
            {"expose": {"databases": ["birdnet", {"owl": {"versions": [4.0]}}]}}
        )
        assert specs[0].route == "databases"
        assert specs[0].include == ["birdnet", {"owl": {"versions": [4.0]}}]

    def test_include_false_keeps_endpoint(self) -> None:
        specs, _ = resolve_listing_specs(
            {"expose": {"databases": {"route": "x", "include": False}}}
        )
        assert len(specs) == 1
        assert specs[0].include is False

    def test_unknown_key_warns(self) -> None:
        _, warns = resolve_listing_specs({"expose": {"foo": True}})
        assert any("unknown key 'foo'" in w for w in warns)

    def test_route_shadow_warns(self) -> None:
        config = {"databases": ["databases"], "expose": {"databases": True}}
        _, warns = resolve_listing_specs(config)
        assert any("shadows the proxy" in w for w in warns)

    def test_unknown_include_warns(self) -> None:
        config = {"databases": ["birdnet"], "expose": {"databases": ["nope"]}}
        _, warns = resolve_listing_specs(config)
        assert any("unknown 'nope'" in w for w in warns)

    def test_invalid_expose_type_warns(self) -> None:
        specs, warns = resolve_listing_specs({"expose": 5})
        assert specs == []
        assert any("expected a boolean or mapping" in w for w in warns)


class TestBuildListing:
    """The listing response bodies."""

    def _build(self, spec):
        patches = _patch_sources()
        for p in patches:
            p.start()
        try:
            return build_listing(spec, {})
        finally:
            for p in patches:
                p.stop()

    def test_databases_dict_form(self) -> None:
        rows = self._build(ListingSpec("databases", "databases", True, True))
        assert rows == [
            {"model": "birdnet", "version": "2.4"},
            {"model": "birdnet", "version": "3.0"},
            {"model": "owl", "version": "0.5"},
        ]

    def test_databases_string_form(self) -> None:
        rows = self._build(ListingSpec("databases", "databases", False, True))
        assert rows == ["birdnet/2.4", "birdnet/3.0", "owl/0.5"]

    def test_remotes(self) -> None:
        rows = self._build(ListingSpec("remotes", "remotes", True, True))
        assert rows == [{"model": "core", "version": "0.5.0"}]

    def test_sources_merges_databases_then_remotes(self) -> None:
        rows = self._build(ListingSpec("sources", "sources", False, True))
        assert rows == ["birdnet/2.4", "birdnet/3.0", "owl/0.5", "core/0.5.0"]

    def test_include_filters_by_name(self) -> None:
        rows = self._build(ListingSpec("databases", "databases", False, ["owl"]))
        assert rows == ["owl/0.5"]

    def test_include_filters_by_version_tolerating_floats(self) -> None:
        spec = ListingSpec("databases", "databases", False, [{"birdnet": {"versions": [3.0]}}])
        rows = self._build(spec)
        assert rows == ["birdnet/3.0"]

    def test_include_false_returns_empty(self) -> None:
        rows = self._build(ListingSpec("databases", "databases", True, False))
        assert rows == []

    def test_unversioned_source(self) -> None:
        with patch("api_dock.listings.get_database_names", return_value=["flat"]), \
             patch("api_dock.listings.is_versioned_database", return_value=False):
            dict_rows = build_listing(ListingSpec("databases", "databases", True, True), {})
            str_rows = build_listing(ListingSpec("databases", "databases", False, True), {})
        assert dict_rows == [{"model": "flat", "version": None}]
        assert str_rows == ["flat"]


class TestAdapterRegistration:
    """Both adapters register listing routes (custom routes included)."""

    def _write_config(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("name: t\nexpose:\n  databases: true\n  remotes: \"list/remotes\"\n")
        return str(cfg)

    def test_fastapi_registers_routes(self, tmp_path) -> None:
        app = create_fastapi_app(self._write_config(tmp_path))
        paths = {route.path for route in app.routes}
        assert "/databases" in paths
        assert "/list/remotes" in paths

    def test_flask_registers_routes(self, tmp_path) -> None:
        app = create_flask_app(self._write_config(tmp_path))
        rules = {rule.rule for rule in app.url_map.iter_rules()}
        assert "/databases" in rules
        assert "/list/remotes" in rules

    def test_fastapi_no_expose_no_routes(self, tmp_path) -> None:
        cfg = tmp_path / "config.yaml"
        cfg.write_text("name: t\n")
        app = create_fastapi_app(str(cfg))
        paths = {route.path for route in app.routes}
        assert "/databases" not in paths

    def test_fastapi_accepts_both_slash_forms(self, tmp_path) -> None:
        """Both /route and /route/ resolve to the listing (not the proxy)."""
        from fastapi.testclient import TestClient
        cfg = tmp_path / "config.yaml"
        cfg.write_text("name: t\nexpose: true\n")
        client = TestClient(create_fastapi_app(str(cfg)))
        for path in ("/sources", "/sources/", "/databases", "/databases/"):
            assert client.get(path).status_code == 200, path

    def test_flask_accepts_both_slash_forms(self, tmp_path) -> None:
        """Flask's strict_slashes=False accepts both /route and /route/."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text("name: t\nexpose: true\n")
        client = create_flask_app(str(cfg)).test_client()
        for path in ("/sources", "/sources/", "/databases", "/databases/"):
            assert client.get(path).status_code == 200, path


class TestConfigOutsideDefaultFolder:
    """Listings read the folder that holds the main config, not the default one."""

    def test_database_versions(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        mapper = _mapper_outside_default_folder(tmp_path, monkeypatch)
        spec = ListingSpec(kind="databases", route="databases", as_dict=False)
        assert mapper.get_listing(spec) == ["catalog/1.0", "catalog/2.0"]

    def test_remote_versions(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        mapper = _mapper_outside_default_folder(tmp_path, monkeypatch)
        spec = ListingSpec(kind="remotes", route="remotes", as_dict=False)
        assert mapper.get_listing(spec) == ["weather/0.1", "forecast"]

    def test_include_uses_remote_name_from_file(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        mapper = _mapper_outside_default_folder(tmp_path, monkeypatch)
        assert mapper.listing_warnings == []


#
# INTERNAL
#
def _mapper_outside_default_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RouteMapper:
    """Write a config to a non-default folder and load it from another directory.

    The folder has a versioned database, a versioned remote, and a remote whose
    file name (``fc``) differs from the ``name`` inside it (``forecast``).

    Args:
        tmp_path: Pytest temp directory.
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        RouteMapper loaded from the config.
    """
    config_dir = tmp_path / "my_config"
    for version in ("1.0", "2.0"):
        _write_yaml(config_dir / "databases" / "catalog" / f"{version}.yaml", DATABASE_CONFIG)
    _write_yaml(config_dir / "remotes" / "weather" / "0.1.yaml",
                {"name": "weather", "url": "https://example.com"})
    _write_yaml(config_dir / "remotes" / "fc.yaml",
                {"name": "forecast", "url": "https://example.com"})
    _write_yaml(config_dir / "config.yaml", {
        "name": "test",
        "databases": ["catalog"],
        "remotes": ["weather", "fc"],
        "expose": {"remotes": ["weather", "forecast"]},
    })
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    return RouteMapper(str(config_dir / "config.yaml"))


def _write_yaml(path: Path, data: dict) -> None:
    """Write data to path as YAML, creating parent folders.

    Args:
        path: File to write.
        data: Data to serialize.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data))

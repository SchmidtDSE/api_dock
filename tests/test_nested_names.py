"""

Tests for nested names: remote/database names that contain "/"

A request goes to the longest served name its path starts with, so
``birdnet/2.4/bullfrog/0.5/recordings`` reaches the ``birdnet/2.4/bullfrog``
database (version 0.5) while ``birdnet/2.4/recordings`` reaches ``birdnet``.
Covers both web frameworks, file-based nested names, the
``allow_nested_names`` setting, name rules, the startup shadowing check,
include/exclude entries, union source columns and lookup-generated names.

License: BSD 3-Clause

"""
#
# IMPORTS
#
from pathlib import Path
from typing import Any, Dict, List, Optional

import duckdb
import pytest
import yaml
from fastapi.testclient import TestClient

from api_dock import fast_api, flask_api
from api_dock.route_mapper import RouteMapper


#
# CONSTANTS
#
NESTED: str = "birdnet/2.4/bullfrog"


#
# PUBLIC
#
class TestRequests:
    """Requests reach the longest matching name."""

    def test_fastapi(self, tmp_path: Path) -> None:
        with TestClient(fast_api.create_app(_setup(tmp_path))) as client:
            assert _ids(client.get("/birdnet/2.4/recordings").json()) == ["b1", "b2"]
            assert _ids(client.get(f"/{NESTED}/0.5/recordings").json()) == ["f1"]
            assert _ids(client.get(f"/{NESTED}/latest/recordings").json()) == ["f1"]
            assert client.get(f"/{NESTED}/").json() == {"versions": ["0.5"]}
            assert client.get(f"/{NESTED}").json() == {"versions": ["0.5"]}
            assert client.get("/birdnet/").json() == {"versions": ["2.4"]}
            response = client.get("/nope/x")
            assert response.status_code == 404
            assert response.json() == {"error": "Remote 'nope' not found"}

    def test_flask_with_trailing_slash_routes(self, tmp_path: Path) -> None:
        client = flask_api.create_app(_setup(tmp_path, slash_routes=True)).test_client()
        assert _ids(client.get("/birdnet/2.4/recordings/").get_json()) == ["b1", "b2"]
        assert _ids(client.get(f"/{NESTED}/0.5/recordings/").get_json()) == ["f1"]
        assert client.get("/nope/x").status_code == 404

    def test_split_name(self, tmp_path: Path) -> None:
        mapper = RouteMapper(_setup(tmp_path))
        assert mapper.split_name(f"{NESTED}/0.5/recordings") == (NESTED, "0.5/recordings")
        assert mapper.split_name("birdnet/2.4/recordings") == ("birdnet", "2.4/recordings")
        assert mapper.split_name("birdnet/2.4/bullfrogs/x") == ("birdnet", "2.4/bullfrogs/x")
        assert mapper.split_name(NESTED) == (NESTED, "")
        assert mapper.split_name("bird") is None

    def test_file_based_nested_versions(self, tmp_path: Path) -> None:
        _parquet(tmp_path, "b", ["b1"])
        _parquet(tmp_path, "f", ["f1"])
        _write(tmp_path / "databases" / "birdnet" / "2.4.yaml", _db_file(tmp_path, "b"))
        _write(tmp_path / "databases" / "birdnet" / "2.4" / "bullfrog" / "0.5.yaml",
               _db_file(tmp_path, "f"))
        _write(tmp_path / "config.yaml", {"name": "x", "databases": ["birdnet", NESTED]})
        with TestClient(fast_api.create_app(str(tmp_path / "config.yaml"))) as client:
            assert client.get("/birdnet/").json() == {"versions": ["2.4"]}
            assert _ids(client.get("/birdnet/2.4/recordings").json()) == ["b1"]
            assert _ids(client.get(f"/{NESTED}/0.5/recordings").json()) == ["f1"]


class TestNameRules:
    """allow_nested_names and the name checks."""

    def test_setting_off(self, tmp_path: Path) -> None:
        config = _setup(tmp_path, main_extra={"settings": {"allow_nested_names": False}})
        with pytest.raises(ValueError, match="allow_nested_names is false"):
            RouteMapper(config)

    def test_setting_off_without_nested_names(self, tmp_path: Path) -> None:
        _setup(tmp_path, nested=False, main_extra={"settings": {"allow_nested_names": False}})
        RouteMapper(str(tmp_path / "config.yaml"))

    @pytest.mark.parametrize("name, message", [
        ("birdnet//bullfrog", "empty segment"),
        ("birdnet/latest/bullfrog", "'latest' segment"),
    ])
    def test_invalid_names(self, tmp_path: Path, name: str, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            RouteMapper(_setup(tmp_path, nested_name=name))


class TestShadowing:
    """Nested names that would hide part of an outer name stop startup."""

    def test_hides_a_route(self, tmp_path: Path) -> None:
        extra = [{"route": "bullfrog/{{x}}", "sql": "SELECT 1"}]
        with pytest.raises(ValueError, match="would hide route 'bullfrog/{{x}}' of 'birdnet/2.4'"):
            RouteMapper(_setup(tmp_path, outer_routes=extra))

    def test_variable_first_segment_is_hidden(self, tmp_path: Path) -> None:
        extra = [{"route": "{{id}}/detail", "sql": "SELECT 1"}]
        with pytest.raises(ValueError, match="would hide route"):
            RouteMapper(_setup(tmp_path, outer_routes=extra))

    def test_longer_nested_path_is_fine(self, tmp_path: Path) -> None:
        extra = [{"route": "bullfrog", "sql": "SELECT 1"}]   # one segment: 'bullfrog/x/...'
        RouteMapper(_setup(tmp_path, outer_routes=extra, nested_name="birdnet/2.4/bullfrog/x"))

    def test_hides_a_whole_version(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="would hide every route of 'birdnet/2.4'"):
            RouteMapper(_setup(tmp_path, nested_name="birdnet/2.4"))

    def test_not_a_version_of_the_outer_name(self, tmp_path: Path) -> None:
        RouteMapper(_setup(tmp_path, nested_name="birdnet/9.9/bullfrog"))

    def test_nested_under_a_remote(self, tmp_path: Path) -> None:
        config = _setup(tmp_path, nested=False)
        _write(tmp_path / "remotes" / "svc.yaml", {"name": "svc", "url": "https://example.org"})
        _write(tmp_path / "remotes" / "config.yaml", {"remotes": {
            "svc/v2": {"url": "https://example.org/v2"}}})
        main = yaml.safe_load(Path(config).read_text())
        main["remotes"] = ["svc", "svc/v2"]
        _write(Path(config), main)
        with pytest.raises(ValueError, match="nested under remote 'svc'"):
            RouteMapper(config)


class TestSelection:
    """include/exclude entries compare against the full name."""

    def test_include_entries(self, tmp_path: Path) -> None:
        routes = [
            {"route": "only_nested", "include": [f"{NESTED}/0.5"], "sql": "SELECT 'n' AS id"},
            {"route": "only_outer", "include": ["birdnet/2.4"], "sql": "SELECT 'o' AS id"},
            {"route": "nested_all", "include": [NESTED], "sql": "SELECT 'a' AS id"},
        ]
        with TestClient(fast_api.create_app(_setup(tmp_path, shared_routes=routes))) as client:
            assert client.get(f"/{NESTED}/0.5/only_nested").json() == [{"id": "n"}]
            assert client.get("/birdnet/2.4/only_nested").status_code == 404
            assert client.get("/birdnet/2.4/only_outer").json() == [{"id": "o"}]
            assert client.get(f"/{NESTED}/0.5/only_outer").status_code == 404
            assert client.get(f"/{NESTED}/0.5/nested_all").json() == [{"id": "a"}]


class TestSourcesAndLookups:
    """Nested names in union source columns and lookup-generated names."""

    def test_union_name_column(self, tmp_path: Path) -> None:
        routes = [{"route": "all", "source_columns": ["name", "version"],
                   "sql": "SELECT u.* FROM [[*.recordings]] u"}]
        with TestClient(fast_api.create_app(_setup(tmp_path, shared_routes=routes))) as client:
            rows = client.get("/birdnet/2.4/all").json()
        assert sorted((r["id"], r["name"], r["version"]) for r in rows) == [
            ("b1", "birdnet", "2.4"), ("b2", "birdnet", "2.4"), ("f1", NESTED, "0.5"),
        ]

    def test_lookup_generated_nested_names(self, tmp_path: Path) -> None:
        _parquet(tmp_path, "f", ["f1"])
        duckdb.sql(f"COPY (SELECT 'birdnet/2.4/bullfrog' AS name, '0.5' AS version, "
                   f"'{tmp_path / 'f.parquet'}' AS uri) TO '{tmp_path / 'runs.parquet'}' "
                   "(FORMAT parquet)")
        _write(tmp_path / "databases" / "config.yaml", {
            "database": {"runs": str(tmp_path / "runs.parquet")},
            "lookups": {"runs": {"sql": "SELECT * FROM [[runs]]", "allow": [str(tmp_path) + "/"]}},
            "slugs": [{"from": "runs", "name": "{{row.name}}", "version": "{{row.version}}",
                       "tables": {"recordings": {"uri": "{{row.uri}}"}}}],
            "routes": [{"route": "recordings", "sql": "SELECT * FROM [[recordings]] ORDER BY id"}],
        })
        _write(tmp_path / "config.yaml", {"name": "x", "databases": [{"from": "runs"}]})
        mapper = RouteMapper(str(tmp_path / "config.yaml"))
        assert mapper.database_names == [NESTED]
        assert mapper.split_name(f"{NESTED}/0.5/recordings") == (NESTED, "0.5/recordings")


#
# INTERNAL
#
def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def _parquet(root: Path, name: str, ids: List[str]) -> None:
    values = ", ".join(f"('{i}')" for i in ids)
    duckdb.sql(f"COPY (SELECT * FROM (VALUES {values}) t(id)) TO '{root / f'{name}.parquet'}' "
               "(FORMAT parquet)")


def _db_file(root: Path, table: str) -> Dict[str, Any]:
    return {"tables": {"recordings": str(root / f"{table}.parquet")},
            "routes": [{"route": "recordings", "sql": "SELECT * FROM [[recordings]] ORDER BY id"}]}


def _setup(root: Path, nested: bool = True, nested_name: str = NESTED,
           outer_routes: Optional[List[Any]] = None, shared_routes: Optional[List[Any]] = None,
           slash_routes: bool = False, main_extra: Optional[Dict[str, Any]] = None) -> str:
    _parquet(root, "b", ["b1", "b2"])
    _parquet(root, "f", ["f1"])
    route = "recordings/" if slash_routes else "recordings"
    recordings = {"route": route, "sql": "SELECT * FROM [[recordings]] ORDER BY id"}
    slugs: List[Dict[str, Any]] = [{
        "name": "birdnet", "version": "2.4", "schema": "birdnet_2p4",
        "routes": [recordings] + list(outer_routes or []),
    }]
    if nested:
        slugs.append({"name": nested_name, "version": "0.5", "schema": "bullfrog_0p5",
                      "routes": [recordings]})
    _write(root / "databases" / "config.yaml", {
        "database": {"schema": {
            "birdnet_2p4": {"recordings": str(root / "b.parquet")},
            "bullfrog_0p5": {"recordings": str(root / "f.parquet")},
        }},
        "slugs": slugs,
        "routes": list(shared_routes or []),
    })
    names = ["birdnet"] + ([nested_name] if nested else [])
    _write(root / "config.yaml", {"name": "x", "databases": names, **(main_extra or {})})
    return str(root / "config.yaml")


def _ids(rows: Any) -> List[str]:
    assert isinstance(rows, list), rows
    return [row["id"] for row in rows]

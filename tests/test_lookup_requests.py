"""

End-to-end tests for lookups: startup, generated databases and remotes, refresh

A SQL lookup over a local Parquet catalog generates one database/version (with
an inline schema) per row. Covers serving the generated databases (routes,
``latest``, version listing, ``[[*.table]]`` unions, ``expose`` listings),
main-config ``from:`` entries, ``required`` lookups, refreshes that are kept
or rejected, the manual refresh endpoint, background refresh (FastAPI) and
lazy refresh (Flask), and remote versions from an HTTP lookup.

License: BSD 3-Clause

"""
#
# IMPORTS
#
import asyncio
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import duckdb
import pytest
import yaml
from fastapi.testclient import TestClient

from api_dock import fast_api, flask_api, route_mapper
from api_dock.route_mapper import RouteMapper
from api_dock.types import PreparedRequest


#
# CONSTANTS
#
TOKEN: str = "test-admin-token"
AUTH: Dict[str, str] = {"Authorization": f"Bearer {TOKEN}"}
# (model, version, schema, run id, detection ids)
RUNS: List[Tuple[str, str, str, str, List[str]]] = [
    ("birdnet", "2.4", "birdnet_2p4", "r1", ["a", "b"]),
    ("birdnet", "3.0", "birdnet_3p0", "r2", ["c"]),
    ("perch", "8.0", "perch_8p0", "r3", ["d"]),
]
NEW_RUN: Tuple[str, str, str, str, List[str]] = ("birdnet", "10.0", "birdnet_10p0", "r4", ["e"])
OWL_RUN: Tuple[str, str, str, str, List[str]] = ("owl", "1.0", "owl_1p0", "r5", ["f"])


#
# PUBLIC
#
class TestGeneratedDatabases:
    """Databases generated at startup are served like any other."""

    def test_routes_latest_and_versions(self, tmp_path: Path) -> None:
        mapper = _mapper(tmp_path)
        assert mapper.database_names == ["birdnet", "perch"]
        assert _ids(mapper, "birdnet", "2.4/detections") == ["a", "b"]
        assert _ids(mapper, "birdnet", "latest/detections") == ["c"]
        assert _ids(mapper, "perch", "8.0/detections") == ["d"]
        assert _get(mapper, "birdnet", "") == {"versions": ["2.4", "3.0"]}

    def test_union_over_generated_schemas(self, tmp_path: Path) -> None:
        rows = _get(_mapper(tmp_path), "perch", "8.0/all")
        assert sorted((r["id"], r["schema_name"], r["name"], r["version"]) for r in rows) == [
            ("a", "birdnet_2p4", "birdnet", "2.4"), ("b", "birdnet_2p4", "birdnet", "2.4"),
            ("c", "birdnet_3p0", "birdnet", "3.0"), ("d", "perch_8p0", "perch", "8.0"),
        ]

    def test_expose_listing(self, tmp_path: Path) -> None:
        _setup(tmp_path, main={"expose": True})
        with TestClient(fast_api.create_app(str(tmp_path / "config.yaml"))) as client:
            assert client.get("/databases").json() == [
                {"model": "birdnet", "version": "2.4"}, {"model": "birdnet", "version": "3.0"},
                {"model": "perch", "version": "8.0"},
            ]

    def test_static_main_config_entries_still_work(self, tmp_path: Path) -> None:
        mapper = _mapper(tmp_path, main={"databases": ["birdnet"]})
        assert mapper.database_names == ["birdnet"]
        assert _get(mapper, "perch", "8.0/detections")["error"]


class TestStartup:
    """Lookups that fail at startup."""

    def test_failed_lookup_starts_without_rows(self, tmp_path: Path, caplog: Any) -> None:
        _setup(tmp_path)
        (tmp_path / "catalog.parquet").unlink()
        with caplog.at_level("WARNING"):
            mapper = RouteMapper(str(tmp_path / "config.yaml"))
        assert mapper.database_names == []
        assert "Lookup 'runs' failed at startup" in caplog.text
        assert mapper.lookups.status()[0]["error"]

    def test_failed_required_lookup_stops_startup(self, tmp_path: Path) -> None:
        _setup(tmp_path, required=True)
        (tmp_path / "catalog.parquet").unlink()
        with pytest.raises(ValueError, match="Required lookup 'runs' failed"):
            RouteMapper(str(tmp_path / "config.yaml"))

    def test_invalid_rows_stop_startup(self, tmp_path: Path) -> None:
        bad_run = ("perch", "8.0", "missing", "r3", ["d"])
        _setup(tmp_path, schema_by_name=True, runs=RUNS[:1] + [bad_run])
        with pytest.raises(ValueError, match="perch.*detections"):
            RouteMapper(str(tmp_path / "config.yaml"))


class TestRefresh:
    """refresh_lookups keeps new rows only when the config they produce is valid."""

    def test_new_rows_are_served(self, tmp_path: Path) -> None:
        mapper = _mapper(tmp_path)
        _catalog(tmp_path, RUNS + [NEW_RUN, OWL_RUN])
        report = mapper.refresh_lookups()
        assert report["refreshed"] == ["runs"] and not report["rejected"]
        assert mapper.database_names == ["birdnet", "owl", "perch"]
        assert _ids(mapper, "birdnet", "latest/detections") == ["e"]
        assert _ids(mapper, "owl", "1.0/detections") == ["f"]

    def test_removed_rows_disappear(self, tmp_path: Path) -> None:
        mapper = _mapper(tmp_path)
        _catalog(tmp_path, RUNS[:1])
        mapper.refresh_lookups()
        assert mapper.database_names == ["birdnet"]
        assert _get(mapper, "birdnet", "") == {"versions": ["2.4"]}

    def test_invalid_rows_are_rejected(self, tmp_path: Path) -> None:
        mapper = _mapper(tmp_path, schema_by_name=True)
        _catalog(tmp_path, RUNS + [("owl", "1.0", "missing", "r5", ["f"])])
        report = mapper.refresh_lookups()
        assert report["refreshed"] == [] and "owl" in report["rejected"]
        assert mapper.database_names == ["birdnet", "perch"]
        assert _ids(mapper, "perch", "8.0/detections") == ["d"]
        assert "rejected" in mapper.lookups.status()[0]["error"]

    def test_skipped_row_warned_once(self, tmp_path: Path, caplog: Any) -> None:
        mapper = _mapper(tmp_path)
        _catalog(tmp_path, RUNS + [("bad name", "1.0", "bad_1p0", "r9", ["z"])])
        with caplog.at_level("WARNING"):
            mapper.refresh_lookups()
            _get(mapper, "perch", "8.0/detections")
        assert caplog.text.count("skipped: name 'bad name'") == 1

    def test_failed_refresh_keeps_rows(self, tmp_path: Path) -> None:
        mapper = _mapper(tmp_path)
        (tmp_path / "catalog.parquet").unlink()
        report = mapper.refresh_lookups()
        assert "runs" in report["failed"]
        assert _ids(mapper, "perch", "8.0/detections") == ["d"]

    def test_unknown_name(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Unknown lookup"):
            _mapper(tmp_path).refresh_lookups(["nope"])

    def test_due(self, tmp_path: Path) -> None:
        mapper = _mapper(tmp_path, refresh="1h")
        assert mapper.lookups.due() == []
        assert mapper.refresh_due_lookups() is None
        assert 3500 < mapper.lookups.seconds_until_due() <= 3600
        assert mapper.lookups.due(now=time.time() + 3601) == ["runs"]


class TestEndpoint:
    """settings.lookups.refresh_route."""

    @pytest.fixture
    def client(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
        monkeypatch.setenv("LOOKUP_ADMIN_TOKEN", TOKEN)
        _setup(tmp_path, main={"settings": {"lookups": {
            "refresh_route": "/admin/lookups", "token": "env:LOOKUP_ADMIN_TOKEN"}}})
        with TestClient(fast_api.create_app(str(tmp_path / "config.yaml"))) as test_client:
            yield test_client

    def test_needs_token(self, client: TestClient) -> None:
        assert client.get("/admin/lookups").status_code == 401
        wrong = {"Authorization": "Bearer x"}
        assert client.post("/admin/lookups", headers=wrong).status_code == 401

    def test_status(self, client: TestClient) -> None:
        status = client.get("/admin/lookups", headers=AUTH).json()["lookups"]
        assert [(s["name"], s["kind"], s["rows"], s["error"]) for s in status] == [
            ("runs", "sql", 3, None)
        ]

    def test_refresh(self, client: TestClient, tmp_path: Path) -> None:
        _catalog(tmp_path, RUNS + [NEW_RUN])
        body = client.post("/admin/lookups", headers=AUTH).json()
        assert body["refreshed"] == ["runs"] and body["lookups"][0]["rows"] == 4
        assert client.get("/birdnet/").json() == {"versions": ["2.4", "3.0", "10.0"]}

    def test_refresh_by_name(self, client: TestClient) -> None:
        assert client.post("/admin/lookups?name=runs", headers=AUTH).json()["refreshed"] == ["runs"]
        response = client.post("/admin/lookups?name=nope", headers=AUTH)
        assert response.status_code == 404 and "Unknown lookup" in response.json()["error"]

    def test_off_by_default(self, tmp_path: Path) -> None:
        mapper = _mapper(tmp_path / "plain")
        assert mapper.lookup_endpoint_response("GET", f"Bearer {TOKEN}").status_code == 404

    def test_flask(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOOKUP_ADMIN_TOKEN", TOKEN)
        _setup(tmp_path, main={"settings": {"lookups": {
            "refresh_route": "admin/lookups", "token": "env:LOOKUP_ADMIN_TOKEN"}}})
        client = flask_api.create_app(str(tmp_path / "config.yaml")).test_client()
        assert client.get("/admin/lookups").status_code == 401
        assert client.get("/admin/lookups", headers=AUTH).get_json()["lookups"][0]["rows"] == 3
        _catalog(tmp_path, RUNS + [NEW_RUN])
        assert client.post("/admin/lookups", headers=AUTH).get_json()["refreshed"] == ["runs"]
        assert client.get("/birdnet/").get_json() == {"versions": ["2.4", "3.0", "10.0"]}


class TestBackgroundRefresh:
    """Lookups with ``refresh`` are re-run as they come due."""

    def test_fastapi(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(route_mapper, "MIN_REFRESH_WAIT", 0.05)
        _setup(tmp_path, refresh="0.2s")
        with TestClient(fast_api.create_app(str(tmp_path / "config.yaml"))) as client:
            _catalog(tmp_path, RUNS + [NEW_RUN])
            assert _wait(lambda: client.get("/birdnet/").json()["versions"][-1] == "10.0")

    def test_flask(self, tmp_path: Path) -> None:
        _setup(tmp_path, refresh="0.2s")
        app = flask_api.create_app(str(tmp_path / "config.yaml"))
        client = app.test_client()
        _catalog(tmp_path, RUNS + [NEW_RUN])
        time.sleep(0.3)
        assert _wait(lambda: client.get("/birdnet/").get_json()["versions"][-1] == "10.0")

    def test_no_refresh_no_task(self, tmp_path: Path) -> None:
        mapper = _mapper(tmp_path)

        async def run() -> None:
            await mapper.start()
            assert mapper._refresh_task is None
            await mapper.aclose()
        asyncio.run(run())


class TestRemoteVersions:
    """Remote versions generated by an HTTP lookup."""

    def test_generated_remote_versions(self, tmp_path: Path, http_server: Any) -> None:
        http_server.body = {"deployments": [
            {"version": "1.0", "url": f"{http_server.url}/v1"},
            {"version": "0.9", "url": f"{http_server.url}/v09"},
        ]}
        _write(tmp_path / "remotes" / "config.yaml", {
            "lookups": {"deploys": {"url": http_server.url, "path": "index",
                                    "rows": "deployments", "allow": [http_server.url + "/"]}},
            "remotes": {"wolf": {"description": "Wolves", "versions": {
                "from": "deploys", "version": "{{row.version}}", "url": "{{row.url}}"}}},
        })
        _write(tmp_path / "config.yaml", {"name": "t", "remotes": ["wolf"],
                                          "settings": {"add_trailing_slash": False}})
        mapper = RouteMapper(str(tmp_path / "config.yaml"))
        listing = asyncio.run(mapper.prepare_remote_request("wolf", "", "GET"))
        assert b'"0.9", "1.0"' in listing.content
        prepared = asyncio.run(mapper.prepare_remote_request("wolf", "latest/items", "GET"))
        assert isinstance(prepared, PreparedRequest)
        assert prepared.url == f"{http_server.url}/v1/items"


#
# INTERNAL
#
def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def _catalog(root: Path, runs: List[Tuple[str, str, str, str, List[str]]]) -> None:
    for _, _, _, run_id, ids in runs:
        folder = root / "runs" / run_id
        folder.mkdir(parents=True, exist_ok=True)
        values = ", ".join(f"('{i}', 1)" for i in ids)
        duckdb.sql(f"COPY (SELECT * FROM (VALUES {values}) t(id, recording_id)) "
                   f"TO '{folder / 'detections.parquet'}' (FORMAT parquet)")
    values = ", ".join(
        f"('{model}', '{version}', '{schema}', '{root / 'runs' / run_id / 'detections.parquet'}')"
        for model, version, schema, run_id, _ in runs
    )
    duckdb.sql(f"COPY (SELECT * FROM (VALUES {values}) t(model, version, schema, uri)) "
               f"TO '{root / 'catalog.parquet'}' (FORMAT parquet)")


def _setup(root: Path, main: Optional[Dict[str, Any]] = None, refresh: Any = None,
           required: bool = False, schema_by_name: bool = False,
           runs: Optional[List[Any]] = None) -> None:
    _catalog(root, RUNS if runs is None else runs)
    lookup: Dict[str, Any] = {
        "sql": "SELECT model, version, schema, uri FROM [[catalog]] ORDER BY model, version",
        "allow": [str(root / "runs") + "/"],
    }
    if refresh is not None:
        lookup["refresh"] = refresh
    if required:
        lookup["required"] = True
    if schema_by_name:
        # Versions name a static schema, so a row naming a missing one is invalid config.
        schema: Any = "{{row.schema}}"
        schemas = {name: {"detections": str(root / "runs" / run / "detections.parquet")}
                   for _, _, name, run, _ in RUNS}
    else:
        schema = {"name": "{{row.schema}}", "tables": {"detections": {"uri": "{{row.uri}}"}}}
        schemas = {}
    _write(root / "databases" / "config.yaml", {
        "lookups": {"runs": lookup},
        "database": {"catalog": str(root / "catalog.parquet"), "schema": schemas},
        "slugs": [{"from": "runs", "name": "{{row.model}}", "version": "{{row.version}}",
                   "schema": schema}],
        "routes": [
            {"route": "detections", "sql": "SELECT * FROM [[detections]] ORDER BY id"},
            {"route": "all", "source_columns": ["schema", "name", "version"],
             "sql": "SELECT d.* FROM [[*.detections]] d"},
        ],
    })
    _write(root / "config.yaml", {"name": "test", "databases": [{"from": "runs"}],
                                  **(main or {})})


def _mapper(root: Path, **kwargs: Any) -> RouteMapper:
    _setup(root, **kwargs)
    return RouteMapper(str(root / "config.yaml"))


def _get(mapper: RouteMapper, database: str, path: str) -> Any:
    import json
    response = asyncio.run(mapper.map_database_route(database, path, {}, {}))
    return json.loads(response.content)


def _ids(mapper: RouteMapper, database: str, path: str) -> List[str]:
    return [row["id"] for row in _get(mapper, database, path)]


def _wait(condition: Callable[[], bool], seconds: float = 5.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False

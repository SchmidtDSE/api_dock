"""

Tests for runtime settings and request handling.

Covers ``settings.duckdb`` (per-connection DuckDB options and
max_concurrent_queries), running database queries off the event loop,
``settings.base_path`` on the FastAPI and Flask apps, and the request headers
that are not forwarded to upstream remotes.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest
import yaml
from fastapi.testclient import TestClient

from api_dock import fast_api, flask_api
from api_dock.database_backends import parse_duckdb_settings
from api_dock.route_mapper import (
    _filter_request_headers,
    normalize_base_path,
    RouteMapper,
    strip_base_path,
)


#
# CONSTANTS
#
# A deliberately slow, dependency-free query (~0.5-2 s on one thread).
SLOW_SQL: str = "SELECT SUM(i * i) AS total FROM range(300000000) t(i)"


#
# PUBLIC
#
class TestBasePathHelpers:
    """normalize_base_path / strip_base_path."""

    @pytest.mark.parametrize("value,expected", [
        (None, None), ("", None), ("/", None),
        ("dock", "/dock"), ("/dock/", "/dock"), ("/a/b", "/a/b"),
    ])
    def test_normalize(self, value: Any, expected: Any) -> None:
        assert normalize_base_path(value) == expected

    @pytest.mark.parametrize("path,expected", [
        ("/dock/birdnet/latest/detections/", "/birdnet/latest/detections/"),
        ("/dock", "/"),
        ("/dock/", "/"),
        ("/birdnet/latest/", "/birdnet/latest/"),
        ("/docks/x", "/docks/x"),
    ])
    def test_strip(self, path: str, expected: str) -> None:
        assert strip_base_path(path, "/dock") == expected

    def test_strip_without_base_path(self) -> None:
        assert strip_base_path("/dock/x", None) == "/dock/x"


class TestDuckdbSettings:
    """``settings.duckdb`` parsing."""

    def test_statements_and_cap(self) -> None:
        statements, cap = parse_duckdb_settings({
            "memory_limit": "700MB", "threads": 2, "preserve_insertion_order": False,
            "temp_directory": "/tmp/it's", "max_concurrent_queries": 3,
        })
        assert statements == [
            "SET memory_limit = '700MB'",
            "SET threads = 2",
            "SET preserve_insertion_order = false",
            "SET temp_directory = '/tmp/it''s'",
        ]
        assert cap == 3

    def test_empty(self) -> None:
        assert parse_duckdb_settings(None) == ([], None)

    @pytest.mark.parametrize("options", [
        {"bad name": 1}, {"max_concurrent_queries": 0}, {"max_concurrent_queries": "2"}, ["x"],
    ])
    def test_invalid(self, options: Any) -> None:
        with pytest.raises(ValueError):
            parse_duckdb_settings(options)


class TestRequestHeaderFilter:
    """Headers not forwarded to remotes."""

    def test_drops_host_and_hop_by_hop(self) -> None:
        headers = {
            "Host": "proxy.example.com", "Connection": "keep-alive", "Content-Length": "3",
            "Accept": "application/json", "Content-Type": "application/json", "X-Custom": "1",
        }
        assert _filter_request_headers(headers) == {
            "Accept": "application/json", "Content-Type": "application/json", "X-Custom": "1",
        }

    @pytest.mark.anyio
    async def test_prepared_request_has_no_host(self) -> None:
        mapper = RouteMapper.__new__(RouteMapper)
        mapper.remote_names, mapper.database_names, mapper.settings = ["core"], [], {}
        mapper.config, mapper.config_dir = {"remotes": ["core"]}, "api_dock_config"
        with patch("api_dock.route_mapper.is_versioned_remote", return_value=False), \
             patch("api_dock.route_mapper.is_route_allowed", return_value=True), \
             patch("api_dock.route_mapper.find_remote_config",
                   return_value={"url": "https://api.example.com"}), \
             patch("api_dock.route_mapper.filter_remote_query_params",
                   side_effect=lambda qp, *a, **kw: qp), \
             patch("api_dock.route_mapper.find_route_mapping", return_value=None), \
             patch("api_dock.route_mapper.filter_cookies_by_config", return_value={}):
            prepared = await mapper.prepare_remote_request(
                "core", "detections/", "GET", headers={"host": "proxy:8000", "accept": "*/*"},
            )
        assert prepared.headers == {"accept": "*/*"}


class TestQueryExecution:
    """Database queries: DuckDB options, worker threads, concurrency cap."""

    @pytest.mark.anyio
    async def test_duckdb_options_applied(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        mapper = _make_mapper(tmp_path, monkeypatch, settings={
            "duckdb": {"threads": 1, "memory_limit": "300MB"},
        })
        result = await mapper.map_database_route("db", "settings")
        row = json.loads(result.content)[0]
        assert row["threads"] == "1"
        assert row["memory_limit"] != _default_duckdb_setting("memory_limit")

    @pytest.mark.anyio
    async def test_query_does_not_block_event_loop(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        mapper = _make_mapper(tmp_path, monkeypatch, settings={"duckdb": {"threads": 1}})
        gaps: List[float] = []

        async def ticker() -> None:
            """Record the gap between event-loop wakeups (large = loop was blocked)."""
            last = time.perf_counter()
            while True:
                await asyncio.sleep(0.01)
                now = time.perf_counter()
                gaps.append(now - last)
                last = now

        tick = asyncio.create_task(ticker())
        start = time.perf_counter()
        result = await mapper.map_database_route("db", "slow")
        duration = time.perf_counter() - start
        tick.cancel()

        assert result.status_code == 200, result.content
        assert duration > 0.2, 'query too fast to show blocking; raise SLOW_SQL range'
        assert max(gaps) < duration / 2

    @pytest.mark.anyio
    async def test_concurrency_cap_acquires_and_releases(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        mapper = _make_mapper(tmp_path, monkeypatch, settings={
            "duckdb": {"max_concurrent_queries": 1},
        })
        slots = MagicMock()
        mapper.duckdb_backend._slots = slots
        await mapper.map_database_route("db", "settings")
        slots.acquire.assert_called_once()
        slots.release.assert_called_once()

    @pytest.mark.anyio
    async def test_failed_query_releases_slot(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        mapper = _make_mapper(tmp_path, monkeypatch, settings={
            "duckdb": {"max_concurrent_queries": 1},
        })
        result = await mapper.map_database_route("db", "broken")
        assert result.status_code == 500
        assert mapper.duckdb_backend._slots.acquire(blocking=False)


class TestBasePathApps:
    """``settings.base_path`` on the FastAPI and Flask apps."""

    def test_fastapi(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_path = _write_config(tmp_path, monkeypatch, settings={"base_path": "/dock"})
        client = TestClient(fast_api.create_app(config_path))
        for prefix in ("", "/dock"):
            assert client.get(f"{prefix}/").json()["name"] == "t"
            assert client.get(f"{prefix}/databases").json() == ["db"]
            assert client.get(f"{prefix}/db/settings").status_code == 200
        assert client.get("/docks/db/settings").status_code == 404

    def test_flask(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_path = _write_config(tmp_path, monkeypatch, settings={"base_path": "dock"})
        client = flask_api.create_app(config_path).test_client()
        for prefix in ("", "/dock"):
            assert client.get(f"{prefix}/").get_json()["name"] == "t"
            assert client.get(f"{prefix}/databases").get_json() == ["db"]
            assert client.get(f"{prefix}/db/settings").status_code == 200


#
# INTERNAL
#
def _write_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                  settings: Dict[str, Any]) -> str:
    """Write a one-database config under tmp_path, chdir there, return its path."""
    config_dir = tmp_path / "api_dock_config"
    (config_dir / "databases").mkdir(parents=True)
    (config_dir / "config.yaml").write_text(yaml.safe_dump({
        "name": "t", "databases": ["db"], "expose": {"databases": True, "dict": False},
        "settings": settings,
    }))
    (config_dir / "databases" / "db.yaml").write_text(yaml.safe_dump({
        "name": "db",
        "routes": [
            {"route": "settings", "sql": "SELECT current_setting('threads')::VARCHAR AS threads, "
                                         "current_setting('memory_limit') AS memory_limit"},
            {"route": "slow", "sql": SLOW_SQL},
            {"route": "broken", "sql": "SELECT * FROM no_such_table"},
        ],
    }))
    monkeypatch.chdir(tmp_path)
    return str(config_dir / "config.yaml")


def _default_duckdb_setting(name: str) -> str:
    """A DuckDB setting's value on a fresh connection with no options applied."""
    import duckdb

    with duckdb.connect() as conn:
        return str(conn.execute(f"SELECT current_setting('{name}')").fetchone()[0])


def _make_mapper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                 settings: Dict[str, Any]) -> RouteMapper:
    """Build a RouteMapper over the one-database test config."""
    return RouteMapper(_write_config(tmp_path, monkeypatch, settings))

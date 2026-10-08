"""

Tests for fixes from the code review: remotes, startup and the CLI

Covers several upstream Set-Cookie headers staying separate (streaming,
buffered, Flask), ``{{route_name}}`` / ``{{cookies.x}}`` and query strings in
remote route mappings, generic error bodies for remote failures,
map_route_sync's event loop, the removed ``follow_protocol_downgrades``
setting, ``api-dock describe`` (expanded SQL, versioned databases, remotes)
and ``api-dock init`` (recursive copy, ``--force`` overwrites).

License: BSD 3-Clause

"""
#
# IMPORTS
#
import asyncio
import json
from pathlib import Path
from typing import Any, Dict

import duckdb
import pytest
import yaml
from click.testing import CliRunner
from fastapi.testclient import TestClient

from api_dock import fast_api, flask_api
from api_dock.cli import cli
from api_dock.config import DEFAULT_SETTINGS, find_route_mapping
from api_dock.route_mapper import RouteMapper
from api_dock.types import PreparedRequest


#
# CONSTANTS
#
SET_COOKIES = [("Set-Cookie", "a=1; Path=/"), ("Set-Cookie", "b=2; Path=/; HttpOnly")]


#
# PUBLIC
#
class TestSetCookie:
    """Several upstream Set-Cookie headers reach the client separately."""

    def test_streaming(self, tmp_path: Path, http_server: Any) -> None:
        http_server.headers = list(SET_COOKIES)
        with TestClient(fast_api.create_app(_remote(tmp_path, http_server.url))) as client:
            response = client.get("/svc/x")
        assert response.headers.get_list("set-cookie") == [v for _, v in SET_COOKIES]

    def test_buffered(self, tmp_path: Path, http_server: Any) -> None:
        http_server.headers = list(SET_COOKIES)
        mapper = RouteMapper(_remote(tmp_path, http_server.url))
        response = asyncio.run(mapper.map_route("svc", "x", "GET"))
        assert response.set_cookies == [v for _, v in SET_COOKIES]
        assert "set-cookie" not in {k.lower() for k in response.headers}

    def test_flask(self, tmp_path: Path, http_server: Any) -> None:
        http_server.headers = list(SET_COOKIES)
        client = flask_api.create_app(_remote(tmp_path, http_server.url)).test_client()
        response = client.get("/svc/x")
        assert response.headers.getlist("Set-Cookie") == [v for _, v in SET_COOKIES]


class TestRouteMapping:
    """remote_route substitution and query strings."""

    def test_route_name_and_cookie_values(self) -> None:
        config = {"routes": [{"route": "{{route_name}}/users/{{user_id}}",
                              "remote_route": "{{route_name}}/u/{{user_id}}/{{cookies.sid}}"}]}
        assert find_route_mapping("svc/users/42", "GET", config, "svc", {"sid": "abc"}) == \
            "svc/u/42/abc"

    def test_single_braces_and_reordering(self) -> None:
        config = {"routes": [{"route": "svc/search/{category}/{term}",
                              "remote_route": "api/{{term}}/in/{category}"}]}
        assert find_route_mapping("svc/search/birds/owl", "GET", config, "svc") == \
            "api/owl/in/birds"

    def test_values_are_not_substituted_again(self) -> None:
        config = {"routes": [{"route": "svc/x/{{a}}", "remote_route": "y/{{a}}/{{b}}"}]}
        assert find_route_mapping("svc/x/{{b}}", "GET", config, "svc") == "y/{{b}}/{{b}}"

    def test_query_string_in_remote_route(self, tmp_path: Path) -> None:
        routes = ["legacy", {"route": "svc/legacy", "remote_route": "v2/items?kind=a&kind=b"}]
        mapper = RouteMapper(_remote(tmp_path, "https://example.org", routes=routes,
                                     trailing_slash=True))
        prepared = asyncio.run(mapper.prepare_remote_request(
            "svc", "legacy", "GET", query_params={"page": "2", "kind": "c"}
        ))
        assert isinstance(prepared, PreparedRequest)
        assert prepared.url == "https://example.org/v2/items/"
        assert prepared.params == {"kind": ["a", "b", "c"], "page": "2"}


class TestErrors:
    """Remote failures don't send internal details to clients."""

    def test_unreachable_remote(self, tmp_path: Path) -> None:
        mapper = RouteMapper(_remote(tmp_path, "http://127.0.0.1:1"))
        response = asyncio.run(mapper.map_route("svc", "x", "GET"))
        assert response.status_code == 502
        assert json.loads(response.content) == {"error": "Error connecting to remote API"}

    def test_map_route_sync(self, tmp_path: Path, http_server: Any) -> None:
        mapper = RouteMapper(_remote(tmp_path, http_server.url))
        assert mapper.map_route_sync("svc", "x", "GET").status_code == 200
        assert mapper.map_route_sync("svc", "x", "GET").status_code == 200

    def test_map_route_sync_inside_a_running_loop(self, tmp_path: Path) -> None:
        mapper = RouteMapper(_remote(tmp_path, "https://example.org"))

        async def call() -> int:
            return mapper.map_route_sync("svc", "x", "GET").status_code
        assert asyncio.run(call()) == 500

    def test_follow_protocol_downgrades_removed(self) -> None:
        assert "follow_protocol_downgrades" not in DEFAULT_SETTINGS


class TestDescribe:
    """api-dock describe."""

    def test_expanded_sql_versions_and_remotes(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        root = tmp_path / "api_dock_config"
        duckdb.sql(f"COPY (SELECT 1 AS id) TO '{tmp_path / 't.parquet'}' (FORMAT parquet)")
        for version in ("1.0", "2.0"):
            _write(root / "databases" / "db" / f"{version}.yaml", {
                "tables": {"t": str(tmp_path / "t.parquet")},
                "routes": [{"route": "items/{{id}}",
                            "sql": "SELECT * FROM [[t]] WHERE id = {{id}}"}],
            })
        _write(root / "remotes" / "svc.yaml", {"name": "svc", "url": "https://example.org"})
        _write(root / "config.yaml", {"name": "demo", "databases": ["db"], "remotes": ["svc"]})
        result = CliRunner().invoke(cli, ["describe"])
        assert result.exit_code == 0, result.output
        assert "db/1.0:" in result.output and "db/2.0:" in result.output
        assert f"SELECT * FROM '{tmp_path / 't.parquet'}' AS t WHERE id = ?" in result.output
        assert "svc: https://example.org" in result.output

    def test_invalid_config_exits(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        _write(tmp_path / "api_dock_config" / "config.yaml", {"name": "x", "databases": ["nope"]})
        result = CliRunner().invoke(cli, ["describe"])
        assert result.exit_code == 1 and "Error loading configuration" in result.output


class TestInit:
    """api-dock init."""

    def test_copies_everything_and_force_overwrites(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        assert runner.invoke(cli, ["init"]).exit_code == 0
        root = tmp_path / "api_dock_config"
        for path in ("config.yaml", "databases/config.yaml", "remotes/config.yaml"):
            assert (root / path).exists()

        (root / "config.yaml").write_text("# my edit\n")
        assert runner.invoke(cli, ["init"]).exit_code == 1          # refuses without --force
        result = runner.invoke(cli, ["init", "--force"])
        assert result.exit_code == 0 and "✓ api_dock_config/config.yaml" in result.output
        assert (root / "config.yaml").read_text() != "# my edit\n"


#
# INTERNAL
#
def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data))


def _remote(root: Path, url: str, routes: Any = None, trailing_slash: bool = False) -> str:
    remote: Dict[str, Any] = {"name": "svc", "url": url}
    if routes is not None:
        remote["routes"] = routes
    _write(root / "remotes" / "svc.yaml", remote)
    _write(root / "config.yaml", {"name": "t", "remotes": ["svc"],
                                  "settings": {"add_trailing_slash": trailing_slash}})
    return str(root / "config.yaml")

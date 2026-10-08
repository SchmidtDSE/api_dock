"""

Tests for the ``api-dock lookups`` command

Local runs print each lookup's rows; ``--url`` calls a server's lookup
endpoint (here the ``http_server`` fixture) for its status or a refresh.

License: BSD 3-Clause

"""
#
# IMPORTS
#
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml
from click.testing import CliRunner

from api_dock.cli import cli


#
# PUBLIC
#
class TestLocal:
    """Running lookups from the config in the current directory."""

    @pytest.fixture(autouse=True)
    def config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        duckdb.sql(f"COPY (SELECT range AS n FROM range(7)) TO '{tmp_path / 'n.parquet'}' "
                   "(FORMAT parquet)")
        _write(tmp_path / "api_dock_config" / "config.yaml", {"name": "t"})
        _write(tmp_path / "api_dock_config" / "databases" / "config.yaml", {
            "database": {"numbers": str(tmp_path / "n.parquet")},
            "lookups": {
                "numbers": {"sql": "SELECT n FROM [[numbers]] ORDER BY n"},
                "broken": {"sql": "SELECT * FROM [[missing]]"},
            },
        })

    def test_runs_and_prints_rows(self) -> None:
        result = CliRunner().invoke(cli, ["lookups", "--name", "numbers", "--rows", "2"])
        assert result.exit_code == 0, result.output
        assert "== numbers (sql" in result.output and "7 row(s)" in result.output
        assert '{"n": 0}' in result.output and '{"n": 1}' in result.output
        assert '{"n": 2}' not in result.output and "... 5 more" in result.output

    def test_failure_exit_code(self) -> None:
        result = CliRunner().invoke(cli, ["lookups"])
        assert result.exit_code == 1
        assert "== broken" in result.output and "failed:" in result.output

    def test_unknown_name(self) -> None:
        result = CliRunner().invoke(cli, ["lookups", "--name", "nope"])
        assert result.exit_code == 1 and "unknown lookup" in result.output

    def test_refresh_needs_url(self) -> None:
        assert CliRunner().invoke(cli, ["lookups", "--refresh"]).exit_code == 2


class TestServer:
    """``--url``: the server's lookup endpoint."""

    def test_status_and_refresh(self, http_server: Any) -> None:
        http_server.body = {"lookups": [{"name": "runs", "rows": 3}]}
        url = f"{http_server.url}/admin/lookups"
        result = CliRunner().invoke(cli, ["lookups", "--url", url, "--token", "t"])
        assert result.exit_code == 0 and '"rows": 3' in result.output
        assert http_server.requests[-1]["headers"]["Authorization"] == "Bearer t"

    def test_token_from_env(self, http_server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("API_DOCK_ADMIN_TOKEN", "from-env")
        CliRunner().invoke(cli, ["lookups", "--url", http_server.url])
        assert http_server.requests[-1]["headers"]["Authorization"] == "Bearer from-env"

    def test_error_status(self, http_server: Any) -> None:
        http_server.status, http_server.body = 401, {"error": "Unauthorized"}
        result = CliRunner().invoke(cli, ["lookups", "--url", http_server.url, "--token", "x"])
        assert result.exit_code == 1 and "Unauthorized" in result.output

    def test_needs_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("API_DOCK_ADMIN_TOKEN", raising=False)
        result = CliRunner().invoke(cli, ["lookups", "--url", "http://127.0.0.1:1"])
        assert result.exit_code == 1 and "--token" in result.output


#
# INTERNAL
#
def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data))

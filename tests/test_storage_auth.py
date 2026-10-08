"""

Tests for storage credentials: quoting in DuckDB secret SQL and the GCS service account

Values from table definitions (S3 region, GCS keys and endpoint, HTTP headers)
are written into ``CREATE SECRET`` statements as quoted literals, so a quote
in a value can't break the statement. The GCS service account is process-wide
in DuckDB, so a second, different one is ignored with a warning.

License: BSD 3-Clause

"""
#
# IMPORTS
#
from typing import Any

import duckdb
import pytest

from api_dock import storage_auth
from api_dock.storage_auth import (
    _s3_secret_sql,
    _setup_gcs_auth,
    _setup_http_support,
    _sql_string,
    GOOGLE_CREDENTIALS_ENV,
)


#
# PUBLIC
#
class TestQuoting:
    """Values are written as escaped string literals."""

    @pytest.mark.parametrize("value, literal", [
        ("plain", "'plain'"), ("it's", "'it''s'"), ("a'); DROP x; --", "'a''); DROP x; --'"),
        (42, "'42'"),
    ])
    def test_sql_string(self, value: Any, literal: str) -> None:
        assert _sql_string(value) == literal

    def test_http_headers_with_quotes(self) -> None:
        conn = duckdb.connect()
        metadata = {"bearer_token": "tok'en", "auth_headers": {"X-Note": "it's fine"},
                    "cookies": {"s": "a'b"}}
        assert _setup_http_support(conn, metadata)
        secrets = conn.execute("SELECT name, type FROM duckdb_secrets()").fetchall()
        assert ("http_auth", "http") in secrets

    def test_gcs_keys_with_quotes(self) -> None:
        conn = duckdb.connect()
        assert _setup_gcs_auth(conn, {"key_id": "k'1", "secret": "s'2",
                                      "endpoint": "storage.example.org"})
        assert conn.execute("SELECT type FROM duckdb_secrets()").fetchall() == [("gcs",)]

    def test_s3_region_with_quote(self) -> None:
        sql = _s3_secret_sql([f"REGION {_sql_string('us-west-2')}"], "s1", "s3://b/x'y")
        assert "REGION 'us-west-2'" in sql and "SCOPE 's3://b/x''y'" in sql


class TestGcsServiceAccount:
    """The service account is set once per process."""

    def test_set_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(GOOGLE_CREDENTIALS_ENV, raising=False)
        _setup_gcs_auth(duckdb.connect(), {"service_account": "/keys/a.json", "public": True})
        assert storage_auth.os.environ[GOOGLE_CREDENTIALS_ENV] == "/keys/a.json"

    def test_different_one_ignored_with_warning(
            self, monkeypatch: pytest.MonkeyPatch, caplog: Any) -> None:
        monkeypatch.setenv(GOOGLE_CREDENTIALS_ENV, "/keys/a.json")
        with caplog.at_level("WARNING"):
            _setup_gcs_auth(duckdb.connect(), {"service_account": "/keys/b.json", "public": True})
        assert storage_auth.os.environ[GOOGLE_CREDENTIALS_ENV] == "/keys/a.json"
        assert "/keys/b.json ignored" in caplog.text

"""

Tests for templated tables.

A DuckDB table whose ``uri:`` is one ``{{<resolver>.<column>}}`` is read with
the reader that ``format:`` names. Its URI is a bound value in marker order.
Before binding, the URI is checked against ``allow:``; ``files:`` is then
added to its end.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from api_dock.database_backends import DuckDBBackend
from api_dock.route_mapper import RouteMapper
from api_dock.sql_builder import build_sql_query
from api_dock.storage_auth import (
    detect_required_backends,
    extract_table_metadata_by_backend,
    extract_table_uris,
    setup_storage_authentication,
)
from api_dock.templated_tables import TableUriError, bound_uri
from tests.chaining_fixtures import (
    RESOLVER_ERROR,
    body,
    catalog,
    chain,
    default_releases,
    get,
    observations,
    release_row,
    write_parquet,
    write_releases,
)
from tests.conftest import write_config


#
# CONSTANTS
#
READINGS_PATH: str = "2.0/projects/7/sites/42/readings"

ITEMS: List[Dict[str, Any]] = [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]

ROUTE_LOGGER: str = "api_dock.route_mapper"


#
# FIXTURES
#
def _table_chain(root: Path, uri: Optional[str], table: Dict[str, Any],
                 route_sql: str = "SELECT * FROM [[t]] ORDER BY id") -> str:
    """Write a chain whose resolver returns uri and whose route reads table ``t``.

    Args:
        root: Test folder; it is the table's allowed prefix unless table sets allow.
        uri: URI the target returns as column ``u``; None for NULL.
        table: Table keys besides uri; allow defaults to the test folder.
        route_sql: SQL of the shop route.

    Returns:
        Path of config.yaml.
    """
    definition = {"uri": "{{res.u}}", "allow": [f"{root}/"], **table}
    target_sql = f"SELECT '{uri}' AS u" if uri is not None else "SELECT NULL AS u"
    return chain(root, target_sql, ["u"], route_sql, shop={"tables": {"t": definition}})


def _release_uri_scenario(root: Path, data_uri: str) -> str:
    """Write the scenario with the hourly release's data_uri replaced.

    Args:
        root: Test folder.
        data_uri: The hourly release's data URI.

    Returns:
        Path of config.yaml.
    """
    rows = default_releases(root)
    rows[0] = release_row(root, "project:7", "hourly", "r-19", data_uri)
    releases_csv = write_releases(root, rows)
    return write_config(root, {
        "catalog": {"versions": {"1.0": catalog(releases_csv)}},
        "observations": {"versions": {"2.0": observations(root)}},
    })


class FakeConnection:
    """Records the SQL that storage setup sends, without running it."""

    def __init__(self) -> None:
        """Start with no statements."""
        self.statements: List[str] = []

    def execute(self, sql: str, *args: Any) -> "FakeConnection":
        """Record one statement.

        Args:
            sql: SQL text.
            *args: Ignored values.

        Returns:
            This connection.
        """
        self.statements.append(sql)
        return self


#
# PUBLIC
#
class TestReaders:
    """format: selects the reader; the file name does not."""

    @pytest.mark.anyio
    async def test_parquet_file(self, tmp_path: Path) -> None:
        """Without files:, the URI is one Parquet file."""
        write_parquet(tmp_path / "items.data", ITEMS)
        config_path = _table_chain(tmp_path, f"{tmp_path}/items.data", {"format": "parquet"})
        assert body(await get(RouteMapper(config_path), "shop", "1.0/items/1")) == ITEMS

    @pytest.mark.anyio
    async def test_parquet_wildcard(self, tmp_path: Path) -> None:
        """files: adds a pattern to the folder URI."""
        write_parquet(tmp_path / "data" / "a" / "part-0.parquet", ITEMS[:1])
        write_parquet(tmp_path / "data" / "b" / "part-0.parquet", ITEMS[1:])
        config_path = _table_chain(
            tmp_path, f"{tmp_path}/data", {"format": "parquet", "files": "**/*.parquet"}
        )
        assert body(await get(RouteMapper(config_path), "shop", "1.0/items/1")) == ITEMS

    @pytest.mark.anyio
    async def test_parquet_hive_partitions(self, tmp_path: Path) -> None:
        """A hive-partitioned folder returns its partition columns."""
        write_parquet(tmp_path / "data" / "part=x" / "f.parquet", ITEMS[:1])
        write_parquet(tmp_path / "data" / "part=y" / "f.parquet", ITEMS[1:])
        config_path = _table_chain(
            tmp_path, f"{tmp_path}/data/", {"format": "parquet", "files": "**/*.parquet"}
        )
        assert body(await get(RouteMapper(config_path), "shop", "1.0/items/1")) == [
            {"id": 1, "name": "a", "part": "x"}, {"id": 2, "name": "b", "part": "y"},
        ]

    @pytest.mark.anyio
    async def test_csv(self, tmp_path: Path) -> None:
        """format: csv reads a file whose name has another extension."""
        (tmp_path / "items.txt").write_text("id,name\n1,a\n2,b\n")
        config_path = _table_chain(tmp_path, f"{tmp_path}/items.txt", {"format": "csv"})
        assert body(await get(RouteMapper(config_path), "shop", "1.0/items/1")) == ITEMS

    @pytest.mark.anyio
    async def test_csv_wildcard(self, tmp_path: Path) -> None:
        """format: csv reads a bound wildcard path."""
        (tmp_path / "csv").mkdir()
        (tmp_path / "csv" / "a.dat").write_text("id,name\n1,a\n")
        (tmp_path / "csv" / "b.dat").write_text("id,name\n2,b\n")
        config_path = _table_chain(
            tmp_path, f"{tmp_path}/csv", {"format": "csv", "files": "*.dat"}
        )
        assert body(await get(RouteMapper(config_path), "shop", "1.0/items/1")) == ITEMS

    @pytest.mark.parametrize("text", [
        json.dumps(ITEMS), "\n".join(json.dumps(item) for item in ITEMS),
    ])
    @pytest.mark.anyio
    async def test_json(self, tmp_path: Path, text: str) -> None:
        """format: json reads a JSON array and newline-delimited JSON."""
        (tmp_path / "items.data").write_text(text)
        config_path = _table_chain(tmp_path, f"{tmp_path}/items.data", {"format": "json"})
        assert body(await get(RouteMapper(config_path), "shop", "1.0/items/1")) == ITEMS

    @pytest.mark.anyio
    async def test_json_wildcard(self, tmp_path: Path) -> None:
        """format: json reads a bound wildcard path."""
        (tmp_path / "j").mkdir()
        (tmp_path / "j" / "a.data").write_text(json.dumps(ITEMS[:1]))
        (tmp_path / "j" / "b.data").write_text(json.dumps(ITEMS[1:]))
        config_path = _table_chain(tmp_path, f"{tmp_path}/j/", {"format": "json", "files": "*"})
        assert body(await get(RouteMapper(config_path), "shop", "1.0/items/1")) == ITEMS


class TestUriChecks:
    """A resolved URI that fails a check is not read."""

    @pytest.mark.parametrize("uri", [
        "s3://bucket/pub", "s3://bucket/pub/", "s3://bucket/pub/x/y.parquet",
    ])
    def test_allowed(self, uri: str) -> None:
        """The URI equals a prefix, or continues it at a slash."""
        assert bound_uri("t", {"uri": "{{r.u}}", "allow": ["s3://bucket/pub"]}, uri) == uri

    def test_prefix_with_slash(self) -> None:
        """A prefix that ends with a slash matches any URI that starts with it."""
        table = {"uri": "{{r.u}}", "allow": ["s3://bucket/pu/"]}
        assert bound_uri("t", table, "s3://bucket/pu/x") == "s3://bucket/pu/x"

    @pytest.mark.parametrize("uri, reason", [
        ("s3://bucket/pub-secret/x", "matches no allow: prefix"),
        ("s3://other/pub/x", "matches no allow: prefix"),
        ("s3://bucket/pu", "matches no allow: prefix"),
        ("s3://bucket/pub/../private/", "'..' segment"),
        ("s3://bucket/pub/..", "'..' segment"),
        ("s3://bucket/pub/%2e%2e/private/", "'..' segment"),
        ("s3://bucket/pub/%2E%2E/private/", "'..' segment"),
        ("s3://bucket/pub/*.parquet", "wildcard"),
        ("s3://bucket/pub/x?.parquet", "wildcard"),
        ("s3://bucket/pub/[ab].parquet", "wildcard"),
    ])
    def test_refused(self, uri: str, reason: str) -> None:
        """Each check refuses the URI on its own."""
        with pytest.raises(TableUriError, match=reason):
            bound_uri("t", {"uri": "{{r.u}}", "allow": ["s3://bucket/pub"]}, uri)

    def test_two_dots_inside_a_name_are_allowed(self) -> None:
        """Only a whole '..' segment is refused."""
        table = {"uri": "{{r.u}}", "allow": ["s3://bucket/pub/"]}
        assert bound_uri("t", table, "s3://bucket/pub/a..b") == "s3://bucket/pub/a..b"

    @pytest.mark.parametrize("uri, expected", [
        ("s3://bucket/pub/r-1", "s3://bucket/pub/r-1/**/*.parquet"),
        ("s3://bucket/pub/r-1/", "s3://bucket/pub/r-1/**/*.parquet"),
    ])
    def test_files_adds_one_slash(self, uri: str, expected: str) -> None:
        """files: is added after one slash; a URI that ends with one gets no second."""
        table = {"uri": "{{r.u}}", "allow": ["s3://bucket/pub/"], "files": "**/*.parquet"}
        assert bound_uri("t", table, uri) == expected

    def test_checks_come_before_files(self) -> None:
        """The wildcard in files: is not refused; the check uses the URI without it."""
        table = {"uri": "{{r.u}}", "allow": ["s3://bucket/pub"], "files": "*.csv"}
        assert bound_uri("t", table, "s3://bucket/pub") == "s3://bucket/pub/*.csv"

    @pytest.mark.parametrize("data_uri", ["../private/", "%2e%2e/private/", "*/"])
    @pytest.mark.anyio
    async def test_request_refused(
            self, tmp_path: Path, caplog: pytest.LogCaptureFixture, data_uri: str) -> None:
        """A refused URI gives 500 Resolver error; the log names the resolver and the check."""
        config_path = _release_uri_scenario(tmp_path, f"{tmp_path}/releases/{data_uri}")
        with caplog.at_level(logging.WARNING, logger=ROUTE_LOGGER):
            response = await get(RouteMapper(config_path), "observations", READINGS_PATH)
        assert (response.status_code, body(response)) == (500, RESOLVER_ERROR)
        assert "resolver 'current_release'" in caplog.text
        assert "catalog/1.0/releases/current" in caplog.text
        assert "table 'readings'" in caplog.text

    @pytest.mark.anyio
    async def test_other_bucket_refused(self, tmp_path: Path) -> None:
        """A URI outside every allow: prefix is not read."""
        config_path = _release_uri_scenario(tmp_path, "s3://other-bucket/x/")
        response = await get(RouteMapper(config_path), "observations", READINGS_PATH)
        assert (response.status_code, body(response)) == (500, RESOLVER_ERROR)

    @pytest.mark.anyio
    async def test_sibling_folder_refused(self, tmp_path: Path) -> None:
        """A sibling folder whose name starts with the prefix text is not read."""
        write_parquet(tmp_path / "releases-secret" / "f.parquet", ITEMS)
        config_path = _release_uri_scenario(tmp_path, f"{tmp_path}/releases-secret/")
        response = await get(RouteMapper(config_path), "observations", READINGS_PATH)
        assert (response.status_code, body(response)) == (500, RESOLVER_ERROR)

    @pytest.mark.anyio
    async def test_exact_prefix_accepted(self, tmp_path: Path) -> None:
        """A URI that equals an allow: prefix is read."""
        write_parquet(tmp_path / "one.parquet", ITEMS)
        config_path = _table_chain(
            tmp_path, f"{tmp_path}/one.parquet",
            {"format": "parquet", "allow": [f"{tmp_path}/one.parquet"]},
        )
        assert body(await get(RouteMapper(config_path), "shop", "1.0/items/1")) == ITEMS

    @pytest.mark.anyio
    async def test_null_uri(self, tmp_path: Path) -> None:
        """A NULL URI gives 500 Resolver error."""
        config_path = _table_chain(tmp_path, None, {"format": "parquet"})
        response = await get(RouteMapper(config_path), "shop", "1.0/items/1")
        assert (response.status_code, body(response)) == (500, RESOLVER_ERROR)


class TestMarkerOrder:
    """The bound URI takes its place among the other values, in marker order."""

    RESOLVED: Dict[str, Optional[str]] = {"res.u": "/data/"}

    # Table names are long enough that FROM is not in the 20 characters
    # before a later [[table]].column reference.
    DATABASE: Dict[str, Any] = {"tables": {
        "readings": {
            "uri": "{{res.u}}", "format": "parquet", "allow": ["/data/"], "files": "*.parquet",
        },
        "readings_csv": {"uri": "{{res.u}}", "format": "csv", "allow": ["/data/"]},
    }}

    def _build(self, sql: str, **query: str) -> Any:
        """Build a route's SQL with the resolver value ``res.u``.

        Args:
            sql: Route SQL.
            **query: Query params.

        Returns:
            Tuple of SQL text and values.
        """
        route = {"route": "x/{{id}}", "resolve": ["res"], "sql": sql}
        return build_sql_query(
            route, self.DATABASE, {"id": "42"}, query, resolved=self.RESOLVED,
        )

    def test_select_value_then_source_then_where(self) -> None:
        """A SELECT value comes before the table source and a WHERE value after it."""
        sql, values = self._build(
            "SELECT {{label}} AS label, [[readings]].id FROM [[readings]] "
            "WHERE [[readings]].id = {{id}}",
            label="L",
        )
        assert sql == (
            "SELECT ? AS label, readings.id FROM read_parquet(?) AS readings "
            "WHERE readings.id = ?"
        )
        assert values == ["L", "/data/*.parquet", "42"]

    def test_two_scopes(self) -> None:
        """A table in two subquery scopes binds its URI twice, with values between."""
        sql, values = self._build(
            "SELECT a.n, b.m FROM "
            "(SELECT count(*) AS n FROM [[readings]] WHERE [[readings]].id > {{low}}) a, "
            "(SELECT count(*) AS m FROM [[readings]] WHERE [[readings]].id > {{high}}) b",
            low="1", high="2",
        )
        assert sql.count("read_parquet(?) AS readings") == 2
        assert values == ["/data/*.parquet", "1", "/data/*.parquet", "2"]

    def test_self_join(self) -> None:
        """Two table names with the same URI keep their own aliases and readers."""
        sql, values = self._build("SELECT * FROM [[readings]] JOIN [[readings_csv]] USING (id)")
        assert sql == (
            "SELECT * FROM read_parquet(?) AS readings JOIN read_csv(?) AS readings_csv "
            "USING (id)"
        )
        assert values == ["/data/*.parquet", "/data/"]

    def test_where_fragment(self) -> None:
        """A table source in a query-param fragment binds its URI in the fragment."""
        route = {
            "route": "x/{{id}}", "resolve": ["res"],
            "sql": "SELECT {{id}} AS id",
            "query_params": [{"in_t": {"sql": "{{in_t}} IN (SELECT id FROM [[readings]])"}}],
        }
        sql, values = build_sql_query(
            route, self.DATABASE, {"id": "42"}, {"in_t": "7"}, resolved=self.RESOLVED,
        )
        assert sql == "SELECT ? AS id WHERE ? IN (SELECT id FROM read_parquet(?) AS readings)"
        assert values == ["42", "7", "/data/*.parquet"]

    @pytest.mark.anyio
    async def test_two_scopes_request(self, tmp_path: Path) -> None:
        """Two scopes of one table give the right counts."""
        write_parquet(tmp_path / "items.parquet", ITEMS)
        route_sql = (
            "SELECT a.n, b.m, {{id}} AS id FROM "
            "(SELECT count(*) AS n FROM [[t]] WHERE id >= {{low}}) a, "
            "(SELECT count(*) AS m FROM [[t]] WHERE id >= {{high}}) b"
        )
        config_path = _table_chain(
            tmp_path, f"{tmp_path}/items.parquet", {"format": "parquet"}, route_sql
        )
        response = await get(RouteMapper(config_path), "shop", "1.0/items/9", low="1", high="2")
        assert body(response) == [{"n": 1 + 1, "m": 1, "id": "9"}]

    @pytest.mark.anyio
    async def test_self_join_request(self, tmp_path: Path) -> None:
        """A self-join of two table names with the same URI returns joined rows."""
        write_parquet(tmp_path / "items.parquet", ITEMS)
        table = {"uri": "{{res.u}}", "format": "parquet", "allow": [f"{tmp_path}/"]}
        config_path = chain(
            tmp_path, f"SELECT '{tmp_path}/items.parquet' AS u", ["u"],
            "SELECT [[a]].id, [[b]].name FROM [[a]] JOIN [[b]] USING (id) ORDER BY id",
            shop={"tables": {"a": table, "b": table}},
        )
        assert body(await get(RouteMapper(config_path), "shop", "1.0/items/1")) == ITEMS


class TestStorageSetup:
    """Storage access for a templated table uses its allow: prefixes."""

    CONFIG: Dict[str, Any] = {"tables": {"readings": {
        "uri": "{{current_release.data_uri}}", "format": "parquet", "files": "**/*.parquet",
        "allow": ["s3://example-releases/"], "region": "us-west-2",
    }}}

    def test_uris_are_prefixes(self) -> None:
        """extract_table_uris gives the allow: prefixes, not the template."""
        assert extract_table_uris(self.CONFIG) == ["s3://example-releases/"]

    def test_metadata(self) -> None:
        """Metadata is grouped under the prefixes' storage type, without table keys."""
        assert extract_table_metadata_by_backend(self.CONFIG) == {"s3": {"region": "us-west-2"}}

    def test_setup_uses_region(self) -> None:
        """S3 setup runs with the table's region; no network is used."""
        connection = FakeConnection()
        backends = detect_required_backends(extract_table_uris(self.CONFIG))
        setup_storage_authentication(
            connection, backends, extract_table_metadata_by_backend(self.CONFIG)
        )
        assert backends == {"s3"}
        assert any("REGION 'us-west-2'" in statement for statement in connection.statements)

    def test_backend_setup(self) -> None:
        """The DuckDB backend sets up S3 for a templated S3 table."""
        connection = FakeConnection()
        DuckDBBackend(self.CONFIG)._setup_storage(connection)
        assert "LOAD aws;" in connection.statements

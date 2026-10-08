"""

Tests for where query-param conditions and table sources go in the SQL

Query-param conditions join the base query's top-level WHERE (not one inside a
CTE, subquery, string or comment), with the existing condition parenthesized,
and go before a trailing GROUP BY / ORDER BY / LIMIT. ``[[table]]`` becomes a
table source right after FROM/JOIN or a FROM-list comma. ``sql_append`` values
are identifiers or integers only, and ``action`` is refused. Queries run on
DuckDB over local Parquet, so the checks are on results.

License: BSD 3-Clause

"""
#
# IMPORTS
#
import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List

import duckdb
import pytest
import yaml

from api_dock.database_config import check_database_config
from api_dock.route_mapper import RouteMapper
from api_dock.sql_builder import _insert_where_condition, _sanitize_sql_identifier


#
# CONSTANTS
#
ROWS: str = "(1, 'a', 10), (2, 'b', 20), (3, 'a', 30), (4, 'c', 40)"


#
# PUBLIC
#
class TestWherePlacement:
    """Conditions from query params, end to end."""

    @pytest.mark.parametrize("sql, params, expected", [
        # no WHERE in the base query
        ("SELECT * FROM [[t]]", {"kind": "a"}, [1, 3]),
        # the base condition is kept whole: (x OR y) AND kind; without the parentheses
        # id = 2 (kind 'b') would get through
        ("SELECT * FROM [[t]] WHERE id = 2 OR id = 1", {"kind": "a"}, [1]),
        # a WHERE only inside a CTE: the outer query gets its own WHERE
        ("WITH big AS (SELECT * FROM [[t]] WHERE n > 15) SELECT * FROM big t", {"kind": "a"}, [3]),
        # ... and inside a subquery
        ("SELECT * FROM (SELECT * FROM [[t]] WHERE n > 15) t", {"kind": "a"}, [3]),
        # 'where' inside a string literal or a comment isn't a WHERE
        ("SELECT * /* where */ FROM [[t]] WHERE kind <> 'x where y'", {"kind": "a"}, [1, 3]),
        # trailing ORDER BY / LIMIT in the base query: the condition goes before them
        ("SELECT * FROM [[t]] ORDER BY id DESC LIMIT 1", {"kind": "a"}, [3]),
        ("SELECT * FROM [[t]] WHERE n < 35 ORDER BY id DESC", {"kind": "a"}, [3, 1]),
    ])
    def test_results(self, tmp_path: Path, sql: str, params: Dict[str, str],
                     expected: List[int]) -> None:
        assert _ids(_query(tmp_path, sql, params)) == expected

    def test_uri_containing_where(self, tmp_path: Path) -> None:
        folder = tmp_path / "somewhere"
        rows = _query(tmp_path, "SELECT * FROM [[t]]", {"kind": "a"}, folder=folder)
        assert _ids(rows) == [1, 3]

    def test_values_keep_marker_order(self, tmp_path: Path) -> None:
        sql = "SELECT * FROM [[t]] WHERE n > {{min}} ORDER BY id LIMIT {{top}}"
        rows = _query(tmp_path, sql, {"kind": "a", "min": "5", "top": "1"},
                      extra_params=[{"min": {"default": "0"}}, {"top": {"default": "10"}}])
        assert _ids(rows) == [1]

    def test_insert_where_condition(self) -> None:
        sql, tail = _insert_where_condition(
            "SELECT * FROM t WHERE a = ? OR b = ? GROUP BY c HAVING COUNT(*) > ? LIMIT 5",
            "(d = ?)", "?",
        )
        assert sql == ("SELECT * FROM t WHERE (a = ? OR b = ?) AND (d = ?) "
                       "GROUP BY c HAVING COUNT(*) > ? LIMIT 5")
        assert tail == 1


class TestTableSources:
    """Which [[table]] references become table sources."""

    @pytest.mark.parametrize("sql", [
        "SELECT [[a]].id, [[b]].v FROM [[a]] JOIN [[b]] ON [[a]].id = [[b]].id",
        "SELECT a.id FROM [[a]] WHERE [[a]].id = 1",
        "SELECT x.from_ts, [[a]].id FROM [[a]] CROSS JOIN (SELECT 1 AS from_ts) x",
        "SELECT a.id FROM\n                          [[a]]",
    ])
    def test_short_names_and_spacing(self, tmp_path: Path, sql: str) -> None:
        rows = _two_tables(tmp_path, sql)
        assert rows and "error" not in rows

    def test_comma_from_list(self, tmp_path: Path) -> None:
        rows = _two_tables(tmp_path, "SELECT a.id, b.v FROM [[s.a]] a, [[s.b]] b WHERE a.id = b.id",
                           schema=True)
        assert rows == [{"id": 1, "v": "x"}]


class TestAppendValues:
    """sql_append values are identifiers or integers."""

    @pytest.mark.parametrize("value", [
        "confidence", "d.confidence DESC", "a, b DESC NULLS LAST", "10", " 5 ", "asc",
    ])
    def test_allowed(self, value: str) -> None:
        assert _sanitize_sql_identifier(value) == value.strip()

    @pytest.mark.parametrize("value", [
        "(SELECT 1)", "count(*)", "1; DROP TABLE t", "a--", "-1", "a b", "a = 1",
        "a /* x */", "'a'", "1.5", "",
    ])
    def test_refused(self, value: str) -> None:
        with pytest.raises(ValueError):
            _sanitize_sql_identifier(value)


class TestAction:
    """`action` was never implemented, so configs can't use it."""

    @pytest.mark.parametrize("param", [
        {"x": {"action": "do_it"}},
        {"x": {"conditional": {"a": {"action": "do_it"}}}},
    ])
    def test_refused_at_startup(self, param: Dict[str, Any]) -> None:
        config = {"routes": [{"route": "r", "sql": "SELECT 1", "query_params": [param]}]}
        with pytest.raises(ValueError, match="isn't implemented"):
            check_database_config(config)


#
# INTERNAL
#
def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data))


def _query(root: Path, sql: str, params: Dict[str, str], folder: Path = None,
           extra_params: List[Dict[str, Any]] = None) -> Any:
    folder = folder or root
    folder.mkdir(parents=True, exist_ok=True)
    duckdb.sql(f"COPY (SELECT * FROM (VALUES {ROWS}) v(id, kind, n)) "
               f"TO '{folder / 't.parquet'}' (FORMAT parquet)")
    query_params = [{"kind": {"sql": "t.kind = {{kind}}"}}] + (extra_params or [])
    _write(root / "databases" / "db.yaml", {
        "tables": {"t": str(folder / "t.parquet")},
        "routes": [{"route": "r", "sql": sql, "query_params": query_params}],
    })
    _write(root / "config.yaml", {"name": "x", "databases": ["db"]})
    mapper = RouteMapper(str(root / "config.yaml"))
    return json.loads(asyncio.run(mapper.map_database_route("db", "r", params, {})).content)


def _ids(rows: Any) -> List[int]:
    assert isinstance(rows, list), rows
    return [row["id"] for row in rows]


def _two_tables(root: Path, sql: str, schema: bool = False) -> Any:
    duckdb.sql(f"COPY (SELECT 1 AS id) TO '{root / 'a.parquet'}' (FORMAT parquet)")
    duckdb.sql(f"COPY (SELECT 1 AS id, 'x' AS v) TO '{root / 'b.parquet'}' (FORMAT parquet)")
    tables = {"a": str(root / "a.parquet"), "b": str(root / "b.parquet")}
    if schema:
        _write(root / "databases" / "config.yaml", {"database": {"schema": {"s": tables}}})
        _write(root / "databases" / "db.yaml", {"routes": [{"route": "r", "sql": sql}]})
    else:
        _write(root / "databases" / "db.yaml", {"tables": tables,
                                               "routes": [{"route": "r", "sql": sql}]})
    _write(root / "config.yaml", {"name": "x", "databases": ["db"]})
    mapper = RouteMapper(str(root / "config.yaml"))
    return json.loads(asyncio.run(mapper.map_database_route("db", "r", {}, {})).content)

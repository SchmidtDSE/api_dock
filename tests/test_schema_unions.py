"""

Tests for cross-schema unions in the SQL builder.

Covers ``[[*.table]]`` / ``[[*!.table]]`` / ``[[group.table]]`` /
``[[group!.table]]`` union references, shared ``schema_groups`` validation,
route ``source_columns``, ``{{self.*}}`` placeholders, schema -> name/version
lookups, and an end-to-end "overlaps" route through
``RouteMapper.map_database_route`` against local parquet files.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import json
from pathlib import Path
from typing import Any, Dict

import duckdb
import pytest
import yaml

from api_dock.database_config import get_schema_sources, load_shared_config
from api_dock.route_mapper import RouteMapper
from api_dock.sql_builder import build_schema_view_statements, build_sql_query_with_tables
from api_dock.types import SqlContext


#
# CONSTANTS
#
SHARED: Dict[str, Any] = {
    "schema": {
        "a": {"detections": "a.parquet", "other": "a_other.parquet"},
        "b": {"detections": "b.parquet"},
        "c": {"detections": "c.parquet"},
    },
}

GROUPS: Dict[str, Any] = {"ab": ["a", "b"], "bc": ["b", "c"]}

OVERLAPS_SQL: str = """
WITH src AS (
  SELECT recording_id, start_time, end_time FROM [[detections]] WHERE id = {{id}}
)
SELECT detections.*
FROM [[*.detections]] detections
JOIN src ON detections.recording_id = src.recording_id
        AND detections.start_time < src.end_time
        AND detections.end_time > src.start_time
WHERE NOT (detections.schema_name = {{self.schema}} AND detections.id = {{id}})
"""


#
# PUBLIC
#
class TestUnionRendering:
    """How union references expand."""

    def _sql(self, sql: str, schema: Any = "a", route: Dict[str, Any] = None,
             context: SqlContext = None) -> str:
        route_config = {"sql": sql, **(route or {})}
        built, _ = build_sql_query_with_tables(
            route_config, {"schema": schema}, shared_config=SHARED,
            context=context or SqlContext(schema_groups=GROUPS),
        )
        return built

    def test_all_schemas(self) -> None:
        assert self._sql("SELECT d.id FROM [[*.detections]] d") == (
            "SELECT d.id FROM ((SELECT * FROM a.detections) UNION ALL BY NAME "
            "(SELECT * FROM b.detections) UNION ALL BY NAME (SELECT * FROM c.detections)) d"
        )

    def test_all_schemas_skips_schemas_without_table(self) -> None:
        assert self._sql("SELECT * FROM [[*.other]] o") == (
            "SELECT * FROM ((SELECT * FROM a.other)) o"
        )

    def test_exclude_self(self) -> None:
        assert self._sql("SELECT * FROM [[*!.detections]] d") == (
            "SELECT * FROM ((SELECT * FROM b.detections) UNION ALL BY NAME "
            "(SELECT * FROM c.detections)) d"
        )

    def test_exclude_self_without_schema_removes_nothing(self) -> None:
        assert self._sql("SELECT * FROM [[*!.detections]] d", schema=None).count("SELECT *") == 4

    def test_exclude_self_leaving_nothing_returns_empty_shape(self) -> None:
        assert self._sql("SELECT * FROM [[*!.other]] o") == (
            "SELECT * FROM ((SELECT * FROM a.other LIMIT 0)) o"
        )

    def test_group_and_group_exclude_self(self) -> None:
        assert self._sql("SELECT * FROM [[bc.detections]] d") == (
            "SELECT * FROM ((SELECT * FROM b.detections) UNION ALL BY NAME "
            "(SELECT * FROM c.detections)) d"
        )
        assert self._sql("SELECT * FROM [[ab!.detections]] d") == (
            "SELECT * FROM ((SELECT * FROM b.detections)) d"
        )

    def test_group_member_missing_table_raises(self) -> None:
        with pytest.raises(ValueError):
            self._sql("SELECT * FROM [[bc.other]] o")

    def test_no_schema_has_table_raises(self) -> None:
        with pytest.raises(ValueError):
            self._sql("SELECT * FROM [[*.nope]] n")

    def test_union_outside_from_raises(self) -> None:
        with pytest.raises(ValueError):
            self._sql("SELECT [[*.detections]].id FROM [[*.detections]] d")

    def test_bang_on_single_schema_raises(self) -> None:
        with pytest.raises(ValueError):
            self._sql("SELECT * FROM [[b!.detections]] d")

    def test_single_schema_unchanged(self) -> None:
        assert self._sql("SELECT * FROM [[b.detections]]") == "SELECT * FROM b.detections"

    def test_members_are_collected_for_views(self) -> None:
        _, refs = build_sql_query_with_tables(
            {"sql": "SELECT * FROM [[*!.detections]] d"}, {"schema": "a"},
            shared_config=SHARED, context=SqlContext(),
        )
        assert [ref.sql_name for ref in refs] == ["b.detections", "c.detections"]
        assert build_schema_view_statements(refs)[0] == "CREATE SCHEMA IF NOT EXISTS b"


class TestSourceColumns:
    """Route ``source_columns`` add source facts to union rows."""

    CONTEXT = SqlContext(schema_sources={"a": ("alpha", "1.0"), "b": ("beta", None)})

    def _sql(self, source_columns: Any, sql: str = "SELECT * FROM [[ab.detections]] d") -> str:
        built, _ = build_sql_query_with_tables(
            {"sql": sql, "source_columns": source_columns}, {"schema": "a"},
            shared_config=SHARED,
            context=SqlContext(schema_groups=GROUPS, schema_sources=self.CONTEXT.schema_sources),
        )
        return built

    def test_default_names(self) -> None:
        assert self._sql(["schema", "name", "version"]) == (
            "SELECT * FROM ("
            "(SELECT *, 'a' AS schema_name, 'alpha' AS name, '1.0' AS version FROM a.detections)"
            " UNION ALL BY NAME "
            "(SELECT *, 'b' AS schema_name, 'beta' AS name, CAST(NULL AS VARCHAR) AS version "
            "FROM b.detections)) d"
        )

    def test_mapping_renames_and_subsets(self) -> None:
        assert self._sql({"schema": "_schema"}) == (
            "SELECT * FROM ((SELECT *, 'a' AS _schema FROM a.detections) UNION ALL BY NAME "
            "(SELECT *, 'b' AS _schema FROM b.detections)) d"
        )

    def test_unknown_source_is_null(self) -> None:
        assert "CAST(NULL AS VARCHAR) AS name FROM c.detections" in self._sql(
            ["name"], "SELECT * FROM [[bc.detections]] d"
        )

    def test_single_member_gets_empty_copy(self) -> None:
        assert self._sql(["schema"], "SELECT * FROM [[ab!.detections]] d") == (
            "SELECT * FROM ((SELECT *, 'b' AS schema_name FROM b.detections) UNION ALL BY NAME "
            "(SELECT *, 'b' AS schema_name FROM b.detections LIMIT 0)) d"
        )

    @pytest.mark.parametrize("spec", [["bogus"], {"schema": "bad-name"}, "schema"])
    def test_invalid_spec_raises(self, spec: Any) -> None:
        with pytest.raises(ValueError):
            self._sql(spec)

    def test_no_source_columns_by_default(self) -> None:
        built, _ = build_sql_query_with_tables(
            {"sql": "SELECT * FROM [[ab.detections]] d"}, {}, shared_config=SHARED,
            context=SqlContext(schema_groups=GROUPS),
        )
        assert "AS schema_name" not in built


class TestSelfParams:
    """``{{self.*}}`` placeholders."""

    def test_values_and_nulls(self) -> None:
        built, _ = build_sql_query_with_tables(
            {"sql": "SELECT {{self.schema}}, {{self.name}}, {{self.version}} FROM [[a.detections]]"},
            {"schema": "a"}, shared_config=SHARED, context=SqlContext(name="owl"),
        )
        assert built == "SELECT 'a', 'owl', CAST(NULL AS VARCHAR) FROM a.detections"


class TestSchemaGroupsValidation:
    """load_shared_config validates ``schema_groups``."""

    @pytest.mark.parametrize("groups", [
        {"a": ["b"]},
        {"g": []},
        {"g": ["nope"]},
        {"bad-name": ["a"]},
        {"g": "a"},
    ])
    def test_invalid_groups_raise(self, tmp_path: Path, groups: Dict[str, Any]) -> None:
        _write_yaml(tmp_path / "databases" / "config.yaml", {
            "database": SHARED, "schema_groups": groups,
        })
        with pytest.raises(ValueError):
            load_shared_config(str(tmp_path))

    def test_valid_groups_load(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path / "databases" / "config.yaml", {
            "database": SHARED, "schema_groups": GROUPS,
        })
        assert load_shared_config(str(tmp_path))["schema_groups"] == GROUPS


class TestGetSchemaSources:
    """Schema -> (name, version), dropping ambiguous schemas."""

    def test_sources(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path / "databases" / "config.yaml", {
            "database": SHARED,
            "slugs": [
                {"name": "alpha", "version": "1.0", "schema": "a"},
                {"name": "beta", "schema": "b"},
                {"name": "gamma", "versions": [
                    {"version": "1", "schema": "c"}, {"version": "2", "schema": "c"},
                ]},
            ],
        })
        assert get_schema_sources(["alpha", "beta", "gamma"], str(tmp_path)) == {
            "a": ("alpha", "1.0"),
            "b": ("beta", None),
        }


class TestOverlapsEndToEnd:
    """An overlaps route over every schema, excluding only the source row."""

    @pytest.mark.anyio
    async def test_overlaps(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        rows = {
            # source "x1" overlaps "x2" in its own schema; "x3" is a different recording
            "m1": "('x1', 1, 10.0, 13.0), ('x2', 1, 12.0, 15.0), ('x3', 2, 10.0, 13.0)",
            # overlap, touching-but-not-overlapping, and no overlap
            "m2": "('x1', 1, 11.0, 14.0), ('y2', 1, 13.0, 16.0), ('y3', 1, 20.0, 23.0)",
        }
        conn = duckdb.connect()
        for name, values in rows.items():
            conn.execute(
                f"COPY (SELECT * FROM (VALUES {values}) t(id, recording_id, start_time, end_time))"
                f" TO '{data_dir / name}.parquet'"
            )
        conn.close()

        config_dir = tmp_path / "api_dock_config"
        _write_yaml(config_dir / "config.yaml", {"name": "t", "databases": ["m1", "m2"]})
        _write_yaml(config_dir / "databases" / "config.yaml", {
            "database": {"schema": {
                "m1_1p0": {"detections": str(data_dir / "m1.parquet")},
                "m2_1p0": {"detections": str(data_dir / "m2.parquet")},
            }},
            "slugs": [
                {"name": "m1", "version": "1.0", "schema": "m1_1p0"},
                {"name": "m2", "version": "1.0", "schema": "m2_1p0"},
            ],
            "routes": [{
                "route": "detections/{{id}}/overlaps",
                "source_columns": ["schema", "name", "version"],
                "sql": OVERLAPS_SQL,
            }],
            "query_params": [{"limit": {"sql_append": "LIMIT {{limit}}"}}],
        })

        monkeypatch.chdir(tmp_path)
        mapper = RouteMapper(str(config_dir / "config.yaml"))

        result = await mapper.map_database_route("m1", "1.0/detections/x1/overlaps")
        assert result.status_code == 200, result.content
        found = sorted(
            (row["schema_name"], row["id"], row["name"], row["version"])
            for row in json.loads(result.content)
        )
        # Same id "x1" in the OTHER schema is kept; only m1's own x1 is excluded.
        assert found == [
            ("m1_1p0", "x2", "m1", "1.0"),
            ("m2_1p0", "x1", "m2", "1.0"),
        ]

        result = await mapper.map_database_route("m2", "1.0/detections/y2/overlaps")
        found = sorted((r["schema_name"], r["id"]) for r in json.loads(result.content))
        assert found == [("m1_1p0", "x2"), ("m2_1p0", "x1")]


#
# INTERNAL
#
def _write_yaml(path: Path, data: Dict[str, Any]) -> None:
    """Write ``data`` as YAML to ``path``, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data))

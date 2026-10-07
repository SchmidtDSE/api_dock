"""

Tests for the shared database config (``databases/config.yaml``).

Covers loading the shared ``database`` mapping, inline ``slugs`` database/version
configs (alongside config files), shared ``routes`` /
``query_params`` merging with overrides, inclusions and exclusions, ``[[table]]`` resolution order
(version tables -> version schema -> shared global tables), ``[[schema.table]]``
references, shared ``meta`` defaults, the SQL each form renders to, schema view
statements, path-scoped S3 secrets, and an end-to-end DuckDB query through
``RouteMapper.map_database_route`` using local parquet files.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import json
from pathlib import Path
from typing import Any, Dict, List

import duckdb
import pytest
import yaml

from api_dock.database_config import (
    apply_shared_definitions,
    get_database_versions,
    get_local_table_references,
    get_slug_configs,
    is_versioned_database,
    load_database_config,
    load_shared_config,
    load_shared_database_config,
    resolve_table_reference,
)
from api_dock.listings import build_listing
from api_dock.route_mapper import RouteMapper
from api_dock.sql_builder import build_schema_view_statements, build_sql_query_with_tables
from api_dock.storage_auth import setup_table_storage_authentication
from api_dock.types import ListingSpec, TableReference


#
# CONSTANTS
#
SHARED_CONFIG: Dict[str, Any] = {
    "table1": {"uri": "s3://bucket/global/table1.parquet"},
    "table3": {"uri": "s3://bucket/global/table3.parquet", "region": "us-west-1", "public": False},
    "meta": {"region": "us-west-2", "public": True},
    "schema": {
        "birdnet_2p4": {"detections": {"uri": "s3://bucket/bn24/detections.parquet"}},
        "birdnet_3p0": {
            "detections": {"uri": "s3://bucket/bn30/detections.parquet", "public": False},
        },
    },
}

VERSION_CONFIG: Dict[str, Any] = {
    "name": "birdnet",
    "schema": "birdnet_2p4",
    "tables": {"revisions": {"uri": "s3://bucket/revisions.parquet", "region": "us-east-1"}},
}

SHARED_FILE: Dict[str, Any] = {
    "routes": [
        {"route": "/recordings/{{recording_id}}/detections/", "sql": "SELECT 'shared-rec'"},
        {"route": "detections/{{id}}", "sql": "SELECT 'shared-id'"},
        {
            "route": "not_for_everyone/{{id}}",
            "sql": "SELECT 'restricted'",
            "exclude": [
                "slug1/3.0",
                "slug1/3.5",
                {"slug": "slug2", "version": 2.3},
                {"slug": "slug3", "version": "*"},
            ],
        },
    ],
    "query_params": [
        {"confidence": {"sql": "c >= {{confidence}}"}},
        {"start_time": {"sql": "s >= {{start_time}}", "exclude": ["slug1/3.9"]}},
        {"direction": {"default": "ASC"}},
    ],
}


#
# PUBLIC
#
class TestLoadSharedDatabaseConfig:
    """load_shared_database_config reads databases/config.yaml's ``database`` key."""

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert load_shared_database_config(str(tmp_path)) == {}

    def test_reads_database_key(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path / "databases" / "config.yaml", {"database": SHARED_CONFIG})
        assert load_shared_database_config(str(tmp_path)) == SHARED_CONFIG

    def test_commented_out_file_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "databases" / "config.yaml"
        path.parent.mkdir(parents=True)
        path.write_text("# database:\n#   meta: {}\n")
        assert load_shared_database_config(str(tmp_path)) == {}

    def test_non_mapping_database_raises(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path / "databases" / "config.yaml", {"database": ["nope"]})
        with pytest.raises(ValueError):
            load_shared_database_config(str(tmp_path))


class TestLoadSharedConfig:
    """load_shared_config returns the whole file with known keys normalized."""

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert load_shared_config(str(tmp_path)) == {}

    def test_normalizes_missing_keys(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path / "databases" / "config.yaml", {"routes": [{"route": "a"}]})
        loaded = load_shared_config(str(tmp_path))
        assert loaded["routes"] == [{"route": "a"}]
        assert loaded["database"] == {}
        assert loaded["query_params"] == []
        assert loaded["route_exclusions"] == []

    def test_wrong_type_raises(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path / "databases" / "config.yaml", {"routes": {"route": "a"}})
        with pytest.raises(ValueError):
            load_shared_config(str(tmp_path))


class TestApplySharedDefinitions:
    """Shared routes/query_params: append, override, and exclude."""

    def test_nothing_to_merge_returns_same_config(self) -> None:
        config = {"routes": []}
        assert apply_shared_definitions(config, {}, "slug1", "1.0") is config

    def test_shared_items_added(self) -> None:
        merged = apply_shared_definitions({}, SHARED_FILE, "slug1", "1.0")
        assert [r["route"] for r in merged["routes"]] == [
            "/recordings/{{recording_id}}/detections/",
            "detections/{{id}}",
            "not_for_everyone/{{id}}",
        ]
        assert [next(iter(p)) for p in merged["query_params"]] == [
            "confidence", "start_time", "direction",
        ]

    def test_exclude_keys_are_stripped(self) -> None:
        merged = apply_shared_definitions({}, SHARED_FILE, "slug1", "1.0")
        assert all("exclude" not in r for r in merged["routes"])
        assert merged["query_params"][1] == {"start_time": {"sql": "s >= {{start_time}}"}}
        assert "exclude" in SHARED_FILE["routes"][2]

    def test_own_routes_first_and_override_by_shape(self) -> None:
        own = {"routes": [
            {"route": "detections/{{detection_id}}/", "sql": "SELECT 'own-id'"},
            {"route": "extra", "sql": "SELECT 'own-extra'"},
        ]}
        merged = apply_shared_definitions(own, SHARED_FILE, "slug1", "1.0")
        assert [r["sql"] for r in merged["routes"]] == [
            "SELECT 'own-id'", "SELECT 'own-extra'", "SELECT 'shared-rec'", "SELECT 'restricted'",
        ]
        assert own["routes"][0]["sql"] == "SELECT 'own-id'" and len(own["routes"]) == 2

    def test_own_query_params_override_by_name(self) -> None:
        own = {"query_params": [{"confidence": {"sql": "own >= {{confidence}}"}}]}
        merged = apply_shared_definitions(own, SHARED_FILE, "slug1", "1.0")
        assert merged["query_params"][0] == {"confidence": {"sql": "own >= {{confidence}}"}}
        assert [next(iter(p)) for p in merged["query_params"]] == [
            "confidence", "start_time", "direction",
        ]

    @pytest.mark.parametrize("slug,version,excluded", [
        ("slug1", "3.0", True),
        ("slug1", "3.5", True),
        ("slug1", "3.1", False),
        ("slug2", "2.3", True),
        ("slug2", "2.4", False),
        ("slug3", "0.1", True),
        ("slug3", None, True),
        ("slug4", "3.0", False),
    ])
    def test_route_exclude_forms(self, slug: str, version: Any, excluded: bool) -> None:
        merged = apply_shared_definitions({}, SHARED_FILE, slug, version)
        routes = [r["route"] for r in merged["routes"]]
        assert ("not_for_everyone/{{id}}" not in routes) is excluded

    def test_query_param_exclude(self) -> None:
        for version, included in (("3.9", False), ("3.8", True)):
            merged = apply_shared_definitions({}, SHARED_FILE, "slug1", version)
            names = [next(iter(param)) for param in merged["query_params"]]
            assert ("start_time" in names) is included

    def test_version_number_forms_match(self) -> None:
        shared = {"routes": [{"route": "a", "sql": "x", "exclude": [{"slug": "s", "version": 4}]}]}
        assert apply_shared_definitions({}, shared, "s", "4.0")["routes"] == []

    def test_slug_only_string_excludes_all_versions(self) -> None:
        shared = {"routes": [{"route": "a", "sql": "x", "exclude": ["s", "t/*"]}]}
        assert apply_shared_definitions({}, shared, "s", "9.9")["routes"] == []
        assert apply_shared_definitions({}, shared, "t", None)["routes"] == []

    def test_top_level_exclusions(self) -> None:
        shared = {
            **SHARED_FILE,
            "route_exclusions": ["slug1/1.0"],
            "query_exclusions": [{"slug": "slug2"}],
        }
        own = {"routes": [{"route": "own", "sql": "x"}], "query_params": [{"q": {"sql": "y"}}]}
        merged = apply_shared_definitions(own, shared, "slug1", "1.0")
        assert merged["routes"] == own["routes"]
        assert len(merged["query_params"]) == 3 + 1
        merged = apply_shared_definitions(own, shared, "slug2", "5.0")
        assert len(merged["routes"]) == 1 + 3
        assert merged["query_params"] == own["query_params"]

    def test_route_include_limits_to_listed(self) -> None:
        shared = {"routes": [{"route": "a", "sql": "x", "include": ["s/1.0", {"slug": "t"}]}]}
        assert len(apply_shared_definitions({}, shared, "s", "1.0")["routes"]) == 1
        assert apply_shared_definitions({}, shared, "s", "2.0")["routes"] == []
        assert len(apply_shared_definitions({}, shared, "t", "7")["routes"]) == 1
        assert apply_shared_definitions({}, shared, "u", "1.0")["routes"] == []

    def test_include_keys_are_stripped(self) -> None:
        shared = {
            "routes": [{"route": "a", "sql": "x", "include": ["s"]}],
            "query_params": [{"q": {"sql": "y", "include": ["s"], "exclude": ["s/2"]}}],
        }
        merged = apply_shared_definitions({}, shared, "s", "1")
        assert merged["routes"] == [{"route": "a", "sql": "x"}]
        assert merged["query_params"] == [{"q": {"sql": "y"}}]

    def test_include_and_exclude_combine(self) -> None:
        shared = {"routes": [{"route": "a", "sql": "x", "include": ["s"], "exclude": ["s/2"]}]}
        assert len(apply_shared_definitions({}, shared, "s", "1")["routes"]) == 1
        assert apply_shared_definitions({}, shared, "s", "2")["routes"] == []

    def test_query_param_include(self) -> None:
        shared = {"query_params": [{"q": {"sql": "y", "include": ["s/1"]}}]}
        assert len(apply_shared_definitions({}, shared, "s", "1")["query_params"]) == 1
        assert apply_shared_definitions({}, shared, "s", "2")["query_params"] == []

    def test_same_route_with_complementary_include_exclude(self) -> None:
        shared = {"routes": [
            {"route": "detections/", "sql": "generic", "exclude": ["birdnet/2.4"]},
            {"route": "/detections", "sql": "birdnet", "include": ["birdnet/2.4"]},
        ]}
        assert apply_shared_definitions({}, shared, "birdnet", "2.4")["routes"] == [
            {"route": "/detections", "sql": "birdnet"},
        ]
        assert apply_shared_definitions({}, shared, "owl", "5.0")["routes"] == [
            {"route": "detections/", "sql": "generic"},
        ]

    def test_first_selected_shared_route_wins(self) -> None:
        shared = {"routes": [
            {"route": "a/{{x}}", "sql": "first"},
            {"route": "a/{{y}}", "sql": "second"},
        ]}
        assert [r["sql"] for r in apply_shared_definitions({}, shared, "s", "1")["routes"]] == [
            "first",
        ]

    def test_first_selected_shared_query_param_wins(self) -> None:
        shared = {"query_params": [
            {"q": {"sql": "first", "include": ["other"]}},
            {"q": {"sql": "second"}},
            {"q": {"sql": "third"}},
        ]}
        assert apply_shared_definitions({}, shared, "s", "1")["query_params"] == [
            {"q": {"sql": "second"}},
        ]

    def test_top_level_inclusions(self) -> None:
        shared = {
            **SHARED_FILE,
            "route_inclusions": ["slug1"],
            "query_inclusions": [{"slug": "slug2", "version": "5.0"}],
        }
        own = {"routes": [{"route": "own", "sql": "x"}], "query_params": [{"q": {"sql": "y"}}]}
        merged = apply_shared_definitions(own, shared, "slug1", "1.0")
        assert len(merged["routes"]) == 1 + 3
        assert merged["query_params"] == own["query_params"]
        merged = apply_shared_definitions(own, shared, "slug2", "5.0")
        assert merged["routes"] == own["routes"]
        assert len(merged["query_params"]) == 1 + 3

    def test_top_level_inclusions_and_exclusions_combine(self) -> None:
        shared = {**SHARED_FILE, "route_inclusions": ["slug1"], "route_exclusions": ["slug1/2"]}
        assert len(apply_shared_definitions({}, shared, "slug1", "1")["routes"]) == 3
        assert "routes" not in apply_shared_definitions({}, shared, "slug1", "2")

    def test_malformed_exclusion_raises(self) -> None:
        shared = {"routes": [{"route": "a", "sql": "x", "exclude": [{"version": "1"}]}]}
        with pytest.raises(ValueError):
            apply_shared_definitions({}, shared, "s", "1")


class TestSlugConfigs:
    """Inline database/version configs from the shared config's ``slugs``."""

    SLUGS: List[Dict[str, Any]] = [
        {"name": "bullfrog", "version": 2.5, "description": "d25", "schema": "bf_2p5"},
        {
            "name": "apple",
            "authors": ["A"],
            "versions": [
                {"version": 1.0, "description": "d1", "schema": "a1"},
                {"version": "12.0", "description": "d12", "authors": ["B"]},
            ],
        },
        {"name": "plain", "description": "unversioned"},
    ]

    def test_single_version_form(self, tmp_path: Path) -> None:
        _write_shared(tmp_path, {"slugs": self.SLUGS})
        assert get_slug_configs(str(tmp_path))["bullfrog"] == {
            "2.5": {"name": "bullfrog", "description": "d25", "schema": "bf_2p5"},
        }

    def test_versions_list_with_defaults(self, tmp_path: Path) -> None:
        _write_shared(tmp_path, {"slugs": self.SLUGS})
        apple = get_slug_configs(str(tmp_path))["apple"]
        assert apple["1.0"] == {
            "name": "apple", "authors": ["A"], "description": "d1", "schema": "a1",
        }
        assert apple["12.0"] == {"name": "apple", "authors": ["B"], "description": "d12"}

    def test_unversioned_form(self, tmp_path: Path) -> None:
        _write_shared(tmp_path, {"slugs": self.SLUGS})
        assert get_slug_configs(str(tmp_path))["plain"] == {
            None: {"name": "plain", "description": "unversioned"},
        }
        assert not is_versioned_database("plain", str(tmp_path))
        assert load_database_config("plain", str(tmp_path))["description"] == "unversioned"

    def test_same_name_in_several_entries_merges(self, tmp_path: Path) -> None:
        _write_shared(tmp_path, {"slugs": [
            {"name": "m", "version": "1.0"}, {"name": "m", "version": "2.0"},
        ]})
        assert sorted(get_slug_configs(str(tmp_path))["m"]) == ["1.0", "2.0"]

    @pytest.mark.parametrize("slugs", [
        [{"version": "1.0"}],
        [{"name": "m", "version": "1.0", "versions": [{"version": "2.0"}]}],
        [{"name": "m", "versions": []}],
        [{"name": "m", "versions": [{"description": "no version"}]}],
        [{"name": "m", "version": "1.0"}, {"name": "m", "version": 1.0}],
        [{"name": "m", "version": "1.0"}, {"name": "m"}],
        ["m"],
    ])
    def test_malformed_raises(self, tmp_path: Path, slugs: List[Any]) -> None:
        _write_shared(tmp_path, {"slugs": slugs})
        with pytest.raises(ValueError):
            get_slug_configs(str(tmp_path))

    def test_files_and_slugs_combine(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path / "databases" / "owl" / "4.0.yaml", {"name": "owl", "src": "file"})
        _write_yaml(tmp_path / "databases" / "owl" / "5.0.yaml", {"name": "owl", "src": "file"})
        _write_shared(tmp_path, {"slugs": [
            {"name": "owl", "versions": [
                {"version": "5.0", "src": "slug"}, {"version": "6.0", "src": "slug"},
            ]},
            {"name": "frog", "version": "1.0", "src": "slug"},
        ]})
        config_dir = str(tmp_path)
        assert is_versioned_database("owl", config_dir)
        assert is_versioned_database("frog", config_dir)
        assert get_database_versions("owl", config_dir) == ["4.0", "5.0", "6.0"]
        assert get_database_versions("frog", config_dir) == ["1.0"]
        assert load_database_config("owl", config_dir, "4.0")["src"] == "file"
        assert load_database_config("owl", config_dir, "5.0")["src"] == "file"  # file wins
        assert load_database_config("owl", config_dir, "6.0")["src"] == "slug"
        assert load_database_config("frog", config_dir, "1.0")["src"] == "slug"

    def test_missing_version_still_raises_file_not_found(self, tmp_path: Path) -> None:
        _write_shared(tmp_path, {"slugs": [{"name": "frog", "version": "1.0"}]})
        with pytest.raises(FileNotFoundError):
            load_database_config("frog", str(tmp_path), "9.9")
        with pytest.raises(FileNotFoundError):
            load_database_config("frog", str(tmp_path))
        with pytest.raises(FileNotFoundError):
            load_database_config("nope", str(tmp_path))

    def test_no_shared_file_keeps_file_behavior(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path / "databases" / "owl" / "4.0.yaml", {"name": "owl"})
        assert get_database_versions("owl", str(tmp_path)) == ["4.0"]
        assert not is_versioned_database("frog", str(tmp_path))

    def test_listing_includes_slug_versions(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = tmp_path / "api_dock_config"
        _write_yaml(config_dir / "databases" / "owl" / "4.0.yaml", {"name": "owl"})
        _write_shared(config_dir, {"slugs": [
            {"name": "owl", "version": "5.0"}, {"name": "plain"},
        ]})
        monkeypatch.chdir(tmp_path)
        spec = ListingSpec(kind="databases", route="databases", as_dict=False)
        assert build_listing(spec, {"databases": ["owl", "plain"]}) == [
            "owl/4.0", "owl/5.0", "plain",
        ]


class TestResolveTableReference:
    """Lookup order, qualified references, and meta defaults."""

    def test_version_table_wins(self) -> None:
        version = {**VERSION_CONFIG, "tables": {"detections": "local/detections.parquet"}}
        ref = resolve_table_reference("detections", version, SHARED_CONFIG)
        assert ref.uri == "local/detections.parquet"
        assert ref.schema is None

    def test_version_schema_used_when_not_local(self) -> None:
        ref = resolve_table_reference("detections", VERSION_CONFIG, SHARED_CONFIG)
        assert ref.uri == "s3://bucket/bn24/detections.parquet"
        assert ref.schema == "birdnet_2p4"
        assert not ref.qualified
        assert ref.sql_name == "detections"

    def test_schema_wins_over_global(self) -> None:
        shared = {**SHARED_CONFIG, "detections": {"uri": "s3://bucket/global/detections.parquet"}}
        ref = resolve_table_reference("detections", VERSION_CONFIG, shared)
        assert ref.schema == "birdnet_2p4"

    def test_global_fallback(self) -> None:
        ref = resolve_table_reference("table1", VERSION_CONFIG, SHARED_CONFIG)
        assert ref.uri == "s3://bucket/global/table1.parquet"

    def test_no_schema_key_skips_to_global(self) -> None:
        shared = {**SHARED_CONFIG, "detections": {"uri": "s3://bucket/global/detections.parquet"}}
        ref = resolve_table_reference("detections", {"tables": {}}, shared)
        assert ref.uri == "s3://bucket/global/detections.parquet"

    def test_unknown_schema_skips_to_global(self) -> None:
        ref = resolve_table_reference("table1", {"schema": "nope"}, SHARED_CONFIG)
        assert ref.uri == "s3://bucket/global/table1.parquet"

    def test_reserved_keys_are_not_tables(self) -> None:
        assert resolve_table_reference("meta", {}, SHARED_CONFIG) is None
        assert resolve_table_reference("schema", {}, SHARED_CONFIG) is None

    def test_missing_returns_none(self) -> None:
        assert resolve_table_reference("nope", VERSION_CONFIG, SHARED_CONFIG) is None

    def test_no_shared_config_matches_legacy(self) -> None:
        ref = resolve_table_reference("revisions", VERSION_CONFIG, None)
        assert ref.uri == "s3://bucket/revisions.parquet"
        assert ref.metadata == {"region": "us-east-1"}

    def test_qualified_reference(self) -> None:
        ref = resolve_table_reference("birdnet_3p0.detections", VERSION_CONFIG, SHARED_CONFIG)
        assert ref.uri == "s3://bucket/bn30/detections.parquet"
        assert ref.qualified
        assert ref.sql_name == "birdnet_3p0.detections"

    def test_qualified_unknown_returns_none(self) -> None:
        assert resolve_table_reference("nope.detections", {}, SHARED_CONFIG) is None
        assert resolve_table_reference("birdnet_2p4.nope", {}, SHARED_CONFIG) is None

    def test_qualified_invalid_identifier_raises(self) -> None:
        with pytest.raises(ValueError):
            resolve_table_reference("bad-name.detections", {}, SHARED_CONFIG)

    def test_meta_applies_to_all_tables(self) -> None:
        schema_ref = resolve_table_reference("detections", VERSION_CONFIG, SHARED_CONFIG)
        global_ref = resolve_table_reference("table1", VERSION_CONFIG, SHARED_CONFIG)
        assert schema_ref.metadata == {"region": "us-west-2", "public": True}
        assert global_ref.metadata == {"region": "us-west-2", "public": True}

    def test_table_keys_override_meta(self) -> None:
        local = resolve_table_reference("revisions", VERSION_CONFIG, SHARED_CONFIG)
        assert local.metadata == {"region": "us-east-1", "public": True}
        table3 = resolve_table_reference("table3", VERSION_CONFIG, SHARED_CONFIG)
        assert table3.metadata == {"region": "us-west-1", "public": False}
        bn30 = resolve_table_reference("birdnet_3p0.detections", {}, SHARED_CONFIG)
        assert bn30.metadata == {"region": "us-west-2", "public": False}

    def test_local_table_references(self) -> None:
        refs = get_local_table_references(VERSION_CONFIG, SHARED_CONFIG)
        assert [ref.name for ref in refs] == ["revisions"]


class TestBuildSqlWithSharedTables:
    """SQL rendering and table collection for shared references."""

    def test_unqualified_schema_table_inlines(self) -> None:
        route = {"sql": "SELECT [[detections]].* FROM [[detections]]"}
        sql, refs = build_sql_query_with_tables(route, VERSION_CONFIG, shared_config=SHARED_CONFIG)
        assert sql == (
            "SELECT detections.* FROM 's3://bucket/bn24/detections.parquet' AS detections"
        )
        assert [ref.sql_name for ref in refs] == ["detections"]

    def test_qualified_renders_view_name(self) -> None:
        route = {
            "sql": (
                "SELECT [[birdnet_2p4.detections]].* FROM [[birdnet_2p4.detections]] "
                "WHERE [[birdnet_2p4.detections]].recording_id = {{recording_id}}"
            ),
        }
        sql, refs = build_sql_query_with_tables(
            route, {}, {"recording_id": "4"}, shared_config=SHARED_CONFIG
        )
        assert sql == (
            "SELECT detections.* FROM birdnet_2p4.detections "
            "WHERE detections.recording_id = '4'"
        )
        assert [ref.sql_name for ref in refs] == ["birdnet_2p4.detections"]

    def test_qualified_keeps_user_alias(self) -> None:
        route = {"sql": "SELECT o.id FROM [[birdnet_3p0.detections]] o"}
        sql, _ = build_sql_query_with_tables(route, {}, shared_config=SHARED_CONFIG)
        assert sql == "SELECT o.id FROM birdnet_3p0.detections o"

    def test_refs_from_fragments_are_collected(self) -> None:
        route = {
            "sql": "SELECT [[revisions]].* FROM [[revisions]]",
            "query_params": [{"g": {"sql": "[[revisions]].id IN (SELECT id FROM [[table1]])"}}],
        }
        _, refs = build_sql_query_with_tables(
            route, VERSION_CONFIG, query_params={"g": "1"}, shared_config=SHARED_CONFIG
        )
        assert [ref.sql_name for ref in refs] == ["revisions", "table1"]

    def test_unknown_table_raises(self) -> None:
        route = {"sql": "SELECT * FROM [[birdnet_9p9.detections]]"}
        with pytest.raises(ValueError):
            build_sql_query_with_tables(route, {}, shared_config=SHARED_CONFIG)


class TestBuildSchemaViewStatements:
    """Only qualified references become schema views."""

    def test_statements(self) -> None:
        refs = [
            TableReference("detections", "s3://b/a.parquet", schema="s1", qualified=True),
            TableReference("other", "s3://b/o'x.parquet", schema="s1", qualified=True),
            TableReference("detections", "s3://b/c.parquet", schema="s2"),
            TableReference("revisions", "s3://b/r.parquet"),
        ]
        assert build_schema_view_statements(refs) == [
            "CREATE SCHEMA IF NOT EXISTS s1",
            "CREATE OR REPLACE VIEW s1.detections AS SELECT * FROM 's3://b/a.parquet'",
            "CREATE OR REPLACE VIEW s1.other AS SELECT * FROM 's3://b/o''x.parquet'",
        ]


class TestSetupTableStorageAuthentication:
    """Default secret plus path-scoped secrets for tables that differ."""

    def test_uniform_tables_get_only_default_secret(self) -> None:
        conn = _RecordingConnection()
        meta = {"region": "us-west-2", "public": True}
        setup_table_storage_authentication(conn, [
            TableReference("a", "s3://b/a.parquet", dict(meta)),
            TableReference("b", "s3://b/b.parquet", dict(meta)),
        ])
        secrets = conn.secret_statements()
        assert len(secrets) == 1
        assert "SCOPE" not in secrets[0]
        assert "REGION 'us-west-2'" in secrets[0]

    def test_differing_tables_get_scoped_secrets(self) -> None:
        conn = _RecordingConnection()
        setup_table_storage_authentication(conn, [
            TableReference("a", "s3://b/a.parquet", {"region": "us-west-1", "public": False}),
            TableReference("g", "s3://b/owl/**/*.parquet", {"region": "us-west-2", "public": True}),
        ])
        secrets = conn.secret_statements()
        # Default secret takes the last table's settings; the first gets its own.
        assert len(secrets) == 2
        assert "SCOPE" not in secrets[0] and "REGION 'us-west-2'" in secrets[0]
        assert "api_dock_s3_table_0" in secrets[1]
        assert "PROVIDER credential_chain" in secrets[1]
        assert "REGION 'us-west-1'" in secrets[1]
        assert "SCOPE 's3://b/a.parquet'" in secrets[1]

    def test_glob_scope_is_directory_prefix(self) -> None:
        conn = _RecordingConnection()
        setup_table_storage_authentication(conn, [
            TableReference("g", "s3://b/owl/v4/**/*.parquet", {"region": "us-east-1"}),
            TableReference("a", "s3://b/a.parquet", {"region": "us-west-2"}),
        ])
        assert "SCOPE 's3://b/owl/v4/'" in conn.secret_statements()[1]

    def test_unset_keys_inherit_default(self) -> None:
        conn = _RecordingConnection()
        setup_table_storage_authentication(conn, [
            TableReference("a", "s3://b/a.parquet", {}),
            TableReference("b", "s3://b/b.parquet", {"region": "us-west-2"}),
        ])
        assert len(conn.secret_statements()) == 1


class TestMapDatabaseRouteEndToEnd:
    """Real DuckDB queries against local parquet through map_database_route."""

    @pytest.mark.anyio
    async def test_schema_and_qualified_routes(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        conn = duckdb.connect()
        conn.execute(
            "COPY (SELECT * FROM (VALUES ('a', 1, 'Nutria'), ('b', 2, 'Bullfrog')) "
            f"t(id, recording_id, common_name)) TO '{data_dir / 'bn24.parquet'}'"
        )
        conn.execute(
            "COPY (SELECT * FROM (VALUES ('r1', 'a'), ('r2', 'a')) t(id, observation_id)) "
            f"TO '{data_dir / 'revisions.parquet'}'"
        )
        conn.close()

        config_dir = tmp_path / "api_dock_config"
        _write_yaml(config_dir / "config.yaml", {"name": "t", "databases": ["birdnet", "owl"]})
        _write_yaml(config_dir / "databases" / "config.yaml", {"database": {
            "schema": {"birdnet_2p4": {"detections": {"uri": str(data_dir / "bn24.parquet")}}},
        }})
        _write_yaml(config_dir / "databases" / "birdnet" / "2.4.yaml", {
            "name": "birdnet",
            "schema": "birdnet_2p4",
            "tables": {"revisions": str(data_dir / "revisions.parquet")},
            "routes": [
                {
                    "route": "recordings/{{recording_id}}/detections",
                    "sql": (
                        "SELECT [[detections]].* FROM [[detections]] "
                        "WHERE [[detections]].recording_id = {{recording_id}}"
                    ),
                },
                {
                    "route": "detections",
                    "sql": [{
                        "when": "count",
                        "then": {
                            "sql": (
                                "SELECT detections.common_name, "
                                "COUNT(revisions.id) AS revcount "
                                "FROM [[birdnet_2p4.detections]] "
                                "LEFT JOIN [[revisions]] "
                                "ON revisions.observation_id = birdnet_2p4.detections.id"
                            ),
                            "sql_append": (
                                "GROUP BY birdnet_2p4.detections.common_name "
                                "ORDER BY revcount DESC"
                            ),
                        },
                    }],
                },
            ],
        })
        _write_yaml(config_dir / "databases" / "owl" / "5.0.yaml", {
            "name": "owl",
            "routes": [{
                "route": "birdnet/{{id}}",
                "sql": (
                    "SELECT [[birdnet_2p4.detections]].* FROM [[birdnet_2p4.detections]] "
                    "WHERE [[birdnet_2p4.detections]].id = {{id}}"
                ),
            }],
        })

        monkeypatch.chdir(tmp_path)
        mapper = RouteMapper(str(config_dir / "config.yaml"))

        result = await mapper.map_database_route("birdnet", "2.4/recordings/2/detections")
        assert result.status_code == 200, result.content
        assert json.loads(result.content) == [
            {"id": "b", "recording_id": 2, "common_name": "Bullfrog"},
        ]

        result = await mapper.map_database_route("birdnet", "2.4/detections", {"count": "true"})
        assert result.status_code == 200, result.content
        assert json.loads(result.content) == [
            {"common_name": "Nutria", "revcount": 2},
            {"common_name": "Bullfrog", "revcount": 0},
        ]

        # A different database can query another model's schema.
        result = await mapper.map_database_route("owl", "5.0/birdnet/a")
        assert result.status_code == 200, result.content
        assert json.loads(result.content) == [
            {"id": "a", "recording_id": 1, "common_name": "Nutria"},
        ]


    @pytest.mark.anyio
    async def test_shared_routes_and_query_params(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        conn = duckdb.connect()
        for name, rows in (("a", "(1, 0.9), (2, 0.1)"), ("b", "(3, 0.8)")):
            conn.execute(
                f"COPY (SELECT * FROM (VALUES {rows}) t(id, confidence)) "
                f"TO '{data_dir / name}.parquet'"
            )
        conn.close()

        config_dir = tmp_path / "api_dock_config"
        _write_yaml(config_dir / "config.yaml", {"name": "t", "databases": ["m"]})
        _write_yaml(config_dir / "databases" / "config.yaml", {
            "database": {"schema": {
                "m_1p0": {"detections": str(data_dir / "a.parquet")},
                "m_2p0": {"detections": str(data_dir / "b.parquet")},
            }},
            "routes": [
                {"route": "detections/", "sql": "SELECT [[detections]].* FROM [[detections]]"},
                {
                    "route": "special/",
                    "sql": "SELECT 1 AS special",
                    "exclude": [{"slug": "m", "version": 2.0}],
                },
            ],
            "query_params": [
                {"confidence": {"sql": "[[detections]].confidence >= {{confidence}}"}},
            ],
        })
        _write_yaml(config_dir / "databases" / "m" / "1.0.yaml", {"name": "m", "schema": "m_1p0"})
        _write_yaml(config_dir / "databases" / "m" / "2.0.yaml", {
            "name": "m",
            "schema": "m_2p0",
            "routes": [{"route": "/detections", "sql": "SELECT 'own' AS source"}],
        })

        monkeypatch.chdir(tmp_path)
        mapper = RouteMapper(str(config_dir / "config.yaml"))

        result = await mapper.map_database_route("m", "1.0/detections/", {"confidence": "0.5"})
        assert json.loads(result.content) == [{"id": 1, "confidence": 0.9}]

        result = await mapper.map_database_route("m", "1.0/special/")
        assert json.loads(result.content) == [{"special": 1}]

        result = await mapper.map_database_route("m", "2.0/detections/")
        assert json.loads(result.content) == [{"source": "own"}]

        result = await mapper.map_database_route("m", "2.0/special/")
        assert result.status_code == 404

        result = await mapper.map_database_route("m", "1.0")
        assert json.loads(result.content) == {"routes": ["detections/", "special/"]}


    @pytest.mark.anyio
    async def test_slug_defined_versions(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        conn = duckdb.connect()
        for name in ("v4", "v5"):
            conn.execute(f"COPY (SELECT '{name}' AS source) TO '{data_dir / name}.parquet'")
        conn.close()

        config_dir = tmp_path / "api_dock_config"
        _write_yaml(config_dir / "config.yaml", {"name": "t", "databases": ["owl"]})
        _write_shared(config_dir, {
            "database": {"schema": {
                "owl_4p0": {"detections": str(data_dir / "v4.parquet")},
                "owl_5p0": {"detections": str(data_dir / "v5.parquet")},
            }},
            "slugs": [{"name": "owl", "version": 5.0, "schema": "owl_5p0"}],
            "routes": [
                {"route": "detections", "sql": "SELECT [[detections]].* FROM [[detections]]"},
                {"route": "v5only", "sql": "SELECT 5 AS v", "include": ["owl/5.0"]},
            ],
        })
        _write_yaml(config_dir / "databases" / "owl" / "4.0.yaml", {
            "name": "owl", "schema": "owl_4p0",
        })

        monkeypatch.chdir(tmp_path)
        mapper = RouteMapper(str(config_dir / "config.yaml"))

        result = await mapper.map_database_route("owl", "")
        assert json.loads(result.content) == {"versions": ["4.0", "5.0"]}

        result = await mapper.map_database_route("owl", "4.0/detections")
        assert json.loads(result.content) == [{"source": "v4"}]

        result = await mapper.map_database_route("owl", "latest/detections")
        assert json.loads(result.content) == [{"source": "v5"}]

        result = await mapper.map_database_route("owl", "5.0/v5only")
        assert json.loads(result.content) == [{"v": 5}]

        result = await mapper.map_database_route("owl", "4.0/v5only")
        assert result.status_code == 404

        result = await mapper.map_database_route("owl", "6.0/detections")
        assert result.status_code == 404

    @pytest.mark.anyio
    async def test_malformed_slugs_return_500(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = tmp_path / "api_dock_config"
        _write_yaml(config_dir / "config.yaml", {"name": "t", "databases": ["owl"]})
        _write_shared(config_dir, {"slugs": [{"version": "1.0"}]})
        monkeypatch.chdir(tmp_path)
        mapper = RouteMapper(str(config_dir / "config.yaml"))
        result = await mapper.map_database_route("owl", "1.0/detections")
        assert result.status_code == 500


#
# INTERNAL
#
class _RecordingConnection:
    """Minimal DuckDB connection stand-in that records executed SQL."""

    def __init__(self) -> None:
        self.statements: List[str] = []

    def execute(self, sql: str) -> "_RecordingConnection":
        self.statements.append(" ".join(sql.split()))
        return self

    def secret_statements(self) -> List[str]:
        return [s for s in self.statements if s.startswith("CREATE OR REPLACE SECRET")]


def _write_shared(config_dir: Path, data: Dict[str, Any]) -> None:
    """Write the shared ``databases/config.yaml`` under ``config_dir``."""
    _write_yaml(config_dir / "databases" / "config.yaml", data)


def _write_yaml(path: Path, data: Dict[str, Any]) -> None:
    """Write ``data`` as YAML to ``path``, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data))

"""

Tests for lookups: parsing, templates, safety checks and config expansion

Covers refresh durations, lookup definitions, ``{{row.*}}`` / ``{{lookup.*}}``
templates (type keeping, forbidden positions, ``allow`` prefixes, plain
names), and how lookup rows expand ``slugs`` (``from:``, ``versions: {from}``,
inline schemas), remote versions, version files and main-config ``from:``
entries. Rows are set directly on the store; running lookups is covered in
test_lookup_sources.py and test_lookup_requests.py.

License: BSD 3-Clause

"""
#
# IMPORTS
#
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from api_dock.config import find_remote_config, get_database_names, get_inline_remote_configs
from api_dock.database_config import (
    get_database_versions,
    get_slug_configs,
    load_database_config,
    load_shared_config,
)
from api_dock.lookups import (
    check_endpoint_token,
    fill_templates,
    get_store,
    load_lookup_specs,
    LookupContext,
    LookupStore,
    parse_duration,
    parse_lookup_settings,
    parse_lookups,
    RowTemplateError,
)


#
# CONSTANTS
#
ALLOW: List[str] = ["s3://bucket/runs/"]
RUNS: List[Dict[str, Any]] = [
    {"model": "birdnet", "version": "2.4", "schema": "birdnet_2p4",
     "uri": "s3://bucket/runs/r1/detections.parquet"},
    {"model": "birdnet", "version": "3.0", "schema": "birdnet_3p0",
     "uri": "s3://bucket/runs/r2/detections.parquet"},
    {"model": "perch", "version": "8.0", "schema": "perch_8p0",
     "uri": "s3://bucket/runs/r3/detections.parquet"},
]
GENERATED_SLUG: Dict[str, Any] = {
    "from": "runs",
    "name": "{{row.model}}",
    "version": "{{row.version}}",
    "description": "{{row.model}} {{row.version}}",
    "schema": {
        "name": "{{row.schema}}",
        "tables": {"detections": {"uri": "{{row.uri}}"}},
    },
}


#
# PUBLIC
#
class TestDurations:
    """Refresh intervals."""

    @pytest.mark.parametrize("value, seconds", [
        (None, None), (False, None), (0, None), ("0s", None),
        (30, 30.0), ("45s", 45.0), ("15m", 900.0), ("12h", 43200.0), ("7d", 604800.0),
        ("2w", 1209600.0), ("0.5s", 0.5), (" 3 d ", 259200.0),
    ])
    def test_valid(self, value: Any, seconds: Any) -> None:
        assert parse_duration(value) == seconds

    @pytest.mark.parametrize("value", ["weekly", "7x", True, -1, "1.2.3d"])
    def test_invalid(self, value: Any) -> None:
        with pytest.raises(ValueError):
            parse_duration(value)


class TestParseLookups:
    """Lookup definitions."""

    def test_sql_and_http(self) -> None:
        specs = parse_lookups({
            "runs": {"sql": "SELECT 1", "refresh": "7d", "allow": "s3://b/", "required": True},
            "deploys": {"remote": "core", "version": "1.0", "path": "deployments",
                        "params": {"live": 1}, "headers": {"X": "y"}, "rows": "data.items",
                        "timeout": 5},
            "plain": {"url": "https://example.org/runs.json"},
        }, "test.yaml")
        assert specs["runs"].kind == "sql" and specs["runs"].refresh == 604800.0
        assert specs["runs"].allow == ["s3://b/"] and specs["runs"].required
        assert specs["deploys"].kind == "http" and specs["deploys"].rows == "data.items"
        assert specs["deploys"].version == "1.0" and specs["deploys"].timeout == 5.0
        assert specs["plain"].url == "https://example.org/runs.json"
        assert specs["plain"].refresh is None

    @pytest.mark.parametrize("raw, message", [
        (["x"], "must be a mapping of name"),
        ({"bad-name": {"sql": "SELECT 1"}}, "letters, digits and underscores"),
        ({"x": "SELECT 1"}, "must be a mapping"),
        ({"x": {}}, "exactly one of"),
        ({"x": {"sql": "SELECT 1", "url": "https://x"}}, "exactly one of"),
        ({"x": {"sql": "SELECT 1", "path": "y"}}, "unknown key"),
        ({"x": {"sql": "SELECT {{id}}"}}, "can't use"),
        ({"x": {"sql": " "}}, "non-empty string"),
        ({"x": {"url": "https://x", "rows": 3}}, "dotted path"),
        ({"x": {"url": "https://x", "headers": ["a"]}}, "must be a mapping"),
        ({"x": {"url": "https://x", "timeout": 0}}, "positive number"),
        ({"x": {"sql": "SELECT 1", "refresh": "often"}}, "duration"),
        ({"x": {"sql": "SELECT 1", "required": "yes"}}, "true or false"),
        ({"x": {"sql": "SELECT 1", "allow": [""]}}, "prefixes"),
    ])
    def test_invalid(self, raw: Any, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            parse_lookups(raw, "test.yaml")

    def test_duplicate_across_files(self, tmp_path: Path) -> None:
        _write(tmp_path / "databases" / "config.yaml", {"lookups": {"x": {"sql": "SELECT 1"}}})
        _write(tmp_path / "remotes" / "config.yaml", {"lookups": {"x": {"url": "https://x"}}})
        with pytest.raises(ValueError, match="defined in both"):
            load_lookup_specs(str(tmp_path))


class TestEndpointSettings:
    """settings.lookups."""

    def test_off_by_default(self) -> None:
        assert parse_lookup_settings(None) is None
        assert parse_lookup_settings({"token": "x"}) is None

    def test_env_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ADMIN_TOKEN", "s3cret")
        endpoint = parse_lookup_settings({"refresh_route": "/admin/lookups/",
                                          "token": "env:ADMIN_TOKEN"})
        assert (endpoint.route, endpoint.token) == ("admin/lookups", "s3cret")
        assert check_endpoint_token("Bearer s3cret", "s3cret")
        assert not check_endpoint_token("Bearer nope", "s3cret")
        assert not check_endpoint_token("s3cret", "s3cret")
        assert not check_endpoint_token(None, "s3cret")

    @pytest.mark.parametrize("raw, message", [
        ("x", "must be a mapping"),
        ({"refresh_route": "/a", "extra": 1}, "unknown key"),
        ({"refresh_route": "/a"}, "token"),
        ({"refresh_route": "/", "token": "t"}, "path like"),
        ({"refresh_route": "/a", "token": "env:API_DOCK_TEST_UNSET_TOKEN"}, "not set"),
    ])
    def test_invalid(self, raw: Any, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            parse_lookup_settings(raw)


class TestTemplates:
    """Filling templates and the safety checks."""

    def test_whole_template_keeps_type(self) -> None:
        store = _store()
        row = {"n": 3, "flag": True, "none": None, "nested": {"a": {"b": "deep"}}}
        assert fill_templates("{{row.n}}", row, store) == 3
        assert fill_templates("v{{row.n}}-{{row.flag}}-{{row.none}}", row, store) == "v3-true-"
        assert fill_templates("{{ row.nested.a.b }}", row, store) == "deep"
        assert fill_templates({"x": ["{{row.n}}", "plain {{x}}"]}, row, store) == {
            "x": [3, "plain {{x}}"]
        }

    def test_null_value_leaves_key_out(self) -> None:
        row = {"uri": None, "n": None, "__lookup__": "runs"}
        template = {"tables": {"rev": {"uri": "{{row.uri}}"}, "keep": "s3://x"},
                    "note": "{{row.n}}", "text": "n={{row.n}}"}
        assert fill_templates(template, row, _store()) == {
            "tables": {"keep": "s3://x"}, "text": "n="
        }

    def test_missing_column(self) -> None:
        with pytest.raises(RowTemplateError, match="no column 'nope'"):
            fill_templates("{{row.nope}}", {"a": 1}, _store())

    def test_single_value_lookup(self) -> None:
        store = _store(rows={"current": [{"uri": "s3://bucket/runs/now.parquet"}]})
        assert fill_templates({"uri": "{{lookup.current.uri}}"}, None, store) == {
            "uri": "s3://bucket/runs/now.parquet"
        }

    @pytest.mark.parametrize("rows, message", [
        (None, "no rows yet"),
        ([], "exactly one row"),
        ([{"uri": "a"}, {"uri": "b"}], "exactly one row"),
    ])
    def test_single_value_needs_one_row(self, rows: Any, message: str) -> None:
        store = _store(rows={"current": rows} if rows is not None else {})
        with pytest.raises(ValueError, match=message):
            fill_templates("{{lookup.current.uri}}", None, store)

    def test_unknown_lookup(self) -> None:
        with pytest.raises(ValueError, match="not a defined lookup"):
            fill_templates("{{lookup.missing.uri}}", None, _store())

    @pytest.mark.parametrize("value", [
        "s3://bucket/runs/r1/d.parquet",
        "s3://bucket/runs/",
        "s3://bucket/runs/r1/part=1/*.parquet",
    ])
    def test_allowed_uris(self, value: str) -> None:
        row = {"uri": value, "__lookup__": "runs"}
        assert fill_templates({"uri": "{{row.uri}}"}, row, _store()) == {"uri": value}

    @pytest.mark.parametrize("value, message", [
        ("s3://bucket/runs-secret/x", "allowed prefix"),
        ("s3://other/runs/x", "allowed prefix"),
        ("s3://bucket/runs/../secret/x", "'..'"),
        ("s3://bucket/runs/%2e%2e/secret/x", "'..'"),
        ("s3://bucket/runs/%252e%252e/secret/x", "'..'"),
        ("s3://bucket/runs/x' AS t; DROP", "unsafe characters"),
        ("s3://bucket/runs/x\nnext", "unsafe characters"),
        (42, "must be text"),
    ])
    def test_refused_uris(self, value: Any, message: str) -> None:
        row = {"uri": value, "__lookup__": "runs"}
        with pytest.raises(RowTemplateError, match=message):
            fill_templates({"uri": "{{row.uri}}"}, row, _store())

    def test_plain_table_string_is_a_uri(self) -> None:
        row = {"uri": "s3://elsewhere/x", "__lookup__": "runs"}
        with pytest.raises(RowTemplateError, match="allowed prefix"):
            fill_templates({"tables": {"t": "{{row.uri}}"}}, row, _store())

    def test_uri_needs_allow(self) -> None:
        store = _store(allow=[])
        with pytest.raises(ValueError, match="needs an 'allow' list"):
            fill_templates({"url": "{{row.uri}}"}, {"uri": "https://x", "__lookup__": "runs"},
                           store)


class TestSlugExpansion:
    """``from:`` in ``slugs`` and ``versions``, and inline schemas."""

    def test_generated_slugs(self, tmp_path: Path) -> None:
        _shared(tmp_path, slugs=[GENERATED_SLUG])
        _rows(tmp_path, RUNS)
        configs = get_slug_configs(str(tmp_path))
        assert set(configs) == {"birdnet", "perch"}
        assert set(configs["birdnet"]) == {"2.4", "3.0"}
        assert configs["birdnet"]["3.0"]["schema"] == "birdnet_3p0"
        assert configs["birdnet"]["3.0"]["description"] == "birdnet 3.0"
        schemas = load_shared_config(str(tmp_path))["database"]["schema"]
        assert schemas["perch_8p0"] == {"detections": {"uri": RUNS[2]["uri"]}}
        assert schemas["static_schema"] == {"t": "s3://x/t.parquet"}
        assert get_database_versions("birdnet", str(tmp_path)) == ["2.4", "3.0"]

    def test_no_rows_generates_nothing(self, tmp_path: Path) -> None:
        _shared(tmp_path, slugs=[GENERATED_SLUG])
        _rows(tmp_path, None)
        assert get_slug_configs(str(tmp_path)) == {}

    def test_static_entry_wins(self, tmp_path: Path) -> None:
        static = {"name": "birdnet", "version": "2.4", "description": "static",
                  "schema": "static_schema"}
        _shared(tmp_path, slugs=[GENERATED_SLUG, static])
        _rows(tmp_path, RUNS)
        configs = get_slug_configs(str(tmp_path))
        assert configs["birdnet"]["2.4"]["description"] == "static"
        assert configs["birdnet"]["3.0"]["description"] == "birdnet 3.0"

    def test_version_file_wins(self, tmp_path: Path) -> None:
        _shared(tmp_path, slugs=[GENERATED_SLUG])
        _rows(tmp_path, RUNS)
        _write(tmp_path / "databases" / "birdnet" / "2.4.yaml", {"description": "file"})
        assert load_database_config("birdnet", str(tmp_path), "2.4")["description"] == "file"
        assert get_database_versions("birdnet", str(tmp_path)) == ["2.4", "3.0"]

    def test_duplicate_rows_keep_first(self, tmp_path: Path, caplog: Any) -> None:
        _shared(tmp_path, slugs=[GENERATED_SLUG])
        _rows(tmp_path, RUNS + [{**RUNS[0], "uri": RUNS[1]["uri"]}])
        # the duplicate row's schema clashes (same name, other tables): skipped
        with caplog.at_level("WARNING"):
            configs = get_slug_configs(str(tmp_path))
        assert configs["birdnet"]["2.4"]["schema"] == "birdnet_2p4"
        assert "already defined with different tables" in caplog.text

    @pytest.mark.parametrize("bad", [
        {"model": "bird net", "version": "1.0", "schema": "s1", "uri": RUNS[0]["uri"]},
        {"model": "x", "version": "1/0", "schema": "s2", "uri": RUNS[0]["uri"]},
        {"model": "x", "version": "1.0", "schema": "s3", "uri": "s3://elsewhere/x.parquet"},
        {"model": "x", "version": "1.0", "schema": "bad.name", "uri": RUNS[0]["uri"]},
        {"version": "1.0", "schema": "s4", "uri": RUNS[0]["uri"]},
    ])
    def test_bad_rows_skipped(self, tmp_path: Path, bad: Dict[str, Any], caplog: Any) -> None:
        _shared(tmp_path, slugs=[GENERATED_SLUG])
        _rows(tmp_path, [RUNS[2], bad])
        with caplog.at_level("WARNING"):
            assert set(get_slug_configs(str(tmp_path))) == {"perch"}
        assert "row 1 skipped" in caplog.text

    def test_versions_from_with_static_versions(self, tmp_path: Path) -> None:
        slug = {
            "name": "birdnet",
            "description": "BirdNET",
            "versions": [
                {"version": "1.0", "schema": "static_schema"},
                {"from": "runs", "version": "{{row.version}}",
                 "schema": {"name": "bn_{{row.schema}}",
                            "tables": {"detections": "{{row.uri}}"}}},
            ],
        }
        _shared(tmp_path, slugs=[slug])
        _rows(tmp_path, RUNS)
        versions = get_slug_configs(str(tmp_path))["birdnet"]
        assert set(versions) == {"1.0", "2.4", "3.0", "8.0"}
        assert versions["2.4"]["description"] == "BirdNET"
        assert versions["2.4"]["schema"] == "bn_birdnet_2p4"

    def test_versions_from_with_no_rows_drops_slug(self, tmp_path: Path) -> None:
        slug = {"name": "birdnet", "versions": {"from": "runs", "version": "{{row.version}}"}}
        _shared(tmp_path, slugs=[slug])
        _rows(tmp_path, [])
        assert get_slug_configs(str(tmp_path)) == {}

    def test_static_inline_schema(self, tmp_path: Path) -> None:
        slug = {"name": "owl", "version": "1.0",
                "schema": {"name": "owl_1", "tables": {"detections": "s3://x/owl.parquet"}}}
        _shared(tmp_path, slugs=[slug])
        assert get_slug_configs(str(tmp_path))["owl"]["1.0"]["schema"] == "owl_1"
        assert "owl_1" in load_shared_config(str(tmp_path))["database"]["schema"]

    def test_static_inline_schema_clash(self, tmp_path: Path) -> None:
        slug = {"name": "owl", "version": "1.0",
                "schema": {"name": "static_schema", "tables": {"other": "s3://x/o.parquet"}}}
        _shared(tmp_path, slugs=[slug])
        with pytest.raises(ValueError, match="already defined"):
            load_shared_config(str(tmp_path))

    @pytest.mark.parametrize("slug, message", [
        ({**GENERATED_SLUG, "routes": [{"route": "x", "sql": "SELECT '{{row.model}}'"}]},
         "can't be used in routes"),
        ({"name": "owl", "version": "{{row.version}}"}, "only allowed in a 'from:' entry"),
        ({**GENERATED_SLUG, "from": "missing"}, "not a defined lookup"),
    ])
    def test_misplaced_templates(self, tmp_path: Path, slug: Dict[str, Any], message: str) -> None:
        _shared(tmp_path, slugs=[slug])
        _rows(tmp_path, RUNS)
        with pytest.raises(ValueError, match=message):
            load_shared_config(str(tmp_path))

    def test_lookup_value_in_shared_tables(self, tmp_path: Path) -> None:
        _shared(tmp_path, tables={"current": {"uri": "{{lookup.runs.uri}}"}})
        _rows(tmp_path, RUNS[:1])
        shared = load_shared_config(str(tmp_path))["database"]
        assert shared["current"] == {"uri": RUNS[0]["uri"]}

    def test_schema_groups_with_generated_schemas(self, tmp_path: Path) -> None:
        _shared(tmp_path, slugs=[GENERATED_SLUG],
                groups={"models": ["birdnet_2p4", "perch_8p0", "static_schema"]})
        _rows(tmp_path, RUNS)
        assert load_shared_config(str(tmp_path))["schema_groups"]["models"][0] == "birdnet_2p4"
        _rows(tmp_path, RUNS[1:])
        with pytest.raises(ValueError, match="unknown schema 'birdnet_2p4'"):
            load_shared_config(str(tmp_path))

    def test_rows_change_expansion(self, tmp_path: Path) -> None:
        _shared(tmp_path, slugs=[GENERATED_SLUG])
        _rows(tmp_path, RUNS[:1])
        assert set(get_slug_configs(str(tmp_path))) == {"birdnet"}
        get_store(str(tmp_path)).set_rows("runs", RUNS)
        assert set(get_slug_configs(str(tmp_path))) == {"birdnet", "perch"}


class TestMainConfigNames:
    """``- from: <lookup>`` in the main config's ``databases``."""

    def test_generated_names(self, tmp_path: Path) -> None:
        _shared(tmp_path, slugs=[GENERATED_SLUG])
        _rows(tmp_path, RUNS)
        main = {"databases": ["static_db", {"from": "runs"}, "birdnet"]}
        assert get_database_names(main, str(tmp_path)) == ["static_db", "birdnet", "perch"]


class TestVersionFiles:
    """``{{lookup.*}}`` in database and remote version files."""

    def test_database_file(self, tmp_path: Path) -> None:
        _shared(tmp_path)
        _rows(tmp_path, RUNS[:1])
        _write(tmp_path / "databases" / "db.yaml",
               {"tables": {"d": {"uri": "{{lookup.runs.uri}}"}}, "description": "v {{x}}"})
        config = load_database_config("db", str(tmp_path))
        assert config == {"tables": {"d": {"uri": RUNS[0]["uri"]}}, "description": "v {{x}}"}

    def test_database_file_needs_one_row(self, tmp_path: Path) -> None:
        _shared(tmp_path)
        _rows(tmp_path, RUNS)
        _write(tmp_path / "databases" / "db.yaml", {"tables": {"d": "{{lookup.runs.uri}}"}})
        with pytest.raises(ValueError, match="db.yaml.*exactly one row"):
            load_database_config("db", str(tmp_path))

    def test_template_in_sql_refused(self, tmp_path: Path) -> None:
        _shared(tmp_path)
        _rows(tmp_path, RUNS[:1])
        _write(tmp_path / "databases" / "db.yaml",
               {"routes": [{"route": "x", "sql": "SELECT '{{lookup.runs.uri}}'"}]})
        with pytest.raises(ValueError, match="can't be used in routes"):
            load_database_config("db", str(tmp_path))

    def test_remote_file(self, tmp_path: Path) -> None:
        _remote_lookup(tmp_path, [{"url": "https://api.example.org/v2"}])
        _write(tmp_path / "remotes" / "svc.yaml", {"name": "svc", "url": "{{lookup.hosts.url}}"})
        config = find_remote_config("svc", {"remotes": ["svc"]}, str(tmp_path))
        assert config["url"] == "https://api.example.org/v2"


class TestRemoteExpansion:
    """``versions: {from: ...}`` in remotes/config.yaml."""

    def test_generated_versions(self, tmp_path: Path) -> None:
        remotes = {"wolf": {
            "description": "Wolves",
            "versions": [
                {"version": "0.1", "url": "https://beta.example.org"},
                {"from": "hosts", "version": "{{row.version}}", "url": "{{row.url}}"},
            ],
        }}
        rows = [{"version": "1.0", "url": "https://v1.example.org"},
                {"version": "0.1", "url": "https://dupe.example.org"},
                {"version": "2.0", "url": "https://elsewhere.org"}]
        _remote_lookup(tmp_path, rows, remotes=remotes)
        versions = get_inline_remote_configs(str(tmp_path))["wolf"]
        assert set(versions) == {"0.1", "1.0"}
        assert versions["0.1"]["url"] == "https://beta.example.org"
        assert versions["1.0"] == {"name": "wolf", "description": "Wolves",
                                   "url": "https://v1.example.org"}


#
# INTERNAL
#
def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def _store(rows: Dict[str, Any] = None, allow: List[str] = None) -> LookupStore:
    store = LookupStore("/nonexistent")
    store.configure(parse_lookups({
        "runs": {"sql": "SELECT 1", "allow": ALLOW if allow is None else allow},
        "current": {"sql": "SELECT 1", "allow": ALLOW},
    }, "test"), LookupContext("/nonexistent"))
    for name, value in (rows or {}).items():
        store.set_rows(name, value)
    return store


def _shared(root: Path, slugs: List[Any] = None, tables: Dict[str, Any] = None,
            groups: Dict[str, List[str]] = None) -> None:
    _write(root / "databases" / "config.yaml", {
        "lookups": {"runs": {"sql": "SELECT 1", "allow": ALLOW}},
        "database": {**(tables or {}), "schema": {"static_schema": {"t": "s3://x/t.parquet"}}},
        "slugs": slugs or [],
        "schema_groups": groups or {},
    })


def _rows(root: Path, rows: Any) -> None:
    store = get_store(str(root))
    store.configure(load_lookup_specs(str(root)), LookupContext(str(root)))
    if rows is not None:
        store.set_rows("runs", rows)


def _remote_lookup(root: Path, rows: List[Dict[str, Any]], remotes: Dict[str, Any] = None) -> None:
    _write(root / "remotes" / "config.yaml", {
        "lookups": {"hosts": {"url": "https://lookup.example.org",
                              "allow": ["https://v1.example.org", "https://api.example.org/"]}},
        "remotes": remotes or {},
    })
    store = get_store(str(root))
    store.configure(load_lookup_specs(str(root)), LookupContext(str(root)))
    store.set_rows("hosts", rows)

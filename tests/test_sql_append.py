#!/usr/bin/env python3
"""
Tests for sql_append functionality in sql_builder.

Tests cover:
- build_append_clause_from_params: basic, defaults, optional, cross-param refs
- build_sql_query integration: WHERE + append combined
- _substitute_variables_raw: unquoted substitution
- _sanitize_sql_identifier: injection prevention
- _apply_default_values: value-only params
- build_where_clause_from_params: skips sql_append and value-only params
- validate_route_config: accepts sql_append

License: BSD 3-Clause
"""
import sys
sys.path.insert(0, '/workspace/api_dock')

from api_dock.sql_builder import (
    build_append_clause_from_params,
    build_where_clause_from_params,
    build_sql_query,
    _substitute_variables_raw,
    _sanitize_sql_identifier,
    _apply_default_values,
)
from api_dock.database_config import validate_route_config, merge_query_params


SIMPLE_DB_CONFIG = {
    "tables": {
        "users": "test/users.parquet",
    }
}


passed = 0
failed = 0


def check(test_name, actual, expected):
    global passed, failed
    if actual == expected:
        print(f"  ✅ {test_name}")
        passed += 1
    else:
        print(f"  ❌ {test_name}")
        print(f"     expected: {expected!r}")
        print(f"     actual:   {actual!r}")
        failed += 1


def check_raises(test_name, func, *args, exc_type=ValueError):
    global passed, failed
    try:
        func(*args)
        print(f"  ❌ {test_name} (no exception raised)")
        failed += 1
    except exc_type:
        print(f"  ✅ {test_name}")
        passed += 1
    except Exception as e:
        print(f"  ❌ {test_name} (wrong exception: {type(e).__name__}: {e})")
        failed += 1


def check_in(test_name, haystack, needle):
    global passed, failed
    if needle in haystack:
        print(f"  ✅ {test_name}")
        passed += 1
    else:
        print(f"  ❌ {test_name}")
        print(f"     '{needle}' not found in '{haystack}'")
        failed += 1


def check_not_in(test_name, haystack, needle):
    global passed, failed
    if needle not in haystack:
        print(f"  ✅ {test_name}")
        passed += 1
    else:
        print(f"  ❌ {test_name}")
        print(f"     '{needle}' unexpectedly found in '{haystack}'")
        failed += 1


# ===========================================================================
print("\n_sanitize_sql_identifier")
print("=" * 50)

check("simple column name", _sanitize_sql_identifier("name"), "name")
check("qualified column", _sanitize_sql_identifier("u.created_date"), "u.created_date")
check("ASC keyword", _sanitize_sql_identifier("ASC"), "ASC")
check("DESC keyword", _sanitize_sql_identifier("DESC"), "DESC")
check("integer string", _sanitize_sql_identifier("100"), "100")
check_raises("rejects semicolon", _sanitize_sql_identifier, "name; DROP TABLE users")
check_raises("rejects quotes", _sanitize_sql_identifier, "name' OR '1'='1")
check_raises("rejects double dash comment", _sanitize_sql_identifier, "name -- comment")


# ===========================================================================
print("\n_substitute_variables_raw")
print("=" * 50)

result = _substitute_variables_raw("ORDER BY {{col}}", {"col": "name"})
check("basic substitution", result, "ORDER BY name")

result = _substitute_variables_raw("LIMIT {{limit}}", {"limit": "10"})
check("no quoting", result, "LIMIT 10")
check("no single quotes in result", "'" in result, False)

result = _substitute_variables_raw("ORDER BY {{sort}} {{dir}}", {"sort": "age", "dir": "ASC"})
check("multiple placeholders", result, "ORDER BY age ASC")

result = _substitute_variables_raw("LIMIT {{limit}}", {})
check("missing param left as placeholder", result, "LIMIT {{limit}}")


# ===========================================================================
print("\n_apply_default_values")
print("=" * 50)

route = {"query_params": [{"sort_direction": {"default": "DESC"}}]}
result = _apply_default_values(route, {"sort": "name"})
check("applies missing defaults", result, {"sort": "name", "sort_direction": "DESC"})

result = _apply_default_values(route, {"sort_direction": "ASC"})
check("does not override provided", result, {"sort_direction": "ASC"})

result = _apply_default_values({}, {"a": "1"})
check("no query_params section", result, {"a": "1"})

route = {"query_params": [{"limit": {"sql_append": "LIMIT {{limit}}", "default": 50}}]}
result = _apply_default_values(route, {})
check("converts default to string", result, {"limit": "50"})


# ===========================================================================
print("\nbuild_append_clause_from_params")
print("=" * 50)

# basic with default
route = {"query_params": [{"limit": {"sql_append": "LIMIT {{limit}}", "default": 100}}]}
result = build_append_clause_from_params(route, {}, {})
check("default kicks in", result, ["LIMIT 100"])

# override default
result = build_append_clause_from_params(route, {"limit": "25"}, {})
check("override default", result, ["LIMIT 25"])

# optional not provided
route = {"query_params": [{"offset": {"sql_append": "OFFSET {{offset}}"}}]}
result = build_append_clause_from_params(route, {}, {})
check("optional not provided", result, [])

# optional provided
result = build_append_clause_from_params(route, {"offset": "20"}, {})
check("optional provided", result, ["OFFSET 20"])

# ordering preserved
route = {
    "query_params": [
        {"sort": {"sql_append": "ORDER BY {{sort}}", "default": "id"}},
        {"limit": {"sql_append": "LIMIT {{limit}}", "default": 50}},
        {"offset": {"sql_append": "OFFSET {{offset}}"}},
    ]
}
result = build_append_clause_from_params(route, {"offset": "10"}, {})
check("ordering preserved", result, ["ORDER BY id", "LIMIT 50", "OFFSET 10"])

# cross-param variable reference (both defaults)
route = {
    "query_params": [
        {"sort": {"sql_append": "ORDER BY {{sort}} {{sort_direction}}", "default": "created_date"}},
        {"sort_direction": {"default": "DESC"}},
    ]
}
result = build_append_clause_from_params(route, {}, {})
check("cross-param defaults", result, ["ORDER BY created_date DESC"])

# cross-param with overrides
result = build_append_clause_from_params(route, {"sort": "name", "sort_direction": "ASC"}, {})
check("cross-param overrides", result, ["ORDER BY name ASC"])

# skips sql params
route = {
    "query_params": [
        {"age": {"sql": "age = {{age}}"}},
        {"limit": {"sql_append": "LIMIT {{limit}}", "default": 10}},
    ]
}
result = build_append_clause_from_params(route, {"age": "25"}, {})
check("skips sql params", result, ["LIMIT 10"])


# ===========================================================================
print("\nbuild_where_clause_from_params — skips sql_append and value-only")
print("=" * 50)

route = {
    "query_params": [
        {"age": {"sql": "age = {{age}}"}},
        {"limit": {"sql_append": "LIMIT {{limit}}", "default": 10}},
    ]
}
result = build_where_clause_from_params(route, {"age": "25"}, {})
check("skips sql_append params (length)", len(result), 1)
check_in("only sql param present", result[0], "age")

route = {
    "query_params": [
        {"age": {"sql": "age = {{age}}"}},
        {"sort_direction": {"default": "DESC"}},
    ]
}
result = build_where_clause_from_params(route, {"age": "30"}, {})
check("skips value-only params (length)", len(result), 1)
check_in("only sql param present (2)", result[0], "age")


# ===========================================================================
print("\nbuild_sql_query integration — WHERE + append combined")
print("=" * 50)

# WHERE and append combined
route = {
    "sql": "SELECT * FROM [[users]]",
    "query_params": [
        {"age": {"sql": "age = {{age}}"}},
        {"sort": {"sql_append": "ORDER BY {{sort}}", "default": "id"}},
        {"limit": {"sql_append": "LIMIT {{limit}}", "default": 100}},
    ]
}
result = build_sql_query(route, SIMPLE_DB_CONFIG, path_params={}, query_params={"age": "25"})
check_in("has WHERE", result, "WHERE age = 25")
check_in("has ORDER BY", result, "ORDER BY id")
check_in("has LIMIT", result, "LIMIT 100")
where_pos = result.index("WHERE")
order_pos = result.index("ORDER BY")
limit_pos = result.index("LIMIT")
check("WHERE < ORDER BY < LIMIT", where_pos < order_pos < limit_pos, True)

# append only, no WHERE
route = {
    "sql": "SELECT * FROM [[users]]",
    "query_params": [
        {"sort": {"sql_append": "ORDER BY {{sort}}", "default": "name"}},
        {"limit": {"sql_append": "LIMIT {{limit}}", "default": 10}},
    ]
}
result = build_sql_query(route, SIMPLE_DB_CONFIG, path_params={}, query_params={})
check_not_in("no WHERE", result, "WHERE")
check_in("has ORDER BY (no where)", result, "ORDER BY name")
check_in("has LIMIT (no where)", result, "LIMIT 10")

# no query_params at all — backward compat
route = {"sql": "SELECT * FROM [[users]]"}
result = build_sql_query(route, SIMPLE_DB_CONFIG, path_params={}, query_params={})
check_in("backward compat has SELECT", result, "SELECT * FROM")
check_not_in("backward compat no WHERE", result, "WHERE")
check_not_in("backward compat no ORDER BY", result, "ORDER BY")

# sort with direction cross-param — overrides
route = {
    "sql": "SELECT * FROM [[users]]",
    "query_params": [
        {"sort": {"sql_append": "ORDER BY {{sort}} {{sort_direction}}", "default": "created_date"}},
        {"sort_direction": {"default": "DESC"}},
        {"limit": {"sql_append": "LIMIT {{limit}}", "default": 50}},
    ]
}
result = build_sql_query(
    route, SIMPLE_DB_CONFIG, path_params={},
    query_params={"sort": "age", "sort_direction": "ASC", "limit": "20"}
)
check_in("sort+dir override", result, "ORDER BY age ASC")
check_in("limit override", result, "LIMIT 20")

# sort with direction cross-param — all defaults
result = build_sql_query(route, SIMPLE_DB_CONFIG, path_params={}, query_params={})
check_in("sort+dir defaults", result, "ORDER BY created_date DESC")

# existing WHERE in base SQL + append
route = {
    "sql": "SELECT * FROM [[users]] WHERE active = true",
    "query_params": [
        {"age": {"sql": "age = {{age}}"}},
        {"limit": {"sql_append": "LIMIT {{limit}}", "default": 10}},
    ]
}
result = build_sql_query(route, SIMPLE_DB_CONFIG, path_params={}, query_params={"age": "30"})
check_in("existing WHERE gets AND", result, "WHERE active = true AND age = 30")
check("ends with LIMIT 10", result.strip().endswith("LIMIT 10"), True)


# ===========================================================================
print("\nvalidate_route_config — accepts sql_append")
print("=" * 50)

config = {
    "route": "users",
    "sql": "SELECT * FROM users",
    "query_params": [
        {"limit": {"sql_append": "LIMIT {{limit}}", "default": 10}},
    ]
}
check("accepts sql_append", validate_route_config(config), True)

config = {
    "route": "users",
    "sql": "SELECT * FROM users",
    "query_params": [
        {"sort_direction": {"default": "DESC"}},
    ]
}
check("accepts value-only param", validate_route_config(config), True)

config = {
    "route": "users",
    "sql": "SELECT * FROM users",
    "query_params": [
        {"age": {"sql": "age = {{age}}"}},
        {"sort": {"sql_append": "ORDER BY {{sort}}", "default": "id"}},
        {"sort_direction": {"default": "DESC"}},
        {"limit": {"sql_append": "LIMIT {{limit}}", "default": 50}},
    ]
}
check("accepts mixed sql + sql_append + value-only", validate_route_config(config), True)


# ===========================================================================
print("\nmerge_query_params — top-level query_params")
print("=" * 50)

# no top-level — returns original
route = {"route": "users", "sql": "SELECT * FROM users", "query_params": [{"age": {"sql": "age = {{age}}"}}]}
db = {"tables": {"users": "test.parquet"}}
merged = merge_query_params(route, db)
check("no top-level returns original", merged is route, True)

# top-level applied when route has no query_params
route = {"route": "users", "sql": "SELECT * FROM users"}
db = {"tables": {"users": "test.parquet"}, "query_params": [{"limit": {"sql_append": "LIMIT {{limit}}", "default": 10}}]}
merged = merge_query_params(route, db)
check("top-level applied to bare route (length)", len(merged["query_params"]), 1)
check("top-level applied to bare route (name)", next(iter(merged["query_params"][0])), "limit")

# top-level appended after route params
route = {
    "route": "users", "sql": "SELECT * FROM users",
    "query_params": [{"sort": {"sql_append": "ORDER BY {{sort}}", "default": "id"}}]
}
db = {"tables": {"users": "test.parquet"}, "query_params": [{"limit": {"sql_append": "LIMIT {{limit}}", "default": 50}}]}
merged = merge_query_params(route, db)
check("appended after route params (length)", len(merged["query_params"]), 2)
check("route param first", next(iter(merged["query_params"][0])), "sort")
check("top-level param second", next(iter(merged["query_params"][1])), "limit")

# route overrides top-level with same name
route = {
    "route": "users", "sql": "SELECT * FROM users",
    "query_params": [{"limit": {"sql_append": "LIMIT {{limit}}", "default": 999}}]
}
db = {"tables": {"users": "test.parquet"}, "query_params": [{"limit": {"sql_append": "LIMIT {{limit}}", "default": 10}}]}
merged = merge_query_params(route, db)
check("override same name (length)", len(merged["query_params"]), 1)
check("override keeps route version", merged["query_params"][0]["limit"]["default"], 999)

# partial override — only matching names are skipped
route = {
    "route": "users", "sql": "SELECT * FROM users",
    "query_params": [{"limit": {"sql_append": "LIMIT {{limit}}", "default": 20}}]
}
db = {
    "tables": {"users": "test.parquet"},
    "query_params": [
        {"limit": {"sql_append": "LIMIT {{limit}}", "default": 10}},
        {"offset": {"sql_append": "OFFSET {{offset}}"}},
    ]
}
merged = merge_query_params(route, db)
check("partial override (length)", len(merged["query_params"]), 2)
check("partial override keeps route limit", merged["query_params"][0]["limit"]["default"], 20)
check("partial override adds offset", next(iter(merged["query_params"][1])), "offset")

# original route_config is not mutated
route = {"route": "users", "sql": "SELECT * FROM users", "query_params": [{"age": {"sql": "age = {{age}}"}}]}
db = {"tables": {"users": "test.parquet"}, "query_params": [{"limit": {"sql_append": "LIMIT {{limit}}", "default": 10}}]}
merged = merge_query_params(route, db)
check("original not mutated", len(route["query_params"]), 1)
check("merged has both", len(merged["query_params"]), 2)


# ===========================================================================
print("\nmerge_query_params — integration with build_sql_query")
print("=" * 50)

# top-level limit applied via build_sql_query
route = {"sql": "SELECT * FROM [[users]]"}
db = {"tables": {"users": "test/users.parquet"}, "query_params": [{"limit": {"sql_append": "LIMIT {{limit}}", "default": 25}}]}
merged = merge_query_params(route, db)
result = build_sql_query(merged, db, path_params={}, query_params={})
check_in("integration: has LIMIT from top-level", result, "LIMIT 25")

# route sort + top-level limit
route = {
    "sql": "SELECT * FROM [[users]]",
    "query_params": [{"sort": {"sql_append": "ORDER BY {{sort}}", "default": "name"}}]
}
db = {"tables": {"users": "test/users.parquet"}, "query_params": [{"limit": {"sql_append": "LIMIT {{limit}}", "default": 50}}]}
merged = merge_query_params(route, db)
result = build_sql_query(merged, db, path_params={}, query_params={})
check_in("integration: has ORDER BY from route", result, "ORDER BY name")
check_in("integration: has LIMIT from top-level", result, "LIMIT 50")
order_pos = result.index("ORDER BY")
limit_pos = result.index("LIMIT")
check("integration: ORDER BY before LIMIT", order_pos < limit_pos, True)

# route overrides top-level limit
route = {
    "sql": "SELECT * FROM [[users]]",
    "query_params": [{"limit": {"sql_append": "LIMIT {{limit}}", "default": 999}}]
}
db = {"tables": {"users": "test/users.parquet"}, "query_params": [{"limit": {"sql_append": "LIMIT {{limit}}", "default": 10}}]}
merged = merge_query_params(route, db)
result = build_sql_query(merged, db, path_params={}, query_params={})
check_in("integration: route limit overrides top-level", result, "LIMIT 999")
check_not_in("integration: top-level limit not present", result, "LIMIT 10")

# WHERE from route + LIMIT from top-level
route = {
    "sql": "SELECT * FROM [[users]]",
    "query_params": [{"age": {"sql": "age = {{age}}"}}]
}
db = {"tables": {"users": "test/users.parquet"}, "query_params": [{"limit": {"sql_append": "LIMIT {{limit}}", "default": 100}}]}
merged = merge_query_params(route, db)
result = build_sql_query(merged, db, path_params={}, query_params={"age": "30"})
check_in("integration: WHERE from route", result, "WHERE age = 30")
check_in("integration: LIMIT from top-level after WHERE", result, "LIMIT 100")


# ===========================================================================
print("\n" + "=" * 50)
print(f"Results: {passed} passed, {failed} failed")
if failed == 0:
    print("🎉 All sql_append tests passed!")
else:
    print(f"💥 {failed} test(s) failed")
    sys.exit(1)

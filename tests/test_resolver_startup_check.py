"""

Startup checks for resolvers, templated tables and resolver headers.

Each bad config stops ``RouteMapper`` from starting. The error names the
database, the version, the route or resolver (or table), and the reason.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import copy
import logging
from pathlib import Path
from typing import Any, Dict

import pytest

from api_dock.route_mapper import RouteMapper
from tests.conftest import write_config, write_yaml


#
# CONSTANTS
#
SHOP: str = "Database 'shop' version '1.0'"

TABLE: Dict[str, Any] = {"uri": "{{res.u}}", "format": "parquet", "allow": ["/data/"]}


#
# FIXTURES
#
def _shop(**changes: Any) -> Dict[str, Any]:
    """Build a shop config with resolver ``res`` and route ``items/{{id}}``.

    Args:
        **changes: Top-level keys to add or replace.

    Returns:
        Database config.
    """
    config: Dict[str, Any] = {
        "name": "shop",
        "resolvers": {"res": {"via": "catalog/1.0/lookup", "bind": ["v", "u"]}},
        "routes": [{"route": "items/{{id}}", "resolve": ["res"], "sql": "SELECT {{res.v}} AS v"}],
    }
    config.update(changes)
    return config


def _catalog(**changes: Any) -> Dict[str, Any]:
    """Build an internal catalog config with route ``lookup``.

    Args:
        **changes: Top-level keys to add or replace.

    Returns:
        Database config.
    """
    config: Dict[str, Any] = {
        "name": "catalog", "internal": True,
        "routes": [{"route": "lookup", "sql": "SELECT 'x' AS v, '/data/' AS u"}],
    }
    config.update(changes)
    return config


def _write(root: Path, shop: Dict[str, Any], catalog: Any = None,
           extra: Any = None) -> str:
    """Write a versioned shop and catalog, plus extra databases.

    Args:
        root: Test folder.
        shop: Shop 1.0 config.
        catalog: Catalog 1.0 config; _catalog() if None.
        extra: More databases for write_config.

    Returns:
        Path of config.yaml.
    """
    databases = {
        "shop": {"versions": {"1.0": shop}},
        "catalog": {"versions": {"1.0": catalog or _catalog()}},
        **(extra or {}),
    }
    return write_config(root, databases)


def _route(config: Dict[str, Any], **changes: Any) -> Dict[str, Any]:
    """Change the first route of a config, in a copy.

    Args:
        config: Database config.
        **changes: Route keys to add or replace.

    Returns:
        The changed copy.
    """
    changed = copy.deepcopy(config)
    changed["routes"][0].update(changes)
    return changed


def _resolver(config: Dict[str, Any], **changes: Any) -> Dict[str, Any]:
    """Change resolver ``res`` of a config, in a copy.

    Args:
        config: Database config.
        **changes: Resolver keys to add or replace.

    Returns:
        The changed copy.
    """
    changed = copy.deepcopy(config)
    changed["resolvers"]["res"].update(changes)
    return changed


def _fails(config_path: str, *parts: str) -> None:
    """Check that the mapper does not start, and that the error has each part.

    Args:
        config_path: Path of config.yaml.
        *parts: Texts the error must contain.
    """
    with pytest.raises(ValueError) as error:
        RouteMapper(config_path)
    for part in parts:
        assert part in str(error.value)


#
# PUBLIC
#
class TestValidConfigs:
    """Valid configs start."""

    def test_minimal(self, tmp_path: Path) -> None:
        """The test's base config starts."""
        RouteMapper(_write(tmp_path, _shop()))

    def test_unversioned_target(self, tmp_path: Path) -> None:
        """An unversioned target is named without a version."""
        shop = _resolver(_shop(), via="catalog/lookup")
        config_path = write_config(tmp_path, {
            "shop": {"versions": {"1.0": shop}}, "catalog": _catalog(),
        })
        RouteMapper(config_path)

    def test_postgres_route_with_resolver(self, tmp_path: Path) -> None:
        """A PostgreSQL database can use resolvers; only templated tables are refused."""
        shop = _shop(backend="postgres", connection={
            "host": "localhost", "dbname": "x", "user": "u", "password": "p",
        })
        RouteMapper(_write(tmp_path, shop))


class TestResolverShape:
    """Each resolver has via, optional params and bind, and a valid name."""

    @pytest.mark.parametrize("resolvers, message", [
        (["res"], "resolvers: must be a mapping"),
        ({"bad-name": {"via": "catalog/1.0/lookup", "bind": ["v"]}},
         "'bad-name' is not a valid resolver name"),
        ({"cookies": {"via": "catalog/1.0/lookup", "bind": ["v"]}},
         "a resolver can't be named 'cookies'"),
        ({"res": "catalog/1.0/lookup"}, "resolvers.res: must be a mapping"),
        ({"res": {"via": "catalog/1.0/lookup", "bind": ["v"], "expect": "one"}},
         "resolvers.res: unknown key 'expect'"),
        ({"res": {"bind": ["v"]}}, "resolvers.res: via: is required"),
        ({"res": {"via": "catalog/1.0/lookup/{{id}}", "bind": ["v"]}},
         "resolvers.res: via: can't contain {{variables}}"),
        ({"res": {"via": "catalog/1.0/lookup"}}, "resolvers.res: bind: must be a non-empty list"),
        ({"res": {"via": "catalog/1.0/lookup", "bind": []}}, "bind: must be a non-empty list"),
        ({"res": {"via": "catalog/1.0/lookup", "bind": "v"}}, "bind: must be a non-empty list"),
        ({"res": {"via": "catalog/1.0/lookup", "bind": ["v", "v"]}}, "bind: 'v' is listed twice"),
        ({"res": {"via": "catalog/1.0/lookup", "bind": ["v"], "params": ["x"]}},
         "resolvers.res: params: must be a mapping"),
        ({"res": {"via": "catalog/1.0/lookup", "bind": ["v"], "params": {"x": 1}}},
         "resolvers.res: params.x: must be text"),
    ])
    def test_refused(self, tmp_path: Path, resolvers: Any, message: str) -> None:
        """A malformed resolver stops startup."""
        shop = _shop(resolvers=resolvers, routes=[{"route": "items", "sql": "SELECT 1"}])
        _fails(_write(tmp_path, shop), SHOP, message)

    def test_params_use_cookies(self, tmp_path: Path) -> None:
        """A params: template can't use cookies."""
        shop = _resolver(_shop(), params={"x": "{{cookies.token}}"})
        _fails(_write(tmp_path, shop), SHOP, "resolvers.res: params.x:",
               "can't use '{{cookies.token}}'")

    def test_params_use_resolver_value(self, tmp_path: Path) -> None:
        """A params: template can't use a resolver value."""
        shop = _resolver(_shop(), params={"x": "{{res.v}}"})
        _fails(_write(tmp_path, shop), SHOP, "resolvers.res: params.x:",
               "can't use the resolver value '{{res.v}}'")

    def test_unused_resolver_is_a_warning(
            self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """A resolver that no route lists is logged; startup continues."""
        shop = _shop(routes=[{"route": "items", "sql": "SELECT 1"}])
        with caplog.at_level(logging.WARNING, logger="api_dock"):
            RouteMapper(_write(tmp_path, shop))
        assert f"{SHOP}: resolver 'res' is not listed in any route's resolve:" in caplog.text


class TestVia:
    """via names an existing internal database, an explicit version and a route."""

    @pytest.mark.parametrize("via, message", [
        ("missing/1.0/lookup", "database 'missing' does not exist"),
        ("catalog/latest/lookup", "uses latest"),
        ("catalog/9.9/lookup", "version '9.9' of database 'catalog' does not exist"),
        ("catalog/1.0/nope", "no route of database 'catalog' matches 'nope'"),
    ])
    def test_refused(self, tmp_path: Path, via: str, message: str) -> None:
        """A via that names nothing usable stops startup."""
        _fails(_write(tmp_path, _resolver(_shop(), via=via)), SHOP, "resolver 'res'", message)

    def test_target_not_internal(self, tmp_path: Path) -> None:
        """A via to a public database stops startup."""
        catalog = _catalog()
        del catalog["internal"]
        _fails(_write(tmp_path, _shop(), catalog), SHOP, "resolver 'res'",
               "database 'catalog' is not internal")

    def test_target_internal_false(self, tmp_path: Path) -> None:
        """internal: false is not internal."""
        _fails(_write(tmp_path, _shop(), _catalog(internal=False)), "is not internal")

    def test_target_is_a_remote(self, tmp_path: Path) -> None:
        """A via to a remote stops startup."""
        config_path = _write(tmp_path, _resolver(_shop(), via="core/lookup"))
        write_yaml(tmp_path / "config.yaml", {
            "name": "test", "databases": ["shop", "catalog"], "remotes": ["core"],
        })
        write_yaml(tmp_path / "remotes" / "core.yaml", {"name": "core", "url": "http://x"})
        _fails(config_path, SHOP, "resolver 'res'", "'core' is a remote")

    def test_target_has_resolvers(self, tmp_path: Path) -> None:
        """A target with resolvers: stops startup, so chains can't form."""
        catalog = _catalog(resolvers={"inner": {"via": "shop/1.0/items/1", "bind": ["v"]}})
        _fails(_write(tmp_path, _shop(), catalog), "resolver 'res'",
               "database 'catalog' has resolvers:")


class TestTargetSql:
    """Each {{variable}} in the target route's SQL needs a permitted source."""

    def test_variable_without_source(self, tmp_path: Path) -> None:
        """A variable that no param, default or path variable supplies stops startup."""
        catalog = _catalog(routes=[{"route": "lookup", "sql": "SELECT {{kind}} AS v"}])
        _fails(_write(tmp_path, _shop(), catalog), SHOP, "resolver 'res'",
               "'{{kind}}' in the target route's sql has no value")

    def test_caller_query_key_is_not_a_source(self, tmp_path: Path) -> None:
        """A target query param without a default is not a source."""
        catalog = _catalog(routes=[{
            "route": "lookup", "sql": "SELECT {{kind}} AS v",
            "query_params": [{"kind": {"sql": "1 = 1"}}],
        }])
        _fails(_write(tmp_path, _shop(), catalog), "'{{kind}}' in the target route's sql")

    def test_selector_leaf_and_named_query(self, tmp_path: Path) -> None:
        """Every selector leaf is checked, after [[query]] expansion."""
        catalog = _catalog(queries={"q": "SELECT {{kind}} AS v"}, routes=[{
            "route": "lookup", "sql": [{"when": "x", "then": "SELECT 1 AS v"}, "[[q]]"],
        }])
        _fails(_write(tmp_path, _shop(), catalog), "'{{kind}}' in the target route's sql")

    @pytest.mark.parametrize("shop_changes, catalog_route", [
        ({"params": {"kind": "fixed"}}, {"route": "lookup", "sql": "SELECT {{kind}} AS v"}),
        ({}, {"route": "lookup", "sql": "SELECT {{kind}} AS v",
              "query_params": [{"kind": {"default": "d"}}]}),
        ({"via": "catalog/1.0/lookup/hourly"},
         {"route": "lookup/{{kind}}", "sql": "SELECT {{kind}} AS v"}),
    ])
    def test_sources(self, tmp_path: Path, shop_changes: Dict[str, Any],
                     catalog_route: Dict[str, Any]) -> None:
        """params:, a target default and a target path variable are sources."""
        catalog = _catalog(routes=[catalog_route])
        RouteMapper(_write(tmp_path, _resolver(_shop(), **shop_changes), catalog))

    def test_inherited_default(self, tmp_path: Path) -> None:
        """A default from the target's top-level query_params is a source."""
        catalog = _catalog(
            query_params=[{"kind": {"default": "d"}}],
            routes=[{"route": "lookup", "sql": "SELECT {{kind}} AS v"}],
        )
        RouteMapper(_write(tmp_path, _shop(), catalog))

    def test_injected_cookie(self, tmp_path: Path) -> None:
        """A configured injected cookie of the target is a source."""
        catalog = _catalog(
            cookies=[{"key": "token", "value": "env:CHAIN_STARTUP_UNSET_VARIABLE"}],
            routes=[{"route": "lookup", "sql": "SELECT {{cookies.token}} AS v"}],
        )
        RouteMapper(_write(tmp_path, _shop(), catalog))

    @pytest.mark.parametrize("cookies", [None, True, ["token"], [{"key": "other"}]])
    def test_cookie_not_injected(self, tmp_path: Path, cookies: Any) -> None:
        """A cookie that the target does not inject stops startup."""
        catalog = _catalog(routes=[{"route": "lookup", "sql": "SELECT {{cookies.token}} AS v"}])
        if cookies is not None:
            catalog["cookies"] = cookies
        _fails(_write(tmp_path, _shop(), catalog),
               "'{{cookies.token}}' in the target route's sql is not an injected cookie")


class TestRoutes:
    """Routes list the resolvers whose values they use."""

    @pytest.mark.parametrize("resolve, message", [
        ("res", "resolve: must be a list of resolver names"),
        (["nope"], "resolve: 'nope' is not a resolver of this database"),
        (["res", "res"], "resolve: 'res' is listed twice"),
    ])
    def test_resolve_list(self, tmp_path: Path, resolve: Any, message: str) -> None:
        """resolve: names each defined resolver once."""
        _fails(_write(tmp_path, _route(_shop(), resolve=resolve)), SHOP, "route 'items/{{id}}'",
               message)

    def test_value_without_resolve(self, tmp_path: Path) -> None:
        """A route that uses a value must list its resolver."""
        shop = _shop()
        del shop["routes"][0]["resolve"]
        _fails(_write(tmp_path, shop), SHOP, "route 'items/{{id}}'",
               "uses '{{res.v}}' but does not list 'res' in resolve:")

    @pytest.mark.parametrize("route_changes", [
        {"query_params": [{"q": {"sql": "x = {{res.v}}"}}]},
        {"query_params": [{"q": {"multivalue_sql": "x IN {{res.v}}"}}]},
        {"query_params": [{"q": {"conditional": {"a": {"sql": "x = {{res.v}}"}}}}]},
        {"sql": [{"when": "q", "then": "SELECT {{res.v}}"}, "SELECT 1"]},
        {"headers": {"X-V": "{{res.v}}"}},
    ])
    def test_value_in_other_templates(self, tmp_path: Path, route_changes: Dict[str, Any]) -> None:
        """Fragments, selector leaves and headers count as uses."""
        shop = _route(_shop(), **{"sql": "SELECT 1", **route_changes})
        del shop["routes"][0]["resolve"]
        _fails(_write(tmp_path, shop), "does not list 'res' in resolve:")

    def test_value_through_named_query(self, tmp_path: Path) -> None:
        """A [[query]] that uses a value counts as a use."""
        shop = _route(_shop(queries={"q": "SELECT {{res.v}}"}), sql="[[q]]")
        del shop["routes"][0]["resolve"]
        _fails(_write(tmp_path, shop), "does not list 'res' in resolve:")

    def test_value_through_templated_table(self, tmp_path: Path) -> None:
        """A route that reads a templated table must list the table's resolver."""
        shop = _route(_shop(tables={"t": TABLE}), sql="SELECT * FROM [[t]]")
        del shop["routes"][0]["resolve"]
        _fails(_write(tmp_path, shop), SHOP, "route 'items/{{id}}'",
               "reads table 't', which uses '{{res.u}}', but does not list 'res' in resolve:")

    def test_column_not_bound(self, tmp_path: Path) -> None:
        """A value must be a column in the resolver's bind: list."""
        shop = _route(_shop(), sql="SELECT {{res.w}}")
        _fails(_write(tmp_path, shop), SHOP, "route 'items/{{id}}'",
               "'{{res.w}}' is not in the bind: list of resolver 'res'")

    @pytest.mark.parametrize("route_changes, message", [
        ({"query_params": [{"sort": {"sql_append": "ORDER BY {{res.v}}"}}]},
         "query_params.sort.sql_append: can't use the resolver value '{{res.v}}'"),
        ({"sql": [{"sql": "SELECT 1", "sql_append": "LIMIT {{res.v}}"}]},
         "sql[0].sql_append: can't use the resolver value '{{res.v}}'"),
        ({"query_params": [{"u": {"sql_append": "UNION SELECT * FROM [[t]]"}}]},
         "query_params.u.sql_append: can't read the templated table 't'"),
    ])
    def test_sql_append(self, tmp_path: Path, route_changes: Dict[str, Any],
                        message: str) -> None:
        """sql_append can't use a resolver value, directly or through a table."""
        shop = _route(_shop(tables={"t": TABLE}), **route_changes)
        _fails(_write(tmp_path, shop), SHOP, message)

    def test_params_variable_must_be_declared(self, tmp_path: Path) -> None:
        """A params: variable must be a path variable or declared query param of the route."""
        shop = _resolver(_shop(), params={"kind": "{{kind}}"})
        _fails(_write(tmp_path, shop), SHOP, "route 'items/{{id}}'",
               "resolver 'res' params.kind uses '{{kind}}', which is not a path variable "
               "or query param of the route")

    def test_params_variable_of_every_route(self, tmp_path: Path) -> None:
        """Every route that lists the resolver must declare the params: variables."""
        shop = _resolver(_shop(), params={"kind": "{{kind}}"})
        shop["routes"][0]["query_params"] = [{"kind": {"default": "a"}}]
        shop["routes"].append({"route": "other", "resolve": ["res"], "sql": "SELECT 1"})
        _fails(_write(tmp_path, shop), "route 'other'", "'{{kind}}'")

    @pytest.mark.parametrize("route_changes", [
        {"route": "items/{{kind}}"},
        {"query_params": [{"kind": {"default": "a"}}]},
        {"query_params": [{"kind": {"sql": "1 = 1"}}]},
    ])
    def test_params_variable_declared(self, tmp_path: Path, route_changes: Dict[str, Any]) -> None:
        """A path variable or any declared query param is accepted."""
        shop = _route(_resolver(_shop(), params={"kind": "{{kind}}"}), **route_changes)
        RouteMapper(_write(tmp_path, shop))

    def test_params_variable_from_top_level_query_params(self, tmp_path: Path) -> None:
        """A top-level query param counts for every route."""
        shop = _resolver(_shop(), params={"kind": "{{kind}}"})
        shop["query_params"] = [{"kind": {"default": "a"}}]
        RouteMapper(_write(tmp_path, shop))


class TestTables:
    """Templated tables need format: and allow:, and only DuckDB can have them."""

    @pytest.mark.parametrize("table, message", [
        ({"uri": "s3://x/{{res.u}}", "format": "parquet", "allow": ["s3://x/"]},
         "a templated uri: must be exactly one {{<resolver>.<column>}}"),
        ({"uri": "{{u}}", "format": "parquet", "allow": ["s3://x/"]},
         "a templated uri: must be exactly one {{<resolver>.<column>}}"),
        ({"uri": "{{res.u}}", "allow": ["/data/"]}, "a templated table needs format:"),
        ({"uri": "{{res.u}}", "format": "avro", "allow": ["/data/"]},
         "a templated table needs format:"),
        ({"uri": "{{res.u}}", "format": "parquet"}, "a templated table needs allow:"),
        ({"uri": "{{res.u}}", "format": "parquet", "allow": []}, "a templated table needs allow:"),
        ({"uri": "{{res.u}}", "format": "parquet", "allow": "/data/"},
         "a templated table needs allow:"),
        ({"uri": "{{res.u}}", "path": "/x", "format": "parquet", "allow": ["/data/"]},
         "a templated table can't use path:"),
        ({"path": "{{res.u}}", "format": "parquet", "allow": ["/data/"]},
         "a templated table can't use path:"),
        ("{{res.u}}", "a templated table needs format:"),
        ({"uri": "/data/x.parquet", "format": "parquet"}, "format:, files: and allow: need"),
        ({"uri": "/data/x.parquet", "files": "*.parquet"}, "format:, files: and allow: need"),
        ({"uri": "/data/x.parquet", "allow": ["/data/"]}, "format:, files: and allow: need"),
        ({**TABLE, "allow": ["/data/", "s3://x/"]},
         "allow: prefixes use more than one storage type"),
        ({**TABLE, "files": "/abs/*.parquet"}, "files: can't start with '/'"),
        ({**TABLE, "files": ""}, "files: can't be empty"),
        ({**TABLE, "files": "../*.parquet"}, "files: can't contain a '..' segment"),
        ({**TABLE, "files": 3}, "files: must be text"),
        ({**TABLE, "allow": [""]}, "allow: '' is empty"),
        ({**TABLE, "allow": ["/data/../x/"]}, "allow: '/data/../x/' contains a '..' segment"),
        ({**TABLE, "allow": ["/data/*/"]}, "allow: '/data/*/' contains a wildcard"),
        ({**TABLE, "allow": ["/data/?/"]}, "contains a wildcard"),
        ({**TABLE, "allow": ["/data/[a]/"]}, "contains a wildcard"),
        ({**TABLE, "allow": [3]}, "allow: entries must be text"),
        ({**TABLE, "uri": "{{nope.u}}"}, "'{{nope.u}}' does not name a resolver of this database"),
        ({**TABLE, "uri": "{{res.w}}"}, "'{{res.w}}' is not in the bind: list of resolver 'res'"),
    ])
    def test_refused(self, tmp_path: Path, table: Any, message: str) -> None:
        """Each broken table rule stops startup."""
        _fails(_write(tmp_path, _shop(tables={"t": table})), SHOP, "tables.t:", message)

    def test_postgres(self, tmp_path: Path) -> None:
        """A PostgreSQL database can't have a templated table."""
        shop = _shop(backend="postgres", tables={"t": TABLE}, connection={
            "host": "localhost", "dbname": "x", "user": "u", "password": "p",
        })
        _fails(_write(tmp_path, shop), SHOP, "tables.t:",
               "a PostgreSQL database can't have a templated table")


class TestHeaders:
    """Header templates can use path variables and values of listed resolvers."""

    def test_resolver_value_accepted(self, tmp_path: Path) -> None:
        """A header can use a value of a resolver in resolve:."""
        RouteMapper(_write(tmp_path, _route(_shop(), headers={"ETag": '"{{res.v}}"'})))

    def test_query_param_refused(self, tmp_path: Path) -> None:
        """A header can't use a query param."""
        shop = _route(_shop(), headers={"X-Q": "{{q}}"}, query_params=[{"q": {"default": "a"}}])
        _fails(_write(tmp_path, shop), SHOP, "route 'items/{{id}}'",
               "headers.X-Q: '{{q}}' is not a path variable of the route or a value of a "
               "resolver in its resolve:")

    def test_cookie_refused(self, tmp_path: Path) -> None:
        """A header can't use a cookie."""
        shop = _route(_shop(), headers={"X-C": "{{cookies.session}}"})
        _fails(_write(tmp_path, shop), "headers.X-C: '{{cookies.session}}' is not a path variable")

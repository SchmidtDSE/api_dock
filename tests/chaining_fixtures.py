"""

Shared configs and data for the route-chaining tests.

An internal ``catalog`` database answers ``releases/current`` from a CSV file
of releases. A public ``observations`` database names a ``current_release``
resolver, reads the release's Parquet folder through a templated table and
sends ``ETag`` and ``X-Release-Id`` headers. ``chain()`` builds a smaller pair:
an internal ``catalog`` with one ``lookup`` route and a public ``shop`` whose
``items/{{id}}`` route lists the resolver ``res``.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import csv
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import duckdb

from api_dock.route_mapper import RouteMapper
from api_dock.types import ProxyResponse
from tests.conftest import write_config


#
# CONSTANTS
#
RELEASE_COLUMNS: List[str] = [
    "project", "dataset", "release_id", "data_uri", "schema_version", "validator",
]

RESOLVER_ERROR: Dict[str, str] = {"error": "Resolver error"}
NOT_FOUND: Dict[str, str] = {"error": "Not found"}
UNAVAILABLE: Dict[str, str] = {"error": "Database unavailable"}

# Readings in each release folder, by hive partition.
READINGS: Dict[str, Dict[str, List[Dict[str, Any]]]] = {
    "r-19": {"region=north": [
        {"site_id": 42, "value": 1.5}, {"site_id": 42, "value": 3.0},
        {"site_id": 43, "value": 9.0},
    ]},
    "r-20": {"region=south": [{"site_id": 42, "value": 7.0}]},
}

CATALOG_SQL: str = (
    "SELECT release_id, data_uri, schema_version, validator FROM [[current_release]] "
    "WHERE project = {{project}} AND dataset = {{dataset}}"
)


#
# PUBLIC
#
def write_releases(root: Path, rows: Optional[List[Dict[str, str]]] = None) -> Path:
    """Write the releases CSV file and every release folder.

    Args:
        root: Test folder.
        rows: Release rows; default_releases(root) if None.

    Returns:
        Path of the CSV file.
    """
    for release_id, partitions in READINGS.items():
        for partition, readings in partitions.items():
            write_parquet(root / "releases" / release_id / partition / "part-0.parquet", readings)
    path = root / "releases.csv"
    with path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=RELEASE_COLUMNS, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(default_releases(root) if rows is None else rows)
    return path


def default_releases(root: Path) -> List[Dict[str, str]]:
    """Return one hourly and one daily release for project 7.

    Args:
        root: Test folder.

    Returns:
        Release rows.
    """
    return [
        release_row(root, "project:7", "hourly", "r-19"),
        release_row(root, "project:7", "daily", "r-20"),
    ]


def release_row(root: Path, project: str, dataset: str, release_id: str,
                data_uri: Optional[str] = None) -> Dict[str, str]:
    """Build one row of the releases CSV file.

    Args:
        root: Test folder.
        project: Project key.
        dataset: Dataset name.
        release_id: Release folder name.
        data_uri: Data URI; the release folder, with a trailing slash, if None.

    Returns:
        Release row.
    """
    uri = data_uri if data_uri is not None else f"{root / 'releases' / release_id}/"
    return {
        "project": project, "dataset": dataset, "release_id": release_id,
        "data_uri": uri, "schema_version": "3", "validator": f"{release_id}:abc",
    }


def write_parquet(path: Path, rows: List[Dict[str, Any]]) -> None:
    """Write rows to a Parquet file, creating its folder.

    Args:
        path: File path.
        rows: Rows with the same keys.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    rows_json = path.with_suffix(".json")
    rows_json.write_text(json.dumps(rows))
    source, target = (str(item).replace("'", "''") for item in (rows_json, path))
    duckdb.sql(f"COPY (SELECT * FROM read_json('{source}')) TO '{target}' (FORMAT parquet)")
    rows_json.unlink()


def catalog(releases_csv: Path, **changes: Any) -> Dict[str, Any]:
    """Build the internal DuckDB catalog config.

    Args:
        releases_csv: Path of the releases CSV file.
        **changes: Top-level keys to add or replace.

    Returns:
        Database config.
    """
    config: Dict[str, Any] = {
        "name": "catalog",
        "internal": True,
        "tables": {"current_release": str(releases_csv)},
        "routes": [{"route": "releases/current", "sql": CATALOG_SQL}],
    }
    config.update(changes)
    return config


def observations(root: Path, **changes: Any) -> Dict[str, Any]:
    """Build the public observations config that reads the current release.

    Args:
        root: Test folder; its releases folder is the only allowed prefix.
        **changes: Top-level keys to add or replace.

    Returns:
        Database config.
    """
    config: Dict[str, Any] = {
        "name": "observations",
        "resolvers": {"current_release": {
            "via": "catalog/1.0/releases/current",
            "params": {"project": "project:{{project_id}}", "dataset": "{{dataset}}"},
            "bind": ["release_id", "data_uri", "schema_version", "validator"],
        }},
        "tables": {"readings": {
            "uri": "{{current_release.data_uri}}",
            "format": "parquet",
            "files": "**/*.parquet",
            "allow": [f"{root / 'releases'}/"],
        }},
        "routes": [readings_route()],
    }
    config.update(changes)
    return config


def readings_route(**changes: Any) -> Dict[str, Any]:
    """Build the observations readings route.

    Args:
        **changes: Route keys to add or replace.

    Returns:
        Route config.
    """
    route: Dict[str, Any] = {
        "route": "projects/{{project_id}}/sites/{{site_id}}/readings",
        "resolve": ["current_release"],
        "sql": (
            "SELECT [[readings]].* FROM [[readings]] "
            "WHERE [[readings]].site_id = {{site_id}}"
        ),
        "headers": {
            "ETag": '"{{current_release.validator}}"',
            "X-Release-Id": "{{current_release.release_id}}",
        },
        "query_params": [
            {"dataset": {"default": "hourly"}},
            {"min_value": {"sql": "[[readings]].value >= {{min_value}}"}},
        ],
    }
    route.update(changes)
    return route


def scenario(root: Path, catalog_config: Optional[Dict[str, Any]] = None,
             observations_config: Optional[Dict[str, Any]] = None) -> str:
    """Write the releases data and the catalog and observations configs.

    Args:
        root: Test folder.
        catalog_config: Catalog 1.0 config; catalog() if None.
        observations_config: Observations 2.0 config; observations() if None.

    Returns:
        Path of config.yaml.
    """
    releases_csv = write_releases(root)
    return write_config(root, {
        "catalog": {"versions": {"1.0": catalog_config or catalog(releases_csv)}},
        "observations": {"versions": {"2.0": observations_config or observations(root)}},
    })


def chain(root: Path, target_sql: str, bind: List[str], route_sql: str,
          route: Optional[Dict[str, Any]] = None,
          resolver: Optional[Dict[str, Any]] = None,
          target: Optional[Dict[str, Any]] = None,
          shop: Optional[Dict[str, Any]] = None,
          target_route: Optional[Dict[str, Any]] = None) -> str:
    """Write an internal ``catalog`` with a ``lookup`` route and a public ``shop``.

    The shop's ``items/{{id}}`` route lists the resolver ``res``, which calls
    ``catalog/1.0/lookup``.

    Args:
        root: Test folder.
        target_sql: SQL of the catalog's lookup route.
        bind: The resolver's bind: list.
        route_sql: SQL of the shop route.
        route: Shop route keys to add or replace.
        resolver: Resolver keys to add or replace.
        target: Catalog top-level keys to add or replace.
        shop: Shop top-level keys to add or replace.
        target_route: Catalog lookup route keys to add or replace.

    Returns:
        Path of config.yaml.
    """
    lookup = {"route": "lookup", "sql": target_sql, **(target_route or {})}
    catalog_config = {"name": "catalog", "internal": True, "routes": [lookup], **(target or {})}
    res = {"via": "catalog/1.0/lookup", "bind": bind, **(resolver or {})}
    items = {"route": "items/{{id}}", "resolve": ["res"], "sql": route_sql, **(route or {})}
    shop_config = {"name": "shop", "resolvers": {"res": res}, "routes": [items], **(shop or {})}
    return write_config(root, {
        "catalog": {"versions": {"1.0": catalog_config}},
        "shop": {"versions": {"1.0": shop_config}},
    })


async def get(mapper: RouteMapper, database: str, path: str,
              multi: Optional[Dict[str, List[str]]] = None,
              cookies: Optional[Dict[str, str]] = None, **query: str) -> ProxyResponse:
    """Send a database request through the public mapper method.

    Args:
        mapper: Route mapper.
        database: Database name.
        path: Path after the database name.
        multi: Repeated query params.
        cookies: Request cookies.
        **query: Query params.

    Returns:
        The response.
    """
    return await mapper.map_database_route(
        database_name=database, path=path, query_params=query,
        cookies=cookies or {}, multi_query_params=multi or {},
    )


def body(response: ProxyResponse) -> Any:
    """Decode a JSON response body.

    Args:
        response: The response.

    Returns:
        Decoded JSON.
    """
    return json.loads(response.content)

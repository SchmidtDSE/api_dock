# API Dock

API Dock builds an API from YAML files. Behind one endpoint it proxies requests to **remote HTTP
APIs** and serves **SQL routes** over Parquet/CSV files (local disk, S3, GCS, Azure or HTTPS) and
**PostgreSQL** tables. Run it with the `api-dock` CLI as a FastAPI or Flask app, or embed its
`RouteMapper` in an existing Python service.

| source | served at | how |
|---|---|---|
| remote HTTP API | `/<remote>/<path>` | forwarded upstream and streamed back, with optional allow-lists and restrictions |
| file tables (Parquet, CSV, ...) | `/<database>/<version>/<route>` | SQL run by DuckDB |
| PostgreSQL tables | `/<database>/<version>/<route>` | SQL run natively, or by DuckDB when a route mixes sources |

Request values reach the database as bound parameters, never pasted into SQL, and every database
config is checked when the server starts.

**Documentation lives in the [wiki](https://github.com/SchmidtDSE/api_dock/wiki/).** Start with [Getting Started](https://github.com/SchmidtDSE/api_dock/wiki/Getting-Started) and
[Concepts](https://github.com/SchmidtDSE/api_dock/wiki/Concepts).

---

## Install

```bash
pip install api-dock                     # core
pip install 'api-dock[postgres]'         # adds the PostgreSQL driver and pool
conda install -c conda-forge api_dock    # or from conda-forge
```

API Dock needs Python 3.11 or later.

---

## Quick start

```bash
api-dock init        # creates api_dock_config/ with commented example files
```

Replace the examples with a main config, one remote and one database:

```
api_dock_config/
├── config.yaml
├── remotes/
│   └── httpbin.yaml
└── databases/
    └── places.yaml
```

```yaml
# api_dock_config/config.yaml
name: my-api
description: My first API Dock
remotes:
  - httpbin
databases:
  - places
settings:
  add_trailing_slash: false   # httpbin doesn't want /get/
```

```yaml
# api_dock_config/remotes/httpbin.yaml
name: httpbin
url: https://httpbin.org
```

```yaml
# api_dock_config/databases/places.yaml
tables:
  places: data/places.parquet            # or s3://bucket/places/**/*.parquet, a PostgreSQL table, ...
routes:
  - route: places
    sql: SELECT * FROM [[places]]
    query_params:
      - country:
          sql: "country = {{country}}"
  - route: places/{{id}}
    sql: SELECT * FROM [[places]] WHERE id = {{id}}
```

`[[places]]` is replaced by the table's source; `{{id}}` and `{{country}}` are request values,
sent to the database as bound parameters.

```bash
api-dock start                                    # FastAPI on port 8000
curl http://localhost:8000/httpbin/get            # proxied to https://httpbin.org/get
curl http://localhost:8000/places/places?country=FR
# [{"id": 2, "name": "Lyon", "country": "FR"}]
curl http://localhost:8000/places/places/1
```

[Getting Started](https://github.com/SchmidtDSE/api_dock/wiki/Getting-Started) walks through this example, including making the Parquet
file.

---

## What you can configure

| topic | wiki page |
|---|---|
| main config, settings (`timeout`, `base_path`, `duckdb`, ...), multiple configs | [Configuration](https://github.com/SchmidtDSE/api_dock/wiki/Configuration) |
| versioned remotes and databases, inline versions, `latest` | [Versioning](https://github.com/SchmidtDSE/api_dock/wiki/Versioning) |
| remote configs (files or `remotes/config.yaml`), allow-lists, `restricted` patterns, route mapping | [Routing and Restrictions](https://github.com/SchmidtDSE/api_dock/wiki/Routing-and-Restrictions) |
| cookies, database authentication, encrypted values | [Authentication and Cookies](https://github.com/SchmidtDSE/api_dock/wiki/Authentication-and-Cookies) |
| tables, `[[table]]` references, routes, startup checks | [SQL Database Support](https://github.com/SchmidtDSE/api_dock/wiki/SQL-Database-Support) |
| filtering, sorting, pagination, required params | [Query Parameters](https://github.com/SchmidtDSE/api_dock/wiki/Query-Parameters) |
| picking SQL from the request | [Conditional SQL](https://github.com/SchmidtDSE/api_dock/wiki/Conditional-SQL) |
| shared tables, schemas, slugs, shared routes | [Shared Database Config](https://github.com/SchmidtDSE/api_dock/wiki/Shared-Database-Config) |
| unions across schemas (`[[*.table]]`, schema groups) | [Cross-Schema Queries](https://github.com/SchmidtDSE/api_dock/wiki/Cross-Schema-Queries) |
| PostgreSQL connections, engines, safety | [PostgreSQL](https://github.com/SchmidtDSE/api_dock/wiki/PostgreSQL) |
| databases, versions and values generated from a query, refreshed on a schedule | [Lookups](https://github.com/SchmidtDSE/api_dock/wiki/Lookups) |
| `/databases`, `/remotes`, `/sources` listings | [Catalog Endpoints](https://github.com/SchmidtDSE/api_dock/wiki/Catalog-Endpoints) |
| `RouteMapper` in your own app, production deployment | [Python API and Deployment](https://github.com/SchmidtDSE/api_dock/wiki/Python-API-and-Deployment) |

---

## Example: versions from a catalog (lookups)

When the list of databases/versions lives somewhere else (a table of model runs, a
deployments API), a lookup turns its rows into config. api_dock runs it at startup, every
`refresh` and on demand:

```yaml
# api_dock_config/databases/config.yaml
database:
  connections:
    core: {host: db.example.com, dbname: catalog, user: readonly, password: env:DB_PASSWORD}
  runs_catalog: {connection: core, table: public.model_runs}   # or {uri: s3://.../runs.parquet}

lookups:
  model_runs:
    sql: |
      SELECT name, version, detections_uri,
             replace(name, '-', '_') || '_' || replace(version, '.', 'p') AS schema
      FROM [[runs_catalog]] WHERE published
    refresh: 7d
    allow: ["s3://my-bucket/runs/"]        # URIs from rows must start with this

slugs:
  - from: model_runs                       # one database/version per row
    name: "{{row.name}}"
    version: "{{row.version}}"
    schema:
      name: "{{row.schema}}"
      tables:
        detections: {uri: "{{row.detections_uri}}"}
```

```yaml
# api_dock_config/config.yaml
databases:
  - from: model_runs                       # serve every database the lookup generates
settings:
  lookups:                                 # optional: GET status / POST refresh
    refresh_route: /admin/lookups
    token: env:API_DOCK_ADMIN_TOKEN
```

Lookups can read PostgreSQL tables, Parquet/CSV files or an HTTP API, and can also
generate remote versions or fill single values. See [Lookups](https://github.com/SchmidtDSE/api_dock/wiki/Lookups).

---

## CLI

```bash
api-dock                                    # list configs and commands
api-dock init [--force]                     # create api_dock_config/
api-dock start [config_name]                # serve api_dock_config/<config_name>.yaml (default: config)
api-dock start --backbone flask --host 127.0.0.1 --port 9000 --log-level debug
api-dock describe [config_name]             # print the config
api-dock generate-key                       # local encryption key
api-dock encrypt "secret"                   # also --method env_key|aws_kms
api-dock decrypt "gAAAAA..."
api-dock lookups                            # run the config's lookups and print their rows
```

Flask responses are buffered and Flask refuses configs with PostgreSQL connections; use the default
FastAPI backbone for those. Full reference: [Getting Started](https://github.com/SchmidtDSE/api_dock/wiki/Getting-Started#cli-reference).

---

## How it works (in brief)

- **Configs.** The main `config.yaml` lists remotes and databases and is read at startup. Remote
  files, database files and the shared `remotes/config.yaml` and `databases/config.yaml` are read
  again on each request, so
  route edits don't need a restart.
- **Remotes.** A request is checked against the remote's allow/block lists, then forwarded with
  httpx. The FastAPI app streams the upstream response back.
- **Databases.** The version is resolved (`latest`, version files, `slugs`), the route matched and
  its SQL built: `[[table]]` references expanded, query-param fragments appended, values bound.
- **Engines.** A route whose tables are all on one PostgreSQL connection runs natively through
  that connection's pool. Anything else runs on an in-memory DuckDB in a worker thread, with
  PostgreSQL attached read-only when needed.
- **Lookups.** Named queries (SQL over the configured tables, or an HTTP API) run at startup
  and on a schedule; `from:` entries turn their rows into database versions or remote versions.
- **Startup checks.** Every database and version is checked as requests will see it (table
  references, quoted variables, unions, connections, engines); a bad config stops startup with a
  message naming the database, version and route.

---

## Repo layout

```
api_dock/                   the package
  cli.py                    api-dock commands
  config.py, config_discovery.py   main/remote config loading, settings, route rules
  route_mapper.py           RouteMapper: request handling, startup checks, PostgreSQL lifecycle
  fast_api.py, flask_api.py the two app backbones
  database_config.py        database configs, shared config, versions and slugs
  lookups.py                lookups: templates, SQL/HTTP runners, refresh
  sql_builder.py            SQL building, table references, unions, engine choice
  database_backends.py      DuckDB backend    postgres_backend.py, postgres_config.py   PostgreSQL
  storage_auth.py, auth.py, encryption.py, listings.py, sql_template_check.py, types.py
  example_api_dock_config/  copied by `api-dock init`
tests/                      pixi run -e dev pytest -q
```

More in the [Developer Guide](https://github.com/SchmidtDSE/api_dock/wiki/Developer-Guide).

---

# Development

```bash
pixi install -e dev           # includes psycopg and a local PostgreSQL for the tests
pixi run -e dev pytest -q     # PostgreSQL tests skip if PostgreSQL/psycopg are missing
```

## Publishing a Release

Publishing a GitHub Release is what publishes to PyPI: `.github/workflows/publish_to_pypi.yml` runs on `release: published`, builds the sdist and wheel with `uv build`, and uploads them using PyPI trusted publishing (OIDC). There's no local build, no API token, and no `twine`. conda-forge follows automatically: its bot opens a version PR on [conda-forge/api_dock-feedstock](https://github.com/conda-forge/api_dock-feedstock), which a maintainer merges.

```bash
# 0. Start from a clean, up-to-date main
export VERSION=0.10.0         # the NEW version, no leading "v"
git checkout main
git pull origin main
git status

# 1. Set `version` in pyproject.toml to $VERSION

# 2. Run the tests
pixi run -e dev pytest -q

# 3. Commit, tag, push (the commit command adds the "v$VERSION: " prefix)
export COMMIT_MESSAGE='code review fixes: remote cookies, safer SQL building, auth caching'
git add -A
git commit -m "v$VERSION: $COMMIT_MESSAGE"
git tag "v$VERSION"
git push origin main "v$VERSION"

# 4. Publish the GitHub Release; this triggers the PyPI upload.
#    Don't use --draft (the workflow only runs on a published release); no wheel needs attaching.
gh release create "v$VERSION" \
  --title "v$VERSION" \
  --notes "$(cat <<'EOF'
**Upgrading:** a remote now receives only the cookies its `cookies:` setting allows (none without it). A remote that relied on the browser's cookies being passed through needs `cookies: [<name>]`, or `cookies: true` for all of them. Configs that set `action`, an unknown version `schema:`, non-boolean `add_trailing_slash`/`follow_redirects`, or a bad `timeout` now stop at startup.

* new features
    - `api-dock describe` builds the API like `start` (lookups, startup checks) and prints every database/version with expanded route SQL, plus each remote's url
    - `api-dock init --force` replaces existing files; the whole example tree is copied
    - `gcp` extra (`pip install 'api_dock[gcp]'`) for GCP Secret Manager authentication
    - Startup warning when an `authentication` block would be ignored by remote routes
* bug fixes
    - Remote cookies: the client's raw `Cookie` header is no longer forwarded; the upstream `Cookie` header is built from the remote's `cookies` setting, so filtering and injected cookies work even when the client sends cookies
    - Several upstream `Set-Cookie` headers reach the client separately instead of merged into one (new `ProxyResponse.set_cookies`)
    - Query-param filters join the base query's top-level `WHERE` (strings, comments, CTEs and subqueries ignored), parenthesized and before a trailing `GROUP BY`/`ORDER BY`/`LIMIT`: fixes `WHERE a OR b` letting rows past filters, CTE-only `WHERE`s and base queries ending in `ORDER BY`
    - `[[table]]` is a table source only right after `FROM`/`JOIN` or a `FROM`-list comma (was any `FROM`/`JOIN` in the previous 20 characters, which broke `ON [[a]].id = [[b]].id`)
    - `sql_append` values are limited to integers and column names (with `ASC`/`DESC`, `NULLS FIRST/LAST`); the unimplemented `action` (which echoed request values, including cookies) is refused
    - Authentication providers are built once per config instead of on every request (no secret-store/KMS call per request); `refresh_interval` now refreshes, keeping the last values if a refresh fails; tokens compared in constant time; `aws_tokens_file` works (`aws_key_id` optional)
    - Remote route mapping fills `{{route_name}}` and `{{cookies.x}}` in `remote_route`, and keeps a query string written there
    - Include/exclude and listing filters compare versions part by part (`1.10` no longer matches `1.1`)
    - Values in DuckDB secret SQL (S3 region, GCS keys, HTTP headers) are quoted; a second GCS `service_account` no longer overwrites the process-wide one
    - A main config that is missing (explicit path), invalid YAML or not a mapping stops startup instead of serving an empty API; settings are validated at startup
    - An explicit encryption `key_file` must exist (only the default key file falls back to `API_DOCK_ENCRYPTION_KEY`); `env_key` reads only its variable
    - Remote error responses no longer include internal error text (it's logged); `map_route_sync` no longer leaks event loops
* cleanup / other improvements
    - `map_database_route` split into named steps; one version-resolution helper for remotes and databases
    - A remote request reads only that remote's config file (was every remote file, twice)
    - Removed unused internal functions; shared YAML-loading and version-matching helpers; `follow_protocol_downgrades` (never implemented) removed
    - FastAPI app reports the package version; classifiers match Python 3.11+; style fixes
    - Tests grew from 626 to 760, including the first tests for authentication, encryption, config discovery and storage credentials
EOF
)"

# 5. Watch the publish workflow, then confirm PyPI has the new version
gh run watch "$(gh run list --workflow=publish_to_pypi.yml -L1 --json databaseId -q '.[0].databaseId')" --repo SchmidtDSE/api_dock
curl -s https://pypi.org/pypi/api-dock/json | python3 -c "import sys,json; print('PyPI latest:', json.load(sys.stdin)['info']['version'])"

# 6. conda-forge: once the bot opens the v$VERSION PR (usually within hours), check that the recipe's
#    run requirements match pyproject.toml dependencies (the bot only bumps version + sha256), then merge it
gh pr list --repo conda-forge/api_dock-feedstock --state open
```

---

# License

BSD 3-Clause

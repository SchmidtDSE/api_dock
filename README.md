# API Dock

API Dock allows users to quickly build API-s that proxy requests to multiple remote APIs and Databases through a single endpoint. Configuration is handled with simple YAML files. Using API Dock's CLI, an API can easily be launched as a FastAPI or Flask app, or integrated into any existing python based API.

## Table of Contents

- [Install](#install)
- [File Structure](#file-structure)
- [Simple Example](#simple-example)
- [Configuration Syntax](#configuration-syntax)
  - [Main Configuration](#main-configuration)
  - [Remote Configurations](#remote-configurations)
  - [SQL Database Support](#sql-database-support)
  - [URL Query Parameters](#url-query-parameters)
- [CLI](#cli)
  - [Commands](#commands)
  - [Examples](#examples)
- [Cookies and Authentication](#cookies-and-authentication)
- [Using RouteMapper in Your Own Projects](#using-routemapper-in-your-own-projects)
  - [Basic Integration](#basic-integration)
  - [Framework Examples](#framework-examples)
  - [Database Integration](#database-integration)
- [Advanced Configuration Examples](#advanced-configuration-examples)
  - [Route Restrictions](#route-restrictions)
  - [Custom Route Mapping](#custom-route-mapping)
  - [Query Parameter Filtering](#query-parameter-filtering)
  - [Sorting and Pagination](#sorting-and-pagination)
  - [Authentication Setup](#authentication-setup)
  - [Cookie Access](#cookie-access)
- [Requirements](#requirements)
- [License](#license)

## Install

**FROM PYPI**

```bash
pip install api_dock
```

**FROM CONDA**

```bash
 conda install -c conda-forge api_dock
```

---

## File Structure

The main configuration files are stored in the top level of the CWD's `api_dock_config/` directory. Multiple configurations, with both versioned and unversioned remote-apis and databases are possible.

Here is an example:

```bash
api_dock_config
├── config.yaml               # The default main-config file
├── databases
│    ├── config.yaml          # (optional) shared tables/schemas for all databases
│    ├── unversioned_db.yaml  # Database config without versioning
│    └── versioned_db         # Folder containing database configs for different versions
│        ├── 0.1.yaml
│        ├── 0.5.yaml
│        └── 1.1.yaml
└── remotes
    ├── service1.yaml         # Remote-Api config without versioning
    ├── service2.yaml         # Remote-Api config without versioning
    └── versioned_service     # Folder containing remote-api configs for different versions
        ├── 0.1.yaml
        ├── 0.2.yaml
        └── 0.3.yaml
```

 By default api-dock expects there to be one called `config.yaml`, however configs with different names (such as `config_v2`) can be added and launched as shown in the CLI Examples section.

---

## Simple Example

Configuration consists of a global config (`api_dock_config/config.yaml`), as well as a config file for each remote-api or database you'd like to proxy. 

Here is a simple example of a configuration serving a single remote-api and database:

```bash
api_dock_config
├── config.yaml
├── databases
│    └── db_example
│        ├── 0.1.yaml
│        └── 0.5.yaml
└── remotes
    └── service1.yaml
```

```yaml 
# api_dock_config/config.yaml
name: "My API Dock"
description: "API proxy for multiple services"
authors: ["Your Name"]

# Remote APIs to proxy
remotes:
  - "service1"

# SQL databases to query
databases:
  - "db_example"
```

```yaml
# api_dock_config/remotes/service1.yaml
name: service1
description: "API Service1"
url: https://remote.api.com
```

```yaml
# api_dock_config/databases/db_example/0.5.yaml
name: db_example
description: "Example DB Version 0.5"
authors:
  - "API Team"

tables:
  users:
    uri: s3://path/to/users-database/partitioned-db_example/**/*.parquet
    region: us-west-2

routes:
  - route: /users
    sql: SELECT [[users]].* FROM [[users]]

  - route: /users/{{user_id}}
    sql: SELECT [[users]].* FROM [[users]] WHERE [[users]].user_id = {{user_id}}
```

Note: the use of wildcards `**/*` for the partioned parquet file. If there was no partitioning you would give the direct uri `s3://path/to/users-database/db_example.parquet`. 

This will create an "api-dock" with the following endpoints. 

```
- `/service1/*`: maps directly onto `https://remote.api.com/*`
- `/db_example/0.5/users`: queries all users in the "users-database"
- `/db_example/0.5/users/{user_id}`: queries all users in the "users-database" with `user.user_id = user_id`
```

Note: the filename is being used for versioning. An endpoint with "latest" is also generated that will numerically order versions by name and serve the most recent version. In this example, the route
`/db_example/0.1` uses the config in `/db_example/0.1.yaml`,  while both `/db_example/0.5` and `/db_example/latest` will use `/db_example/0.5.yaml`, the most recent version in the `/db_example` folder.

These basic configurations can be expanded to include a number of use cases: [restricting routes/methods](#route-restrictions), [custom mapping of remote-api routes](#custom-route-mapping), [accepting query parameters to filter data](#query-parameter-filtering), [limiting and sorting results](#sorting-and-pagination), [authentication](#authentication-setup), and [accessing data stored in cookies](#cookie-access).

---

# Configuration Syntax

## Main Configuration

The main configuration file tells api-dock which remote-api and database configuration files to connect to.  Additionally there are adds meta-data (returned by the base-api-route) and optional-settings:

```yaml
# api_dock_config/config.yaml

# meta-data: this is returned as the base api-route by default
name: # API Name
description: # Description of API
authors: # list of Authors

# sql-databases to query
databases:
  - "unversioned_db"     # adds database configuration in  "api_dock_config/databases/unversioned_db.yaml"
  - "versioned_db"       # adds database configurations in  "api_dock_config/databases/versioned_db/"

# remote APIs to proxy
remotes:
  - "service1"           # add configuration in "api_dock_config/remotes/service1.yaml"
  - "service2"           # add configuration in "api_dock_config/remotes/service2.yaml"
  - "versioned_service"  # add configurations in versions in "api_dock_config/remotes/versioned_service/"

# Optional HTTP behavior settings
settings:
  add_trailing_slash: true              # Auto-add trailing slash to paths (default: true)
  follow_protocol_downgrades: false     # Allow HTTPS->HTTP redirects (default: false)
  follow_redirects: true                # Follow remote redirects (default: true)
  timeout: 10                           # Upstream request timeout in seconds (default: 10)
  base_path: /dock                      # Also serve the API under this prefix (default: none)
  duckdb:                               # Options for database queries (default: none)
    memory_limit: 700MB
    threads: 2
    max_concurrent_queries: 2
```

### Settings

The optional `settings` section controls HTTP and query behavior:

- **`add_trailing_slash`** (default: `true`): Automatically append a trailing slash to all proxied paths. This prevents 307/301 redirects from remote APIs that require trailing slashes (e.g., `/projects` → `/projects/`). Set to `false` to disable this behavior.

- **`follow_protocol_downgrades`** (default: `false`): Control how HTTP redirects are handled. When `false` (recommended), HTTPS→HTTP redirects are blocked for security. When `true`, allows following redirects that downgrade from HTTPS to HTTP (not recommended for production).

- **`follow_redirects`** (default: `true`): Whether redirects from a remote are followed by API Dock (`true`) or passed through to the client with their `Location` header (`false`). Set it to `false` when a remote answers with redirects the client should follow itself, such as presigned S3 URLs for large files.

- **`timeout`** (default: `10`): Upstream request timeout in seconds, applied to both the streaming and buffered proxy paths. Raise it for slow upstreams (e.g. large aggregation queries) that would otherwise return a 502 on timeout. Set to `null` or `false` to disable the timeout entirely (not recommended — a stalled upstream can hold the connection open indefinitely).

- **`base_path`** (default: none): An extra URL prefix the API is also served under, e.g. `/dock`. Use it when a proxy or CDN forwards a path on another domain without stripping it (say CloudFront routes `https://app.example.org/dock/*` to API Dock): `/dock/birdnet/latest/detections/` is then handled as `/birdnet/latest/detections/`. Paths without the prefix keep working, so direct calls and health checks are unaffected.

- **`duckdb`** (default: none): Options for the DuckDB connection each database query runs on. Every key except `max_concurrent_queries` is applied as `SET <key> = <value>`, so any [DuckDB setting](https://duckdb.org/docs/configuration/overview) works; the useful ones on small servers are `memory_limit` (DuckDB spills to disk or fails the query instead of exceeding it), `threads`, and `temp_directory`. `max_concurrent_queries` caps how many database queries run at once in the process; further queries wait their turn. Memory limits apply per query, so on a small instance set `memory_limit × max_concurrent_queries` below the instance's memory.

Database queries run in worker threads, so a slow query doesn't hold up other requests (including health checks on `/`).

### Catalog Endpoints (`expose`)

The optional `expose` section adds read-only endpoints that list the models and versions of your configured databases, remotes, or both ("sources"). Listings are **opt-in** — with no `expose` key nothing is added.

```yaml
# Enable all three defaults: /databases, /remotes, /sources
expose: true
```

```yaml
GET /databases
# [{"model": "birdnet", "version": "2.4"},
#  {"model": "birdnet", "version": "3.0"},
#  {"model": "owl", "version": "0.5"}]

GET /sources        # databases + remotes, combined
# [{"model": "birdnet", "version": "2.4"}, ..., {"model": "core", "version": "0.5.0"}]
```

Versions are the config filename stems, so semver like `0.5.0` is preserved; unversioned sources report `version: null`. Enable only what you want, and control the output shape with `dict`:

```yaml
expose:
  dict: false          # return "model/version" strings instead of {model, version} dicts
  databases: true      # add /databases
  remotes: true        # add /remotes
  # sources omitted → not added

# GET /databases -> ["birdnet/2.4", "birdnet/3.0", "owl/0.5"]
```

Each of `databases` / `remotes` / `sources` accepts several forms:

```yaml
expose:
  databases: false                     # do not add the endpoint

  remotes: "list/remotes"              # custom route path (same as true, but at /list/remotes)

  databases:                           # explicit list of models (optionally version-filtered)
    - birdnet
    - owl:
        versions: [4.0, 5.0]           # ints/floats/strings all match the "4.0"/"5.0" stems

  sources:                             # most explicit form
    route: "list/sources"
    include: [birdnet, core]           # true (default) | false | list of models
    dict: true                         # per-endpoint override of the top-level `dict`
```

Each listing route is served both with and without a trailing slash (e.g. `/sources` and `/sources/` both work), so it doesn't matter which convention your clients use. Custom multi-segment routes (e.g. `list/databases`) take precedence over the `/{remote}/{path}` proxy. If a listing route would shadow a configured remote/database, or an `include` names something that doesn't exist, API Dock emits a startup warning. The exposed routes are also reflected in the root (`/`) metadata's `endpoints`.

---

## Remote Configurations

Remote Configurations allow you to proxy existing apis.  In the simple example above, 

```yaml
# api_dock_config/remotes/service1.yaml
name: service1
description: "API Service1"
url: https://remote.api.com
```

`name` defines the slug and `url` points to the existing api. So any route on `https://remote.api.com/*` may also be reached by at `service1/*`. However, the configuration file offers much more control over what endpoints may or may not be served through the api-proxy.  In particular, specific endpoints may be added or blocked, methods such as `DELETE` may be blocked, and routes with different signatures may be parsed. The structure is as follows:

```yaml 
# api_dock_config/remotes/service1.yaml
name:        # <str> this is the slug that goes in the url
url:         # <str> the base-url of the api being proxied
description: # (optional) <str> included in response for /service1 base route

routes:      # (optional) <list[str, dict]> constrain available-routes or map between route-signatures

restricted:  # (optional) <list[str, dict]> block specific endpoints or methods
```

### Variable Placeholders

Routes use double curly braces `{{}}` for variable placeholders:

- `users` - Matches exactly "users"
- `users/{{user_id}}` - Matches "users/123", "users/abc", etc.
- `users/{{user_id}}/profile` - Matches "users/123/profile"
- `{{}}` - Anonymous variable (matches any single path segment)

### Routes

#### String Routes (Simple GET Routes)

```yaml
routes:
  - users                          # GET /users
  - users/{{user_id}}              # GET /users/123
  - users/{{user_id}}/profile      # GET /users/123/profile
  - posts/{{post_id}}              # GET /posts/456
```

#### Dictionary Routes (Custom Methods and Mappings)

```yaml
routes:
  # A simple GET (note this is the same as passing the string 'users/{{user_id}}')
  - route: users/{{user_id}}
    method: get  

  # Different HTTP method
  - route: users/{{user_id}}
    method: post                   # POST /users/123

  # Custom remote mapping
  - route: users/{{user_id}}/permissions
    remote_route: user-permissions/{{user_id}}
    method: get                    # Maps local route to different remote endpoint

  # Complex mapping with multiple variables
  - route: search/{{category}}/{{term}}/after/{{date}}
    remote_route: api/v2/search/{{term}}/in/{{category}}?after={{date}}
    method: get
```

### Route Restrictions

You can restrict access to specific routes using the `restricted` section. Restrictions support wildcards and method-specific filtering:

#### String Routes Restrictions

```yaml
# Simple route restrictions (string format)
restricted:
  - admin/{{}}                       # Block all admin routes (single segment wildcard)
  - users/{{user_id}}/private        # Block private user data
  - system/*                         # Block all routes starting with system/ (prefix wildcard)
```

#### Dictionary Routes Restrictions
```yaml
# Method-aware restrictions (dict format)
restricted:
  - route: "*"
    method: delete                   # Block all DELETE requests
  - route: "stuff/*"
    method: delete                   # Block DELETE to any route starting with stuff/
  - route: "users/{{user_id}}"
    method: patch                    # Block PATCH requests to user routes
```


---

## SQL Database Support

Adding databases is similar to adding remote-apis, however now the `routes` section is required and maps directly to specifc `SQL` queries. Database routes support declarative URL query parameters via the `query_params` section. This lets you add filtering, sorting, pagination, conditional logic, and direct responses — all driven by the URL query string. Here's the structure


```yaml 
# api_dock_config/databases/versioned_db/0.1.yaml
name:        # <str> this is the slug that goes in the url
description: # (optional) <str> included in response for /versioned_db/0.1 base route
authors:     # (optional) <str,list> included in response for /versioned_db/0.1 base route

tables:      # <list[dict(name: uri)]> table definitions
queries:     # (optional) named-queries used for complex sql queries for readability 

routes:      # <list[dict]> maps routes to sql queries
```

For now only parquet support is working but we will be adding other Databases in the future.


### Database Configuration

Database configurations are stored in `api_dock_config/databases/` directory. Each database defines:
- **tables**: Mapping of table names to file paths (supports S3, GCS, HTTPS, local paths)
- **queries**: Named SQL queries for reuse
- **routes**: REST endpoints mapped to SQL queries
- **schema** (optional): the shared schema (from `databases/config.yaml`) this config's `[[table]]` references fall back to. See [Shared Tables and Schemas](#shared-tables-and-schemas-databasesconfigyaml)

### Startup checks

When API Dock starts, it checks every database and version listed in the main config, from config files and from the shared `databases/config.yaml` (`slugs`), exactly as requests will see them: merged with the main config and with the shared `routes`/`query_params` that apply to that version (after `include`/`exclude`). If anything is wrong it refuses to start and names the database, version, route and template, for example:

```
ValueError: Database 'owl' version '4.0', route '/detections/{{id}}/overlaps': sql: Table 'all_modelz.detections' not found in database configuration
```

It checks that:
- the shared `databases/config.yaml` is valid (`slugs`, `schema_groups`, and the types of its sections)
- every route and query param has a valid shape
- no `{{variable}}` is inside a quoted string (other than a string that is exactly one variable, `'{{x}}'`), a quoted identifier, or a SQL comment, and no template ends inside a comment (see [How values reach the database](#how-values-reach-the-database))
- every `[[table]]`, `[[schema.table]]`, `[[*.table]]` and `[[group.table]]` reference resolves, unions are only used after `FROM`/`JOIN`, and `source_columns` is valid

Database and remote config files are read from the folder that holds the main config file.

### Syntax

As with the remote-apis, the routes to databases use double-curly-brackets {{}} to reference url variable placeholders.
Additionally for SQL there are double-square-brackets [[]]. These are used to reference other items in the database config, namely: table_names, named-queries.

#### Table References: `[[table_name]]`

Use double square brackets to reference tables defined in the `tables` section. If we have

```yaml
tables:
  users: s3://your-bucket/users.parquet
```

then `SELECT [[users]].* FROM [[users]]` automatically expands to:

```sql
SELECT users.* FROM 's3://your-bucket/users.parquet' AS users
```

#### Named Queries: `[[query_name]]`

Similarly, you can reference named queries from the `queries` section with [[]]. This is one way to keep the routes clean even with complicated sql queries.


```yaml
queries:
  get_user_permissions: |
    SELECT [[users]].user_id, [[users]].name, [[user_permissions]].permission_name, [[user_permissions]].granted_date
    FROM [[users]]
    JOIN [[user_permissions]] ON [[users]].user_id = [[user_permissions]].user_id
    WHERE [[users]].user_id = {{user_id}}

routes:
  - route: users/{{user_id}}/permissions
    sql: "[[get_user_permissions]]"
```


#### EXAMPLE

Here's a complete example

```yaml
name: db_example
description: Example database with Parquet files
authors:
  - API Team

# Table definitions - supports multiple storage backends
tables:
  users: s3://your-bucket/users.parquet                # S3
  permissions: gs://your-bucket/permissions.parquet    # Google Cloud Storage
  posts: https://store-files.com/bucket/posts.parquet  # HTTPS
  local_data: tables/local_data.parquet                # Local filesystem

# Named queries (optional)
queries:
  get_permissions: >
    SELECT [[users]].*, [[permissions]].permission_name
    FROM [[users]]
    JOIN [[permissions]] ON [[users]].ID = [[permissions]].ID
    WHERE [[users]].user_id = {{user_id}}

# REST route definitions
routes:
  - route: users
    sql: SELECT [[users]].* FROM [[users]]

  - route: users/{{user_id}}
    sql: SELECT [[users]].* FROM [[users]] WHERE [[users]].user_id = {{user_id}}

  - route: users/{{user_id}}/permissions
    sql: "[[get_permissions]]"
```

### Shared Tables and Schemas (`databases/config.yaml`)

When several databases or versions read the same tables, define them once in the optional `api_dock_config/databases/config.yaml`. Everything lives under a `database` key: `meta` holds default table metadata, `schema` holds named groups of tables, and every other key is a global table.

```yaml
# api_dock_config/databases/config.yaml
database:
  # global tables, available as [[table1]] in any database config
  table1:
    uri: s3://your-bucket/table1.parquet
  table3:
    uri: s3://your-other-bucket/table3.parquet
    region: us-west-1        # a table's own keys override `meta`
    public: false

  # defaults applied to every table (shared tables and the tables in each version config)
  meta:
    region: us-west-2
    public: true

  # schemas, available as [[birdnet_2p4.detections]] in any route
  schema:
    birdnet_2p4:
      detections:
        uri: s3://your-bucket/birdnet/2.4/detections.parquet
    birdnet_3p0:
      detections:
        uri: s3://your-bucket/birdnet/3.0/detections.parquet
        public: false
```

A version config can name the schema it uses with `schema:`. An unqualified `[[name]]` is then looked up in order, first match wins:

1. the version config's own `tables`
2. its `schema:` in the shared config
3. the shared config's global tables

```yaml
# api_dock_config/databases/birdnet/2.4.yaml
name: birdnet
schema: birdnet_2p4
tables:
  revisions: s3://your-bucket/birdnet/2.4/revisions.parquet   # local to this version

routes:
  # [[detections]] isn't in `tables`, so it comes from the birdnet_2p4 schema
  - route: recordings/{{recording_id}}/detections
    sql: SELECT [[detections]].* FROM [[detections]] WHERE [[detections]].recording_id = {{recording_id}}
```

Any route can reference any schema directly as `[[schema.table]]`, so a different database (e.g. `owl/5.0`) can query `[[birdnet_2p4.detections]]`. To keep a table private to one database/version, define it in that version's `tables` instead. Qualified references are exposed to DuckDB as real views, so you can also use the full name or your own alias in plain SQL:

```yaml
  - route: detections/
    sql: >
      SELECT detections.common_name, COUNT(revisions.id) AS revcount
      FROM [[birdnet_2p4.detections]]
      LEFT JOIN [[revisions]] ON revisions.observation_id = birdnet_2p4.detections.id
      GROUP BY birdnet_2p4.detections.common_name
```

expands to

```sql
SELECT detections.common_name, COUNT(revisions.id) AS revcount
FROM birdnet_2p4.detections
LEFT JOIN 's3://your-bucket/birdnet/2.4/revisions.parquet' AS revisions ON revisions.observation_id = birdnet_2p4.detections.id
GROUP BY birdnet_2p4.detections.common_name
```

Notes:
- After `FROM`/`JOIN`, `[[schema.table]]` becomes `schema.table` with no alias, so `FROM [[birdnet_2p4.detections]] o` works. Elsewhere it becomes the bare table name (`detections`), because DuckDB doesn't accept `schema.table.*`.
- Schema and table names used as `[[schema.table]]` must be plain identifiers (letters, digits, underscores).
- Storage credentials are set per table. Tables whose `region`/`public` differ from the rest get their own S3 secret scoped to their path, so one query can mix regions and public/private buckets.
- Views are created only for the `[[schema.table]]` tables a query actually references.

#### Querying across schemas (`[[*.table]]`, schema groups)

Union references read the same table from several schemas at once:

| Reference | Reads |
|---|---|
| `[[*.detections]]` | every shared schema that has a `detections` table, including the current one |
| `[[*!.detections]]` | the same, minus the current database/version's `schema:` |
| `[[group1.detections]]` | the schemas listed in `schema_groups.group1` |
| `[[group1!.detections]]` | that group, minus the current schema |

```yaml
# api_dock_config/databases/config.yaml
schema_groups:          # named lists of shared schemas
  birdnet_models:
    - birdnet_2p4
    - birdnet_bullfrog_2p4v0p5
```

- A union expands, after `FROM`/`JOIN` only, to a parenthesized `UNION ALL BY NAME` over the member schemas, so give it an alias: `FROM [[*.detections]] detections`. Columns missing from some members come back as `NULL`.
- `*` skips schemas without the table. A group whose member lacks the table, a group naming an unknown schema, and a group sharing a name with a schema are all errors. `!` applies only to `*` and groups; with no `schema:`, it removes nothing.
- Every member gets its own S3 credentials (see above), so a union can mix regions and public/private buckets.

**Source columns.** Union rows carry only the tables' real columns unless the route asks for more with `source_columns`. The available facts are `schema` (the member schema), and `name` and `version` (the database/version whose `schema:` is that schema, or `NULL` if none or several use it):

```yaml
source_columns: [schema, name, version]      # adds schema_name, name, version
source_columns: {schema: _schema, name: model}   # pick a subset and rename
```

A source column that clashes with a real column raises an error. To use one for filtering without returning it, use DuckDB's `EXCLUDE`: `SELECT detections.* EXCLUDE (schema_name) ...`.

**`{{self.*}}` placeholders.** `{{self.schema}}`, `{{self.name}}` and `{{self.version}}` are the current database/version's schema, name and version, as SQL literals (`NULL` when unknown).

Together they make an "overlaps" route that every database/version can share. It returns every detection overlapping the given one, across all schemas, except that detection itself; other overlapping rows in the same schema are kept:

```yaml
routes:
  - route: detections/{{id}}/overlaps
    source_columns: [schema, name, version]
    sql: |
      WITH src AS (
        SELECT recording_id, start_time, end_time FROM [[detections]] WHERE id = {{id}}
      )
      SELECT detections.*
      FROM [[*.detections]] detections
      JOIN src ON detections.recording_id = src.recording_id
              AND detections.start_time < src.end_time
              AND detections.end_time   > src.start_time
      WHERE NOT (detections.schema_name = {{self.schema}} AND detections.id = {{id}})
```

Aliasing the union as `detections` also lets shared filters such as `[[detections]].confidence >= {{confidence}}` apply to the overlapping rows.

#### Inline database configs (`slugs`)

Simple database/version configs (often just a description and a `schema`) can live in the shared file instead of in their own files. Config files keep working, and the two can be mixed, even for the same database:

```yaml
# api_dock_config/databases/config.yaml
slugs:
  - name: birdnet-bullfrog         # the database slug in the URL
    version: "2.5"                 # one version...
    description: American Bullfrog Classifier from Birdnet 2.4
    schema: birdnet_bullfrog_2p5v0p5
  - name: birdnet-apple
    authors: [API Team]            # ...or several; keys here are defaults for each version
    versions:
      - version: "1.0"
        description: Apple Classifier 1.0
        schema: birdnet_apple_1p0
      - version: "12.0"
        description: Apple Classifier 12.0
        schema: birdnet_apple_12p0
  - name: notes                    # no version/versions = an unversioned database
    tables:
      notes: s3://your-bucket/notes.parquet
```

- Each entry (or each `versions` item) takes the same keys as a database config file: `description`, `authors`, `schema`, `tables`, `routes`, `query_params`, and so on. Shared `routes`/`query_params` (below), including `include`/`exclude`, apply to them like any other database/version.
- Like file-based databases, a slug is only served if it's listed under `databases:` in the main `config.yaml`.
- A database's versions are the union of its version files and its `slugs` versions, so `latest`, the `/{database}` versions listing, and the `expose` catalog endpoints all see both. If a file and a slug define the same database/version, the file wins.
- Quote versions (`version: "2.10"`). Unquoted YAML numbers are floats, so `2.10` would become `"2.1"`.
- A malformed `slugs` section (missing `name`, both `version` and `versions`, a duplicate version, or a mix of versioned and unversioned entries for one name) returns a 500 "Shared database configuration error".

#### Shared routes and query params

The shared file can also define top-level `routes` and `query_params`. These are added to **every** database/version, which is handy when each model/version serves the same endpoints over its own `schema`:

```yaml
# api_dock_config/databases/config.yaml
database:
  ...

routes:
  - route: recordings/{{recording_id}}/detections/
    sql: SELECT [[detections]].* FROM [[detections]] WHERE [[detections]].recording_id = {{recording_id}}
  - route: detections/{{id}}
    sql: SELECT [[detections]].* FROM [[detections]] WHERE [[detections]].id = {{id}}
  - route: not_for_everyone/{{id}}
    sql: SELECT [[other]].* FROM [[other]] WHERE [[other]].id = {{id}}
    exclude:                       # don't add this route to these slug/versions
      - 'slug1/3.0'
      - slug: slug2
        version: 2.3
      - slug: slug3
        version: '*'               # '*' = every version

  # the same route defined twice: one for everything except birdnet/2.4, one only for it
  - route: detections/
    exclude: ['birdnet/2.4']
    sql: SELECT [[detections]].* FROM [[detections]]
  - route: detections/
    include: ['birdnet/2.4']       # ONLY add this route to these slug/versions
    sql: SELECT [[detections]].*, [[revisions]].id AS revision_id FROM [[detections]] LEFT JOIN [[revisions]] ON [[revisions]].observation_id = [[detections]].id

query_params:
  - confidence:
      sql: "[[detections]].confidence >= {{confidence}}"
  - start_time:
      sql: "[[detections]].start_time >= {{start_time}}"
      exclude: ['slug1/3.9']
  - limit:
      sql_append: LIMIT {{limit}}

# limit ALL shared routes / query params to these slug/versions
route_inclusions: ['birdnet', 'owl/5.0']
query_inclusions: []                 # empty or missing = no restriction

# opt slug/versions out of ALL shared routes / query params
route_exclusions: ['legacy_db']
query_exclusions:
  - slug: slug4
    version: 1.0
```

Rules:
- **The version config wins.** Its own routes come first and replace any shared route with the same shape. Shape means the same path segments; `{{param}}` names and leading/trailing slashes are ignored, so `detections/{{id}}` and `/detections/{{detection_id}}/` are the same route. Routes the version config adds on top are kept.
- Shared `query_params` behave like a version config's top-level `query_params`. They apply to every route, and a param with the same name in the version config (or on a route) overrides the shared one.
- `include` and `route_inclusions`/`query_inclusions` are the opposite of `exclude` and `route_exclusions`/`query_exclusions`. When given (non-empty), the route or query param is added **only** to the listed slug/versions. A shared item is added only if it passes both the top-level lists and its own `include`/`exclude`.
- The same route (by shape) or query param (by name) can appear more than once in the shared file. Each database/version gets the first one whose `include`/`exclude` select it, so complementary `include`/`exclude` lists give different databases different versions of an endpoint.
- `include`, `exclude` and the four top-level lists take a list of `'<slug>/<version>'` strings or `{slug: <slug>, version: <version>}` mappings. `'<slug>'`, `'<slug>/*'` or a missing/`'*'` version match every version, including unversioned databases. Versions compare numerically when possible (`2.3`, `"2.3"`), and `latest` is resolved before matching.

**For more details**, see the [SQL Database Support Wiki](https://github.com/SchmidtDSE/api_dock/wiki/SQL-Database-Support).

### PostgreSQL tables

Tables can also live in PostgreSQL. Define named connections in the shared `databases/config.yaml` and point tables at them with `table:` instead of `uri:` (install with `pip install 'api_dock[postgres]'`):

```yaml
database:
  connections:
    core:
      host: db.example.com
      dbname: soundhub
      user: api_dock_readonly            # use a SELECT-only login
      password: env:SOUNDHUB_DB_PASSWORD
      sslmode: verify-full
  schema:
    core_v1:
      recordings: {connection: core, table: public.recordings}
```

Each route runs **natively on PostgreSQL** when all its tables (union members included) are on one connection, and on **DuckDB** otherwise: DuckDB attaches the PostgreSQL connections read-only, so unions and joins can mix PostgreSQL tables with files and with other connections. An optional `engine: postgres | duckdb` on a route checks or forces the choice. Connections open one pool each when the FastAPI server starts (Flask is not supported with PostgreSQL). See the [PostgreSQL wiki page](https://github.com/SchmidtDSE/api_dock/wiki/PostgreSQL) for connection settings, SQL details, safety, errors and lifecycle.

---

## URL Query Parameters



### How values reach the database

api_dock does not paste request values into SQL. Each `{{variable}}` in `sql`, `multivalue_sql`, conditional `sql` and `queries:` becomes a placeholder, and its value (from the path, query string, a `default`, or a cookie) is sent to DuckDB separately. DuckDB converts the value to the column's type, so number, date and boolean filters work as written. A value that is not a valid number, date or boolean for its column, such as `?age=25 OR true`, is rejected with an error instead of being run as SQL.

Because the value is sent separately, **write variables without quotes**:

| Write | Not |
|---|---|
| `department = {{department}}` | `department = '{{department}}'` |
| `UPPER(name) = UPPER({{name}})` | `UPPER(name) = UPPER('{{name}}')` |
| `name ILIKE '%' \|\| {{name}} \|\| '%'` | `name ILIKE '%{{name}}%'` |

For compatibility with 0.8.x and earlier configs, a string that is exactly one variable (`'{{department}}'`) is read as `{{department}}`. Any other quoted variable, like `'%{{name}}%'`, would be read as literal text, so api_dock refuses to start and prints the route with the fixed form (rewrite it with `||` as in the last row above). A variable can't be used as a column name in double quotes (`"{{column}}"`) either. A variable inside a SQL comment (`-- {{x}}` or `/* {{x}} */`) would be sent with nothing in the query to use it, so api_dock refuses to start and asks you to remove it. A template also can't end inside a comment, because api_dock adds WHERE conditions and `sql_append` clauses after it on the same line: put the comment on its own line or use `/* */`.

`sql_append` works differently. Its values are column names, `ASC`/`DESC` or numbers, which a database can't accept as separate values, so they are written into the SQL text. Each one must contain only letters, digits, spaces and `_ . , ( ) -`, and must not contain `--`. The same applies to the `sql_append` of a [conditional SQL selection](#conditional-sql-selection) branch.

The `# SQL:` comments in the examples below show values in place so the queries are easier to read.

### Basic Filtering with `sql`

Use `sql` to add WHERE clause fragments. Each fragment is joined with `AND`. Optional by default — only included if the parameter is in the URL.

```yaml
routes:
  - route: users
    sql: SELECT * FROM [[users]]
    query_params:
      - age:
          sql: age = {{age}}            # optional — only if ?age= provided
      - department:
          sql: department = {{department}}
      - height:
          sql: height < {{height}}
          default: 200                  # always included (uses 200 if not in URL)
```

```bash
GET /db/users?age=25&department=engineering
# SQL: SELECT * FROM users WHERE age = 25 AND height < 200 AND department = 'engineering'

GET /db/users
# SQL: SELECT * FROM users WHERE height < 200
```

### Repeated Parameters with `multivalue_sql`

A query parameter key can appear more than once in the URL (e.g. `?recording_id=1&recording_id=4`). Add a `multivalue_sql` template alongside `sql` to handle this: when **more than one** value is passed for the key, `multivalue_sql` is used instead of `sql`, and `{{param}}` expands to a parenthesized list with one value per URL entry, suitable for an `IN` clause. Each value is sent to the database separately, like any other variable.

Behavior is unchanged when `multivalue_sql` is absent, and when only a single value is passed the normal `sql` template is used.

```yaml
routes:
  - route: detections
    sql: SELECT [[detections]].* FROM [[detections]]
    query_params:
      - recording_id:
          sql: "[[detections]].recording_id = {{recording_id}}"            # single value
          multivalue_sql: "[[detections]].recording_id IN {{recording_id}}"  # 2+ values
      - scientific_name:
          sql: "[[detections]].scientific_name = {{scientific_name}}"
```

```bash
GET /db/detections?recording_id=4&scientific_name=Gryllus%20fultoni
# SQL: SELECT detections.* FROM detections
#      WHERE detections.recording_id = '4' AND detections.scientific_name = 'Gryllus fultoni'

GET /db/detections?recording_id=4&recording_id=1&scientific_name=Gryllus%20fultoni
# SQL: SELECT detections.* FROM detections
#      WHERE detections.recording_id IN ('4', '1') AND detections.scientific_name = 'Gryllus fultoni'
```

### Sorting and Pagination with `sql_append`

Use `sql_append` to append clauses *after* the WHERE clause — for `ORDER BY`, `LIMIT`, `OFFSET`, etc. Fragments are appended in the order they appear in the YAML config, so **the YAML order must match valid SQL order** (ORDER BY before LIMIT before OFFSET).

`sql_append` templates can reference `{{variables}}` from other parameters, including **value-only parameters** — params that only have a `default` and exist solely to provide a variable for other templates.

```yaml
routes:
  - route: users
    sql: SELECT * FROM [[users]]
    query_params:
      # WHERE clause params
      - department:
          sql: department = {{department}}
      # Post-WHERE params
      - sort:
          sql_append: ORDER BY {{sort}} {{sort_direction}}
          default: created_date
      - sort_direction:
          default: DESC               # value-only param — feeds into sort's template
      - limit:
          sql_append: LIMIT {{limit}}
          default: 50
      - offset:
          sql_append: OFFSET {{offset}}  # optional — only if ?offset= provided
```

```bash
GET /db/users?department=engineering&sort=name&sort_direction=ASC&limit=10
# SQL: SELECT * FROM users WHERE department = 'engineering' ORDER BY name ASC LIMIT 10

GET /db/users
# SQL: SELECT * FROM users ORDER BY created_date DESC LIMIT 50

GET /db/users?limit=20&offset=40
# SQL: SELECT * FROM users ORDER BY created_date DESC LIMIT 20 OFFSET 40
```

### Required Parameters

Use `required: true` to return a `400` error if the parameter is missing. Optionally provide a custom error response with `missing_response`.

```yaml
query_params:
  - report_type:
      sql: report_type = {{report_type}}
      required: true
      missing_response:
          error: "report_type is required"
          valid_types: ["summary", "detailed"]
          http_status: 400
```

```bash
GET /db/reports
# Response (400): {"error": "report_type is required", "valid_types": [...], "http_status": 400}
```

### Direct Responses with `response`

Use `response` to return a fixed JSON or string response immediately when the parameter is present (no SQL is executed).

```yaml
query_params:
  - debug:
      response:
          message: Debug mode enabled
          info: "This endpoint queries the users table"
  - sleeping:
      response: "Wake up! This endpoint is disabled during sleep mode."
```

```bash
GET /db/users?debug=anything
# Response (200): {"message": "Debug mode enabled", "info": "This endpoint queries the users table"}

GET /db/users?sleeping=true
# Response (200): "Wake up! This endpoint is disabled during sleep mode."
```

### Conditional Logic with `conditional`

Use `conditional` to branch on the parameter's value. Each branch can lead to a `sql` fragment, a `response`, or an `action`.

```yaml
query_params:
  - enrolled:
      conditional:
          true:
              sql: enrolled = true       # adds to WHERE clause
          false:
              sql: enrolled = false
          pending:
              response:
                  message: "Pending users cannot be queried"
                  action: "Contact admin"
          default:
              response: "Unknown enrollment status"
```

```bash
GET /db/users?enrolled=true
# SQL: SELECT * FROM users WHERE enrolled = true

GET /db/users?enrolled=pending
# Response (200): {"message": "Pending users cannot be queried", "action": "Contact admin"}

GET /db/users?enrolled=xyz
# Response (200): "Unknown enrollment status"
```

### Complete Example

Combining all parameter types in a single route:

```yaml
name: my_database
tables:
  users: s3://bucket/users.parquet

routes:
  - route: users/search
    sql: SELECT * FROM [[users]]
    query_params:
      # WHERE clause filters
      - name:
          sql: name ILIKE '%' || {{name}} || '%'
      - age_min:
          sql: age >= {{age_min}}
      - age_max:
          sql: age <= {{age_max}}
      - department:
          sql: department = {{department}}
      # Sorting and pagination (sql_append)
      - sort:
          sql_append: ORDER BY {{sort}} {{sort_direction}}
          default: created_date
      - sort_direction:
          default: DESC
      - limit:
          sql_append: LIMIT {{limit}}
          default: 50
      - offset:
          sql_append: OFFSET {{offset}}
      # Direct response
      - sleeping:
          response: "Search is disabled during sleep mode."
```

```bash
# Full search with filters, sorting, and pagination
GET /my_database/users/search?name=john&age_min=21&age_max=65&sort=age&sort_direction=ASC&limit=20&offset=40
# SQL: SELECT * FROM users
#      WHERE name ILIKE '%john%' AND age >= 21 AND age <= 65
#      ORDER BY age ASC LIMIT 20 OFFSET 40

# Just defaults
GET /my_database/users/search
# SQL: SELECT * FROM users ORDER BY created_date DESC LIMIT 50

# Direct response, no SQL
GET /my_database/users/search?sleeping=true
# Response: "Search is disabled during sleep mode."
```

### Processing Order

Parameters are processed in this order (first match wins for early returns):

1. `response` parameters — return immediately if parameter present
2. `conditional` parameters — evaluate value, may return response or add SQL
3. `required` parameters — return 400 if missing
4. `sql` parameters — build WHERE clause fragments
5. `sql_append` parameters — append post-WHERE clauses (ORDER BY, LIMIT, etc.)
6. Execute final SQL query

---

## Conditional SQL Selection

Some routes need a *different* base query depending on the request — for example, `?count=true` should return a species histogram (`SELECT … COUNT(*) … GROUP BY …`) rather than rows. `sql_append` can't help (it only adds trailing clauses), and a second `route:` can't either (the path is identical). For this, a route's `sql` may be a **rule list** instead of a string: a first-match-wins decision tree that picks the base query from the presence and value of path, query, and cookie params.

Everything downstream is unchanged: the selected base composes with `query_params` WHERE-fragments and `sql_append` exactly as a plain `sql:` string does.

### The histogram example

```yaml
routes:
  - route: detections
    sql:
      # ?count=<truthy>  → species histogram
      - when: count
        then:
          sql: >
            SELECT [[detections]].common_name, [[detections]].scientific_name,
                   COUNT(*) AS count
            FROM [[detections]]
          sql_append: GROUP BY [[detections]].common_name, [[detections]].scientific_name
      # otherwise → detection rows
      - else: SELECT [[detections]].* FROM [[detections]]
    query_params:
      - recording:
          sql: "[[detections]].recording_id = {{recording}}"
          multivalue_sql: "[[detections]].recording_id IN {{recording}}"
      - limit:
          sql_append: LIMIT {{limit}}          # applies in BOTH modes
```

```bash
GET /db/detections?recording=1&count=true
# SELECT detections.common_name, detections.scientific_name, COUNT(*) AS count
#   FROM detections WHERE detections.recording_id = '1'
#   GROUP BY detections.common_name, detections.scientific_name

GET /db/detections?recording=1
# SELECT detections.* FROM detections WHERE detections.recording_id = '1'
```

Note the pipeline order: the selected branch's `sql_append` (the `GROUP BY`) is applied **before** route-level `sql_append` (the shared `LIMIT`), so SQL clause order stays valid.

### Rule forms

A `sql` list contains rules evaluated top to bottom; the **first match wins**. Each rule's payload (an *sql node*) is a SQL string, a leaf object `{sql, sql_append}`, or a nested rule list.

```yaml
sql:
  - when: count                       # fires when `count` is truthy (shorthand for equals: _truthy)
    then: <sql node>

  - when: mode
    equals: 'true'                    # fires only when mode == "true" (case-insensitive)
    then: <sql node>

  - when: format                      # value map: different SQL per value
    match:
      species: <sql node>             # ?format=species
      recording: <sql node>           # ?format=recording
      _truthy: <sql node>             # any other truthy value
      _default: <sql node>            # any present value not matched above

  - when: [count, recording]          # list: fires when ALL are truthy (AND)
    then: <sql node>

  - when: [count, something_else]     # list + positional case list
    match:
      - values: [_any, x]             # something_else == x, count anything
        then: <sql node>
      - values: [_truthy, _absent]    # count truthy AND something_else not passed
        then: <sql node>
      - default: <sql node>

  - else: <sql node>                  # default (a trailing bare string works too)
```

Nesting works because a payload can itself be a rule list:

```yaml
sql:
  - when: some_value
    match:
      '4':
        - when: count
          then: <sql for value 4 with count>
        - else: <sql for value 4>
      _default: <sql for other values>
  - else: <base sql>
```

### Value specs

| spec | matches when the param… |
|---|---|
| `'literal'` (`'4'`, `'true'`) | is present and equals it (case-insensitive) |
| `_truthy` / `_falsy` | present, and value is / isn't in `{"", "0", "false", "no", "off", "null", "none"}` |
| `_present` / `_absent` | exists / does not exist |
| `_any` | wildcard — present or absent (used for a position in a case list) |
| `_default` | catch-all for any *present* value (value maps only) |

In a single-param `match:` map, precedence is order-independent: exact literal > `_falsy`/`_truthy` > `_present`/`_absent` > `_default`. In a positional case list, cases match strictly top-to-bottom.

### No match → URL error

If no rule matches and there is no default (`else`, a trailing bare string, or a `_default`/`default` catch-all), the request returns a **400** with `{"error": "No matching query configuration for the given parameters", "http_status": 400}`. Customize it with a terminal `no_match` rule:

```yaml
sql:
  - when: recording
    then: SELECT [[detections]].* FROM [[detections]] WHERE recording_id = {{recording}}
  - no_match:
      error: "recording is required, or pass count=true for a histogram"
      http_status: 400
```

Cookies participate via the `cookies.<name>` key (e.g. `when: cookies.role`, `equals: admin`).

---

# CLI

## Commands

API Dock provides a modern Click-based CLI:

- **pixi run api-dock** (default): List all available configurations and commands
- **pixi run api-dock init [--force]**: Initialize `api_dock_config/` directory with default configs
- **pixi run api-dock start [config_name]**: Start API Dock server with optional config name
- **pixi run api-dock describe [config_name]**: Display formatted configuration with expanded SQL queries
- **pixi run api-dock encrypt <plaintext>**: Encrypt values using local/AWS KMS encryption
- **pixi run api-dock decrypt <ciphertext>**: Decrypt encrypted values (for testing/debugging)
- **pixi run api-dock generate-key**: Generate new Fernet encryption key for local encryption

**Note**: All commands shown use `pixi run` for the pixi environment. If not using pixi, drop the `pixi run` prefix (e.g., `api-dock start` instead of `pixi run api-dock start`).


## Examples

```bash
# Initialize local configuration directory
pixi run api-dock init

# List available configurations, and available commands
pixi run api-dock

# Start API server
# - default configuration (api_dock_config/config.yaml) with FastAPI
pixi run api-dock start
# - default configuration with Flask (backbone options: fastapi (default) or flask)
pixi run api-dock start --backbone flask
# - specify with host and/or port
pixi run api-dock start --host 0.0.0.0 --port 9000

# Alternative configurations (example: api_dock_config/config_v2.yaml)
pixi run api-dock start config_v2
pixi run api-dock describe config_v2

# Encryption commands
pixi run api-dock generate-key                                    # Generate new encryption key
pixi run api-dock encrypt "my-secret-token"                      # Encrypt using local key
pixi run api-dock encrypt --method aws_kms --key-id arn:aws:... "secret"  # Encrypt using AWS KMS
pixi run api-dock decrypt "gAAAAABh..."                          # Decrypt encrypted value
```

**For more details**, see the [Configuration Wiki](https://github.com/SchmidtDSE/api_dock/wiki/Configuration).

---





---

# Cookies and Authentication

API Dock supports cookie extraction and authentication for both remote APIs and database routes. Cookies can be passed through to remote APIs or used for authentication validation.

## Cookie Configuration

### Forwarding client cookies

Configure which cookies to extract from incoming requests and forward to the upstream API (or make available as template variables in SQL routes):

```yaml
# Forward all cookies from the client request
cookies: true

# Forward only specific cookies
cookies: [session_id, auth_token, user_preferences]

# Forward no cookies (default behavior)
cookies: false
```

When `cookies: true`, all cookies are accepted and available. When `cookies: false` (default), no cookies are processed except authentication cookies when authentication is configured. When providing a list, only the named cookies are forwarded.

Forwarded cookies are accessible in SQL queries using `{{cookies.cookie_name}}`:

```yaml
routes:
  - route: user/profile
    sql: SELECT * FROM [[users]] WHERE session_id = {{cookies.session_id}}
```

### Injecting cookies from the server environment

The `cookies` list also accepts dict entries to inject cookies into every outgoing upstream request, regardless of what the client sent. This is useful when the upstream API requires a server-side credential (e.g. a session token stored in an environment variable) rather than a cookie from the end user.

Dict entries and string entries can be mixed freely in the same list.

```yaml
cookies:
  - session_id                            # forward this cookie from the client request
  - key: __Secure-authjs.session-token    # inject from environment variable
    value: "env:SOUNDHUB_SESSION_TOKEN"
```

#### Dict entry forms

| Form | Behaviour |
|---|---|
| `{key: NAME, value: "literal"}` | Inject cookie `NAME` with the literal string `"literal"` |
| `{key: NAME, value: "env:MY_VAR"}` | Inject cookie `NAME` with the value of env var `MY_VAR`; empty string if unset |
| `{key: MY_VAR}` | Shorthand — equivalent to `{key: MY_VAR, value: "env:MY_VAR"}` |

The shorthand form uses the key as both the cookie name **and** the env var name. Use the explicit `value: "env:..."` form when the cookie name and the env var name differ (which is common — cookie names often contain characters that aren't valid env var names).

#### Example: proxying an API that requires a session cookie

```yaml
# api_dock_config/remotes/my_service/1.0.yaml
name: my_service
url: https://api.example.com

cookies:
  - key: __Secure-authjs.session-token
    value: "env:MY_SERVICE_SESSION_TOKEN"
```

Set the env var before starting api-dock:

```bash
export MY_SERVICE_SESSION_TOKEN="your-session-token-here"
pixi run api-dock start
```

Every request proxied to `my_service` will include the `__Secure-authjs.session-token` cookie, even if the client did not send one.

#### Injection precedence

If an injected cookie has the same name as a forwarded client cookie, the injected value takes precedence. This ensures the server-side credential is always used, regardless of what the client sends.

## Authentication Configuration

Configure authentication to validate requests before processing. Multiple authentication methods are supported:

### Fixed Value Authentication
```yaml
authentication:
  key: "auth_token"
  value: "secret123"
  encrypted: false
  failed_response:
    status: 401
    message: "Access denied"
```

### List of Valid Values
```yaml
authentication:
  key: "auth_token"
  values:
    - "Z0FBQUFBQnBx...c9PQ=="
    - "Z0FzxDeBFBnB...9OuT=="
    - "Z54dUeiIFZnk...cXnn=="
  encrypted: true
```

### File-Based Authentication
```yaml
authentication:
  key: "auth_token"
  filepath: "/path/to/tokens.txt"
  encrypted: true
```

### AWS Secrets Manager
```yaml
authentication:
  key: "auth_token"
  aws_secret_name: "api-dock/tokens"
  aws_region: "us-west-2"
  encrypted: false
```

### AWS KMS Encryption
```yaml
authentication:
  key: "auth_token"
  aws_key_id: "arn:aws:kms:us-west-2:123456789:key/12345678-1234-1234-1234-123456789012"
  aws_region: "us-west-2"
  encrypted: true
```

### GCP Secret Manager
```yaml
authentication:
  key: "auth_token"
  gcp_secret_name: "api-dock-tokens"
  gcp_project_id: "my-project"
  encrypted: false
```

## Authentication Options

- `encrypted: true/false` - Whether stored values are encrypted and need decryption

Note: Authentication extracts tokens from cookies and supports multiple backend sources including AWS Secrets Manager, AWS KMS encryption, GCP Secret Manager, and file-based authentication.

For detailed setup instructions and examples, see the complete authentication documentation.

---

# Using RouteMapper in Your Own Projects

The core functionality is available as a standalone `RouteMapper` class that can be integrated into any web framework:

## Basic Integration

```python
from api_dock import RouteMapper
import asyncio

# Initialize with optional config path
route_mapper = RouteMapper(config_path="path/to/config.yaml")

# Get API metadata
metadata = route_mapper.get_config_metadata()

# Check configuration values
success, value, error = route_mapper.get_config_value("some_key")

# Route requests (async version for FastAPI, etc.)
success, data, status, error = await route_mapper.map_route(
    remote_name="service1",
    path="users/123",
    method="GET",
    headers={"Authorization": "Bearer token"},
    query_params={"limit": "10"}
)

# Route requests (sync version for Flask, etc.)
success, data, status, error = route_mapper.map_route_sync(
    remote_name="service1",
    path="users/123",
    method="GET"
)
```

## Framework Examples

### Django Integration
```python
from django.http import JsonResponse
from api_dock.route_mapper import RouteMapper

route_mapper = RouteMapper()

def api_proxy(request, remote_name, path):
    success, data, status, error = route_mapper.map_route_sync(
        remote_name=remote_name,
        path=path,
        method=request.method,
        headers=dict(request.headers),
        body=request.body,
        query_params=dict(request.GET)
    )

    if not success:
        return JsonResponse({"error": error}, status=status)

    return JsonResponse(data, status=status)
```

### Custom Framework Integration
```python
from api_dock.route_mapper import RouteMapper

route_mapper = RouteMapper()

@your_framework.route("/{remote_name}/{path:path}")
def proxy_handler(remote_name, path, request):
    success, data, status, error = route_mapper.map_route_sync(
        remote_name=remote_name,
        path=path,
        method=request.method,
        headers=request.headers,
        body=request.body,
        query_params=request.query_params
    )

    return your_framework.Response(data, status=status)
```

## Database Integration

The `RouteMapper` also supports SQL database queries through the `map_database_route` method:

```python
from api_dock.route_mapper import RouteMapper
import asyncio

route_mapper = RouteMapper(config_path="path/to/config.yaml")

# Query database (async version)
async def query_database():
    success, data, status, error = await route_mapper.map_database_route(
        database_name="db_example",
        path="users/123",
        query_params={},
        cookies={}
    )

    if success:
        print(data)  # List of dictionaries from SQL query
    else:
        print(f"Error: {error}")

# Run async query
asyncio.run(query_database())
```

### Django Database Integration

```python
from django.http import JsonResponse
from api_dock.route_mapper import RouteMapper
import asyncio

route_mapper = RouteMapper()

def database_query(request, database_name, path):
    # Run async database query in sync context
    success, data, status, error = asyncio.run(
        route_mapper.map_database_route(
            database_name=database_name,
            path=path
        )
    )

    if not success:
        return JsonResponse({"error": error}, status=status)

    return JsonResponse(data, safe=False, status=status)
```

### Flask Database Integration

```python
from flask import Flask, jsonify
from api_dock.route_mapper import RouteMapper
import asyncio

app = Flask(__name__)
route_mapper = RouteMapper()

@app.route("/<database_name>/<path:path>")
def database_proxy(database_name, path):
    success, data, status, error = asyncio.run(
        route_mapper.map_database_route(
            database_name=database_name,
            path=path
        )
    )

    if not success:
        return jsonify({"error": error}), status

    return jsonify(data), status
```

---

# Advanced Configuration Examples

This section provides examples for advanced API Dock features mentioned in the [Simple Example](#simple-example).

## Route Restrictions

Restrict access to specific routes using the `restricted` section:

```yaml
# api_dock_config/remotes/secure_api.yaml
name: secure_api
url: https://internal-api.company.com

routes:
  - health
  - users/{{user_id}}
  - admin/{{}}

# Block access to admin routes
restricted:
  - admin/{{}}                       # Block all admin routes
  - route: "users/{{user_id}}"       # Block DELETE on user routes
    method: delete
```

## Custom Route Mapping

Map local routes to different remote endpoints:

```yaml
# api_dock_config/remotes/legacy_api.yaml
name: legacy_api
url: https://old-system.company.com

routes:
  # Map modern endpoint to legacy path
  - route: users/{{user_id}}/profile
    remote_route: legacy/user-info/{{user_id}}
    method: get

  # Complex mapping with query parameters
  - route: search/{{category}}/{{term}}
    remote_route: api/v1/search?category={{category}}&query={{term}}
    method: get
```

## Query Parameter Filtering

Add dynamic filtering to database routes:

```yaml
# api_dock_config/databases/analytics.yaml
name: analytics
tables:
  events: s3://analytics-bucket/events/**/*.parquet

routes:
  - route: events
    sql: SELECT * FROM [[events]]
    query_params:
      - date_from:
          sql: event_date >= {{date_from}}
      - event_type:
          sql: type = {{event_type}}
      - user_id:
          sql: user_id = {{user_id}}
          required: true
```

## Sorting and Pagination

Add sorting and pagination to database queries:

```yaml
# api_dock_config/databases/user_data.yaml
name: user_data
tables:
  users: s3://data-bucket/users.parquet

routes:
  - route: users
    sql: SELECT * FROM [[users]]
    query_params:
      - sort:
          sql_append: ORDER BY {{sort}} {{sort_direction}}
          default: created_date
      - sort_direction:
          default: DESC
      - limit:
          sql_append: LIMIT {{limit}}
          default: 50
      - offset:
          sql_append: OFFSET {{offset}}
```

## Authentication Setup

Protect database routes with token-based authentication:

```yaml
# api_dock_config/databases/secure_data.yaml
name: secure_data

# Authentication configuration
authentication:
  key: "api_token"
  values: ["secret123", "admin456", "readonly789"]
  encrypted: false
  failed_response:
    status: 403
    message: "Valid API token required"

tables:
  sensitive_data: s3://private-bucket/data.parquet

routes:
  - route: data
    sql: SELECT * FROM [[sensitive_data]]
```

Note: there are several (safer) options for authentication. See [Authentication Configuration](#authentication-configuration) for more details.


## Cookie Access

Extract and use cookie values in database queries:

```yaml
# api_dock_config/databases/user_session.yaml
name: user_session

# Enable specific cookie extraction
cookies: [user_id, session_token, preferences]

tables:
  user_activity: s3://analytics/activity.parquet

routes:
  - route: my-activity
    sql: SELECT * FROM [[user_activity]] WHERE user_id = {{cookies.user_id}}

  - route: user-settings
    sql: |
      SELECT * FROM [[user_activity]]
      WHERE user_id = {{cookies.user_id}}
      AND session_token = {{cookies.session_token}}
```

---

# Requirements

Requirements are managed through a [Pixi](https://pixi.sh/latest) "project" (similar to a conda environment). After pixi is installed use `pixi run <cmd>` to ensure the correct project is being used. For example,

```bash
# launch jupyter
pixi run jupyter lab .

# run a script
pixi run python scripts/hello_world.py
```

---

# Development

## Publishing a Release

Publishing a GitHub Release is what publishes to PyPI: `.github/workflows/publish_to_pypi.yml` runs on `release: published`, builds the sdist and wheel with `uv build`, and uploads them using PyPI trusted publishing (OIDC). There's no local build, no API token, and no `twine`. conda-forge follows automatically: its bot opens a version PR on [conda-forge/api_dock-feedstock](https://github.com/conda-forge/api_dock-feedstock), which a maintainer merges.

```bash
# 0. Start from a clean, up-to-date main
export VERSION=0.9.0          # the NEW version, no leading "v"
git checkout main
git pull origin main
git status

# 1. Set `version` in pyproject.toml to $VERSION

# 2. Run the tests
pixi run -e dev pytest -q

# 3. Commit, tag, push (the commit command adds the "v$VERSION: " prefix)
export COMMIT_MESSAGE='bound SQL parameters (fix SQL injection) and startup config checks'
git add -A
git commit -m "v$VERSION: $COMMIT_MESSAGE"
git tag "v$VERSION"
git push origin main "v$VERSION"

# 4. Publish the GitHub Release; this triggers the PyPI upload.
#    Don't use --draft (the workflow only runs on a published release); no wheel needs attaching.
gh release create "v$VERSION" \
  --title "v$VERSION" \
  --notes "$(cat <<'EOF'
* new features
    - Request values are sent to DuckDB as bound parameters, separately from the SQL, so they can never run as SQL. Write variables without quotes (`UPPER(name) = UPPER({{name}})`); a string that is exactly one variable (`'{{name}}'`) still works, and patterns like `'%{{name}}%'` become `'%' || {{name}} || '%'`
    - Startup checks: API Dock refuses to start, naming the database, version, route and template, if a config has a quoted or commented variable, a template ending in a comment, a malformed route, an invalid shared `databases/config.yaml`, a `[[table]]` / `[[schema.table]]` / union reference that doesn't resolve, or invalid `source_columns`. Each version is checked as requests see it (shared routes/query_params, include/exclude, slugs)
    - Database and remote configs are read from the folder that holds the main config file
* bug fixes
    - SQL injection: query/path/cookie values were pasted into SQL and only quoted when the SQL fragment happened to contain words like SELECT/WHERE/AND/OR, so filters like `confidence >= {{confidence}}` accepted raw SQL (`?confidence=0 OR 1=1`, `UNION SELECT ... read_csv(...)`)
    - `response:` bodies are filled in as plain text (they no longer get SQL quotes)
    - Importing api_dock (or running `api-dock --help`) no longer builds an app from the current directory's config; the default `app`/`fastapi_app`/`flask_app` are built on first use
* cleanup / other improvements
    - Removed unused `build_sql_query_legacy` / `_substitute_parameters`; `build_sql_query` returns `(sql, values)` and `build_sql_query_with_tables` returns `(sql, values, tables)`
    - New `sql_template_check` module and `check_table_references`; README sections "How values reach the database" and "Startup checks"
    - Test suite grew from 253 to 373 tests
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

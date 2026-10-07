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
  - [PostgreSQL Databases](#postgresql-databases)
  - [Route Chaining](#route-chaining)
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

For [PostgreSQL databases](#postgresql-databases), install the `postgres` extra:

```bash
pip install 'api_dock[postgres]'
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
  timeout: 10                           # Upstream request timeout in seconds (default: 10)
```

### HTTP behavior Settings

The optional `settings` section controls HTTP behavior:

- **`add_trailing_slash`** (default: `true`): Automatically append a trailing slash to all proxied paths. This prevents 307/301 redirects from remote APIs that require trailing slashes (e.g., `/projects` → `/projects/`). Set to `false` to disable this behavior.

- **`follow_protocol_downgrades`** (default: `false`): Control how HTTP redirects are handled. When `false` (recommended), HTTPS→HTTP redirects are blocked for security. When `true`, allows following redirects that downgrade from HTTPS to HTTP (not recommended for production).

- **`timeout`** (default: `10`): Upstream request timeout in seconds, applied to both the streaming and buffered proxy paths. Raise it for slow upstreams (e.g. large aggregation queries) that would otherwise return a 502 on timeout. Set to `null` or `false` to disable the timeout entirely (not recommended — a stalled upstream can hold the connection open indefinitely).

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

Tables are read with DuckDB (Parquet and other files) unless the config sets `backend: postgres`; see [PostgreSQL Databases](#postgresql-databases).


### Database Configuration

Database configurations are stored in `api_dock_config/databases/` directory. Each database defines:
- **tables**: Mapping of table names to file paths (supports S3, GCS, HTTPS, local paths)
- **queries**: Named SQL queries for reuse
- **routes**: REST endpoints mapped to SQL queries

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

**For more details**, see the [SQL Database Support Wiki](https://github.com/SchmidtDSE/api_dock/wiki/SQL-Database-Support).

---

## URL Query Parameters


### How values reach the database

api_dock does not paste request values into SQL. Each `{{variable}}` in `sql`, `multivalue_sql`, conditional `sql` and `queries:` becomes a placeholder, and its value (from the path, query string, a `default`, or a cookie) is sent to the database separately. The database converts the value to the column's type, so number, date and boolean filters work as written. A value that is not a valid number, date or boolean for its column, such as `?age=25 OR true`, is rejected with an error instead of being run as SQL.

Because the value is sent separately, **do not put quotes around variables**:

| Write | Not |
|---|---|
| `department = {{department}}` | `department = '{{department}}'` |
| `UPPER(name) = UPPER({{name}})` | `UPPER(name) = UPPER('{{name}}')` |
| `name ILIKE '%' \|\| {{name}} \|\| '%'` | `name ILIKE '%{{name}}%'` |

A quoted variable would be read as literal text, so api_dock refuses to start and prints the route with the fixed form. A variable can't be used as a column name in double quotes (`"{{column}}"`) either. A variable inside a SQL comment (`-- {{x}}` or `/* {{x}} */`) would be sent with nothing in the query to use it, so api_dock refuses to start and asks you to remove it. A template also can't end inside a comment, because api_dock adds WHERE conditions and `sql_append` clauses after it on the same line: put the comment on its own line or use `/* */`.

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

## PostgreSQL Databases

A database config is served by DuckDB unless it sets `backend: postgres`. A PostgreSQL database's routes, `query_params`, `[[table]]` references and bound values work as described above, but its SQL is PostgreSQL's SQL, not DuckDB's. Install the driver with:

```bash
pip install 'api_dock[postgres]'
```

This installs `psycopg[binary]` and `psycopg_pool`. The binary package includes the PostgreSQL client library, so no separate libpq or PostgreSQL install is needed.

```yaml
# api_dock_config/databases/inventory.yaml
name: inventory
backend: postgres
connection:
  host: db.example.com
  dbname: inventory
  user: api_dock_readonly
  password: env:INVENTORY_DB_PASSWORD
  sslmode: verify-full
  sslrootcert: /etc/ssl/certs/provider-ca-bundle.pem   # the database provider's CA bundle
tables:
  products: catalog.products
routes:
  - route: products/{{id}}
    sql: SELECT [[products]].* FROM [[products]] WHERE [[products]].id = {{id}}
  - route: search
    sql: SELECT [[products]].name FROM [[products]]
    query_params:
      - q:
          sql: "[[products]].name ILIKE '%' || {{q}} || '%'"
```

### Connection

`connection:` holds [libpq connection fields](https://www.postgresql.org/docs/current/libpq-connect.html#LIBPQ-PARAMKEYWORDS) such as `host`, `port`, `dbname`, `user`, `password`, `sslmode` and `sslrootcert`. Other keys, including psycopg settings such as `autocommit`, stop api_dock from starting. `options`, `default_transaction_read_only` and `statement_timeout` can't be set, because api_dock sets them (see [Read-only queries](#read-only-queries)).

A value written `env:NAME` is read from the environment variable `NAME` when the server starts. If the variable is not set, api_dock does not start, and the error names the variable.

Invalid `sslmode` or `port` values in `connection:` also stop startup, including values read through `env:`. Ports must be integers from 1 to 65535; comma-separated ports and empty entries for the default port are supported.

For a database reached over a network, use `sslmode: verify-full` with `sslrootcert` set to your provider's CA bundle. libpq's default, `prefer`, falls back to an unencrypted connection if the server doesn't offer TLS, and `require` encrypts without checking the server's certificate or host name. A local test database may use `sslmode: disable`.

### Resource limits

These optional settings have the following defaults:

```yaml
connection:
  connect_timeout: 5       # seconds for each connection attempt
pool:
  min_size: 1              # connections kept open
  max_size: 4              # most connections open at once
  timeout: 5               # seconds a request waits for a free connection
  max_waiting: 16          # most requests waiting for a connection at once
  startup_timeout: 3       # seconds the startup check waits for a connection
statement_timeout_ms: 10000  # milliseconds a query may run
```

Sizes, `connect_timeout` and `statement_timeout_ms` must be positive integers; `timeout` and `startup_timeout` must be positive numbers. The limits apply to separate steps (connecting, waiting for a connection, running the query), so a request can take longer than any one of them.

Each database version has its own connection pool in each server process, so the most connections api_dock can open is the sum of `max_size` over all PostgreSQL databases and versions, times the number of server processes, times the number of running instances. Keep this below the database's connection limit.

### Read-only queries

Every connection is opened with read-only transactions, the `statement_timeout_ms` limit and the time zone set to UTC. Each query runs in its own transaction, which is rolled back after the rows are read, so a query that changes a setting doesn't affect the next one. Queries are sent as prepared statements, which PostgreSQL limits to one statement, so SQL such as `COMMIT; BEGIN READ WRITE; DELETE ...` fails. A route whose SQL writes, such as `DELETE FROM ...`, returns `500 Database query error` and changes nothing.

These settings guard against mistakes in a config's SQL. They don't stop a single statement with side effects, such as a call to a function that writes. **Connect with a login that can only SELECT from the tables it serves.**

### Tables and SQL

`tables:` values are `schema.table` or `table` names; the `{uri: ...}` form is for DuckDB only. Each part, and each table key, must be lower case and contain only letters, digits and `_`, at most 63 bytes. `[[table]]` is written as quoted names, such as `"catalog"."products" AS "products"` after FROM or JOIN and `"products"` elsewhere, so names that are SQL keywords work. A name without a schema is looked up in the login's search path, so schema-qualified names are recommended.

Write `%` as usual: api_dock doubles it for the driver. `E'...'` strings are recognized when checking for quoted variables.

Values are sent as text and PostgreSQL converts them to the column's type, as DuckDB does. A value that can't be converted, such as `?id=abc` for an integer column, returns `500 Database query error`.

### Responses

Rows are returned as JSON in the same shape as DuckDB rows. For both backends, dates and times become ISO 8601 strings, decimals become numbers (which may lose precision), bytes become base64 text, UUIDs and network addresses become strings, intervals become a number of seconds, and arrays become JSON arrays. PostgreSQL JSON and JSONB values are returned as JSON, not as text. PostgreSQL times with a time zone are returned in UTC; times without one are returned as stored.

### Availability and errors

If api_dock can't reach a PostgreSQL database when the server starts, it logs a warning and starts anyway. The warning looks like this: `Database 'inventory': No connection obtained within 3s; PostgreSQL routes will return 503 until a connection is available`.

Until a connection succeeds, that database's routes return `503 Database unavailable`. Other routes work as usual. The pool keeps trying to connect in the background, so the routes recover without a restart.

The warning doesn't say why the connection failed. To find the reason, read psycopg's log messages for the pool. The pool has the database's name, and the messages don't include the password. If the password is wrong, the routes return 503 until you correct the password and restart the server.

Once running, a request returns 503 when no connection becomes free within `pool.timeout`, when `pool.max_waiting` requests are already waiting, or when the connection is lost. Queries are not retried. Invalid SQL, a value of the wrong type, a permission error and a query over `statement_timeout_ms` return `500 Database query error`. Error responses don't include the database's error message.

A missing `connection:`, an unknown `backend:`, an invalid table name or resource limit, or missing PostgreSQL packages stop api_dock from starting, with the database and the reason in the error.

### Configuration changes need a restart

A database with any PostgreSQL version is read once, when the server starts: its versions, their configs, `/latest`, the `/databases` and `/sources` listings and its `env:` values stay as they were until the server restarts. To rotate a password, update the environment variable (for example, the secret your host loads into it), then restart or redeploy the server. Updating the secret store alone doesn't change the running server's password.

DuckDB-only databases are still read from their files on each request. If such a database's file is changed to `backend: postgres`, or a PostgreSQL version is added, its requests return `500 Database configuration changed; restart required` and listings leave that version out until the server restarts.

### Servers and lifecycle

PostgreSQL databases need FastAPI, the default server (`api-dock start`). The FastAPI app opens connection pools when the server starts and closes them when it stops. Flask still serves DuckDB and remote configs, but `--backbone flask`, or `create_flask_app()`, refuses a config with any PostgreSQL database version.

To use `RouteMapper` directly, start it, send requests and close it on one event loop:

```python
import asyncio

from api_dock.route_mapper import RouteMapper


async def main():
    mapper = RouteMapper(config_path="api_dock_config/config.yaml")
    try:
        await mapper.start()
        response = await mapper.map_database_route(
            database_name="inventory",
            path="products/7",
        )
        print(response.status_code, response.content)
    finally:
        await mapper.aclose()

asyncio.run(main())
```

Using a PostgreSQL route before `start()`, after `aclose()` or from another event loop raises `DatabaseLifecycleError`. Calling `asyncio.run()` for each request, as in the [Database Integration](#database-integration) examples, works only for DuckDB and remote configs.

A FastAPI app mounted inside another app doesn't run its own startup and shutdown, so the parent app must start and close its mapper:

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

from api_dock.fast_api import create_app

api = create_app("api_dock_config/config.yaml")


@asynccontextmanager
async def lifespan(app):
    await api.state.route_mapper.start()
    yield
    await api.state.route_mapper.aclose()

app = FastAPI(lifespan=lifespan)
app.mount("/api", api)
```

Importing api_dock doesn't build an app. Each of these app names builds its app the first time it is read: `api_dock.app`, `api_dock.fastapi_app`, `api_dock.fast_api.app`, `api_dock.flask_app` and `api_dock.flask_api.app`.

If a PostgreSQL database is configured, reading `flask_app` raises the Flask error. `from api_dock import *` raises it too, because it reads every exported name. Import only the names you need, for example `from api_dock.fast_api import create_app`.

## Route Chaining

A database route can get values from a route of another database before it runs. The other route is called a resolver. It must return exactly one row. The route can use the row's values in its SQL, in the URI of a table and in its response headers.

A typical use: a catalog database names the current release of a dataset, and a second database reads that release's Parquet files.

### Internal databases

The resolver's target is a database with `internal: true`:

```yaml
# api_dock_config/databases/catalog/1.0.yaml
name: catalog
backend: postgres            # DuckDB works too
internal: true
connection:
  host: catalog.example.internal
  dbname: catalog
  user: api_dock_readonly
  password: env:CATALOG_DB_PASSWORD
  sslmode: verify-full
tables:
  current_release: releases.current_release
routes:
  - route: releases/current
    sql: >
      SELECT release_id, data_uri, schema_version,
             revision || ':' || checksum AS validator
      FROM [[current_release]]
      WHERE project = {{project}} AND dataset = {{dataset}}
```

HTTP requests can't reach an internal database. Every request to `/catalog/...`, including `/catalog`, `/catalog/1.0` and `/catalog/latest/...`, returns `404` with the same body as for a name that does not exist. `RouteMapper.map_database_route()` also returns 404 for it. The root metadata and the `expose` listings leave it out.

`internal:` must be `true` or `false`; an omitted setting means `false`. All versions of a database must have the same setting. `internal:` is read from the database's own file, not from the main config.

### Resolvers

A database names its resolvers in `resolvers:`. A route lists the resolvers it uses in `resolve:`:

```yaml
# api_dock_config/databases/observations/2.0.yaml
name: observations
resolvers:
  current_release:
    via: catalog/1.0/releases/current
    params:
      project: "project:{{project_id}}"
      dataset: "{{dataset}}"
    bind: [release_id, data_uri, schema_version, validator]

tables:
  readings:
    uri: "{{current_release.data_uri}}"
    format: parquet
    files: "**/*.parquet"
    allow: ["s3://example-releases/"]
    region: us-west-2

routes:
  - route: projects/{{project_id}}/sites/{{site_id}}/readings
    resolve: [current_release]
    sql: >
      SELECT [[readings]].* FROM [[readings]]
      WHERE [[readings]].site_id = {{site_id}}
    headers:
      ETag: '"{{current_release.validator}}"'
      X-Release-Id: "{{current_release.release_id}}"
    query_params:
      - dataset: { default: hourly }
      - min_value:
          sql: "[[readings]].value >= {{min_value}}"
```

`GET /observations/2.0/projects/7/sites/42/readings` calls `catalog/1.0/releases/current` with `project=project:7` and `dataset=hourly`. It gets one row, reads the Parquet files under that row's `data_uri`, and returns the rows for site 42 with an `ETag` and an `X-Release-Id` header. `?dataset=daily` changes the value that the resolver sends.

A resolver has these keys:

- `via` (required): `<database>/<version>/<route path>`, or `<database>/<route path>` for a database without versions. The version must be explicit: `latest` is not allowed, so a new target version can't change this config without an edit. The path has no `{{variables}}`.
- `params` (optional): query params to send to the target. Each value is a text template that can use the route's path variables and query params, with their defaults. A template can't use cookies or the values of another resolver. If a template's variable has no value, that param is not sent.
- `bind` (required): the columns of the target's row that the route can use. To rename a column, use `AS` in the target's `SELECT`.

A resolver's name must be letters, digits and `_`, and can't be `cookies`. A route uses a value as `{{<resolver>.<column>}}`. In `sql:`, query-param `sql:` and `multivalue_sql:` fragments and selector branches, the value is bound like any other value. It can't be used in `sql_append`, because those values are written into the SQL text.

A resolver value always comes from the resolver. A path value, query value or default with the same dotted name can't replace it.

Resolvers run after authentication and after any early response (`response:`, `conditional:` or a missing `required` param), so a request that returns early makes no resolver call. Several resolvers run one after another, in `resolve:` order.

The call to the target is made by the server. It skips the target's authentication, whether the target sets it or inherits it from the main config. It sends none of the caller's cookies or query params, only `params:`. Cookies that the target injects with `{key: ..., value: "env:..."}` still apply; if the environment variable is not set, a query that uses the cookie fails.

Each request runs its resolvers again. There is no cache, so a new release is used at once.

### Values

api_dock turns each `bind:` value into text before it uses it:

- text stays as it is;
- integers and finite decimal and floating-point numbers are written as Python's `str()` writes them;
- booleans become `true` or `false`;
- dates and timestamps become ISO 8601;
- UUIDs become their text form;
- NULL stays NULL.

NaN, infinity and other types (lists, objects, bytes) give `500 Resolver error`.

In SQL, a NULL value is bound as NULL. Note that `x = NULL` matches no row in SQL; use `IS NULL` or `IS NOT DISTINCT FROM` to compare with a value that can be NULL. A NULL table URI gives `500 Resolver error`. A NULL value in a header leaves that header out.

### Templated tables

A DuckDB table whose `uri:` contains `{{` is templated. Its `uri:` must be exactly one `{{<resolver>.<column>}}`. It also needs:

- `format:`: `parquet`, `csv` or `json`. The format, not the file name, selects the reader: `read_parquet`, `read_csv` or `read_json`. `json` reads a JSON array and newline-delimited JSON. DuckDB detects reader options such as the CSV delimiter.
- `allow:`: a list of URI prefixes that the resolved URI must match.

It can also have `files:`, a pattern such as `"**/*.parquet"`, and the usual storage keys such as `region:`. It can't have `path:`. Only templated tables can have `format:`, `files:` and `allow:`. PostgreSQL databases can't have templated tables.

After `FROM` or `JOIN`, `[[readings]]` is written as `read_parquet(?) AS readings`, and the URI is a bound value. It is never written into the SQL text. Elsewhere `[[readings]]` is the alias, as for any table. A table can appear in separate subqueries. For a self-join, define two table names with the same `uri:` and use each one once.

Before the URI is used, api_dock checks it. It refuses a URI that:

- contains a `..` path segment, as written or after percent-decoding;
- contains `*`, `?` or `[`;
- matches no `allow:` prefix.

A URI matches a prefix if it equals the prefix, or starts with the prefix and the prefix ends with `/`, or starts with the prefix and the next character is `/`. So `s3://bucket/pub` matches `s3://bucket/pub/x`, but not `s3://bucket/pub-secret/x`.

After the checks, `files:` is added to the end of the URI. If the URI does not end with `/`, api_dock adds one first. Without `files:`, the URI is read as one file. A hive-partitioned folder, read with `files: "**/*.parquet"`, returns its partition columns.

Storage access (S3, GCS, Azure or HTTP) is set up from the `allow:` prefixes, so all prefixes of one table must use the same storage type.

`allow:` can hold local folders. DuckDB follows symbolic links, so make sure that no link in an allowed folder points outside it.

### Response headers

A database route can set response headers with `headers:`. Each value is a text template that can use the route's path variables and the values of the resolvers in its `resolve:`. It can't use query params or cookies. The headers are added to a successful response only.

api_dock doesn't add quotes or change the value. Write the quotes that `ETag` needs in the config: `ETag: '"{{current_release.validator}}"'`. api_dock doesn't handle `If-None-Match` and never returns `304`.

A header name must be a valid HTTP header name. A route can't set hop-by-hop headers, `Content-Type`, `Content-Length` or `Set-Cookie`. If a filled value has a control character (such as a line feed) or a non-ASCII character, the request returns `500 Resolver error` and no header is sent.

### Errors

The caller gets one of these responses. The body never includes a message from the target or the database driver. The server log names the database, the route, the resolver, the target and the reason.

| What happened | Response |
|---|---|
| The target returned no rows | `404 Not found` |
| The target returned more than one row | `500 Resolver error` |
| The target is unavailable (`503`) | `503 Database unavailable` |
| The target returned another error, for example `400` for a missing param | `500 Resolver error` |
| The row has no column that `bind:` lists | `500 Resolver error` |
| A value can't be converted, a table URI fails a check, or a header value can't be sent | `500 Resolver error` |

If a route lists two resolvers and the second one fails, the caller gets the second one's error.

### Startup checks

These problems stop api_dock from starting. The error names the database, the version, and the route, resolver or table:

- a `via` to a database, version or route that does not exist, to a remote, or to a database that is not internal;
- a `via` that uses `latest` or contains `{{`;
- a target that has `resolvers:` itself (resolvers can't form chains);
- a malformed resolver, an unknown resolver key, or a resolver named `cookies`;
- a `params:` template that uses a cookie or a resolver value, or a variable that is not a path variable or declared query param of every route that lists the resolver;
- a `{{variable}}` in the target route's SQL with no value: it must be a key of `params:`, a target query-param default or a target path variable in the `via` path. A `{{cookies.name}}` there must be a cookie that the target injects;
- a `resolve:` entry that is not defined or is listed twice;
- a route that uses `{{<resolver>.<column>}}`, directly or through a templated table, without listing the resolver in `resolve:`, or with a column that is not in `bind:`;
- a resolver value or a templated table in `sql_append`;
- a templated table without `format:` or `allow:`, with an invalid `files:` or `allow:` entry, with prefixes of more than one storage type, or on a PostgreSQL database;
- a header name that is invalid or reserved, or a header template that uses another name.

api_dock can't check at startup that the target's `SELECT` returns the columns in `bind:`. A missing column gives `500 Resolver error` at request time. A resolver that no route lists is logged as a warning.

### Configuration changes need a restart

If any version of a database uses `internal:`, `resolvers:`, route `resolve:` or `headers:`, or a templated table, all its versions are read once, when the server starts, as for PostgreSQL databases. This includes `internal: false`. File edits, deleted routes and new version files take effect only after a restart, when the startup checks run again. This also applies to public visibility: setting `internal:` in a file doesn't hide or show the database until the server restarts.

DuckDB databases without these settings are still read from their files on each request. If such a file starts to use one of these settings, or a table gets `format:`, `files:` or `allow:`, its requests return `500 Database configuration changed; restart required` until the server restarts.

Resolvers work on Flask wherever their target works. Flask refuses PostgreSQL configs, so on Flask a resolver's target must be an internal DuckDB database.

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
# - default configuration with Flask (backbone options: fastapi (default) or flask);
#   Flask does not serve PostgreSQL databases
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

The `RouteMapper` also supports SQL database queries through the `map_database_route` method. The examples below run each request with its own `asyncio.run()`, which works for DuckDB databases and remotes only; for PostgreSQL databases see [Servers and lifecycle](#servers-and-lifecycle).

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

```bash
# 0. Make sure you are on `main` and merged with any changes

# 1. Bump version in pyproject.toml

# 2. Commit everything
git add -A
git commit -m "v0.6.1: stream proxy responses (fix large-response 502 + content-encoding)"

# 3. Tag and push
git tag v0.6.1
git push origin main v0.6.1

# 4. Build the wheel (requires the `dev` pixi environment)
rm -rf dist/
find . -name "__pycache__" -type d -exec rm -rf {} +
find . -name "*.pyc" -delete
pixi run -e dev python -m build --wheel
ls dist/*.whl

# 5. Create GitHub release with the wheel attached
gh release create v0.6.1 dist/api_dock-0.6.1-py3-none-any.whl \
    --title "v0.6.1" --notes "$(cat <<'EOF'
* new features
    - Remote proxy responses are now streamed (FastAPI) — upstream bytes are piped to the client as they arrive instead of being buffered fully in memory
    - New `timeout` setting (default 10s) for the upstream request; set to `null`/`false` to disable
* bug fixes
    - Large upstream responses no longer return 502 — streamed via `StreamingResponse` instead of reading the whole body into memory
    - `Content-Encoding` (gzip/br/deflate) is now preserved on compressed responses — raw bytes are streamed via `aiter_raw()` so the header stays valid and the client can decompress
    - Slow upstreams (e.g. large aggregation queries) no longer 502 at httpx's hardcoded 5s default — the timeout is now configurable via the `timeout` setting
* cleanup / other improvements
    - Added `PreparedRequest` dataclass and split route validation/resolution into `RouteMapper.prepare_remote_request()`; the FastAPI adapter issues the streaming HTTP call
    - `map_route()` (buffered) retained for the Flask/sync path
    - Added streaming test coverage (`TestStreamUpstream`, plus `prepare_remote_request` and streaming-header tests) — 53 tests total
EOF
)"

# 6. Publish to PyPI
pixi run -e dev python -m twine upload dist/*.whl
```


---

# License

BSD 3-Clause

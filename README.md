# API Dock

API Dock (API(s) + (data)Base(s)/base-(for)-API(s)) a flexible API gateway that allows you to proxy requests to multiple remote APIs and Databases through a single endpoint. The proxy can easily be launched as a FastAPI or Flask app, or integrated into any existing python based API.

## Table of Contents

- [Features](#features)
- [Install](#install)
- [Quick Example](#quick-example)
- [CLI](#cli)
  - [Commands](#commands)
  - [Examples](#examples)
- [CONFIGURATION AND SYNTAX](#configuration-and-syntax)
  - [Main Configuration](#main-configuration-api_dock_configconfigyaml)
  - [Remote Configurations](#remote-configurations)
  - [SQL Database Support](#sql-database-support)
  - [URL Query Parameters](#url-query-parameters)
- [Using RouteMapper in Your Own Projects](#using-routemapper-in-your-own-projects)
  - [Basic Integration](#basic-integration)
  - [Framework Examples](#framework-examples)
  - [Database Integration](#database-integration)
- [Requirements](#requirements)
- [License](#license)

## Features

- **Multi-API Proxying**: Route requests to different remote APIs based on configuration
- **SQL Database Support**: Query Parquet files and databases using DuckDB via REST endpoints
- **Cloud Storage Support**: Native support for S3, GCS, HTTPS, and local file paths
- **YAML Configuration**: Simple, human-readable configuration files
- **Access Control**: Define allowed/restricted routes per remote API
- **Version Support**: Handle API versioning in URL paths
- **URL Query Parameters**: Declarative query parameter support for database routes with filtering, sorting, pagination, conditional logic, and direct responses
- **Flexibility**: Quickly launch FastAPI or Flask apps, or easily integrate into any existing framework

## Install

**FROM PYPI**

```bash
pip install api_doc
```

**FROM CONDA**

```bash
 conda install -c conda-forge api_doc
```

## Quick Example

Suppose we have these 3 config files (and similar ones for service2 and service3)

```yaml 
# api_dock_config/config.yaml
name: "My API Dock"
description: "API proxy for multiple services"
authors: ["Your Name"]

# Remote APIs to proxy
remotes:
  - "service1"
  - "service2"
  - "service3"

# SQL databases to query
databases:
  - "db_example"
```

```yaml 
# api_dock_config/remotes/service1.yaml
name: service1
description: Example showing all routing features
url: http://api.example.com

# Unified routes (mix of strings and dicts)
routes:
  # routes with identical signatures
  - health                                  # GET  http://api.example.com/health
  - route: users                            # GET  http://api.example.com/users (using explicit method)
    method: get
  - users/{{user_id}}                       # GET  http://api.example.com/users/{{user_id}}
  - route: users/{{user_id}}/posts          # POST http://api.example.com/users/{{user_id}}/posts
    method: post
  # route with a different signature
  - route: users/{{user_id}}/permissions    # GET  http://api.example.com/user-permissions/{{user_id}}
    remote_route: user-permissions/{{user_id}}
    method: get
```

```yaml 
# api_dock_config/databases/db_example.yaml
name: db_example
description: Example database with Parquet files
authors:
  - API Team

# Table definitions - supports multiple storage backends
tables:
  users: s3://your-bucket/users.parquet                       # S3
  permissions: gs://your-bucket/permissions.parquet           # Google Cloud Storage
  posts: https://storage.googleapis.com/bucket/posts.parquet  # HTTPS
  local_data: tables/local_data.parquet                       # Local filesystem

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

Then just run `pixi run api-dock start` to launch a new api with following endpoints:

- list remote api names and databases: `/`
- list of available db_example queries: `/db_example/users`
  - query example_db for users: `/db_example/users`
  - query example_db for user: `/db_example/users/{{user_id}}`
  - query example_db for user-permissions: `/db_example/users/{{user_id}}/permissions`
- list service1 endpoints: `/service1` 
  - proxy for http://api.example.com/health: `/service1/health`
  - proxy for http://api.example.com/user-permissions/{{user_id}}: `/service1/users/{{user_id}}/permissions`
- list service2|3 endpoints: `/service2|3` 
  - ...

---

# CLI

## Commands

API Dock provides a modern Click-based CLI:

- **api-dock** (default): List all available configurations
- **api-dock init [--force]**: Initialize `api_dock_config/` directory with default configs
- **api-dock start [config_name]**: Start API Dock server with optional config name
- **api-dock describe [config_name]**: Display formatted configuration with expanded SQL queries


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


# these commands also work for alternative configurations (example: api_dock_config/config_v2.yaml)
pixi run api-dock start config_v2
pixi run api-dock describe config_v2
```

**For more details**, see the [Configuration Wiki](https://github.com/yourusername/api_dock/wiki/Configuration).

---

# CONFIGURATION AND SYNTAX

Assume our file structure is:

```bash
api_dock_config
├── config.yaml
├── config_v2.yaml
├── databases
│    ├── analytics_db.yaml
│    └── versioned_db
│        ├── 0.1.yaml
│        ├── 0.5.yaml
│        └── 1.1.yaml
└── remotes
    ├── service1.yaml
    ├── service2.yaml
    └── versioned_service
        ├── 0.1.yaml
        ├── 0.2.yaml
        └── 0.3.yaml
```

---

## Main Configuration (`api_dock_config/config.yaml`)

The main configuration files are stored in the top level of the CWD's `api_dock_config/` directory. By default api-dock expects there to be one called `config.yaml`, however configs with different names (such as `config_v2`) can be added and launched as shown in the CLI Examples section.

```yaml
# api_dock_config/config.yaml
name: "My API Dock"
description: "API proxy for multiple services"
authors: ["Your Name"]

# Remote APIs to proxy
remotes:
  - "service1"           # add configuration in "api_dock_config/remotes/service1.yaml"
  - "service2"           # add configuration in "api_dock_config/remotes/service2.yaml"
  - "versioned_service"  # add configurations in versions in "api_dock_config/remotes/versioned_service/"

# SQL databases to query
databases:
  - "analytics_db"       # adds database configuration in  "api_dock_config/databases/analytics_db.yaml"
  - "versioned_db"       # adds database configurations in  "api_dock_config/databases/versioned_db/"

# Optional HTTP behavior settings
settings:
  add_trailing_slash: true              # Auto-add trailing slash to paths (default: true)
  follow_protocol_downgrades: false     # Allow HTTPS->HTTP redirects (default: false)
```

### Settings

The optional `settings` section controls HTTP behavior:

- **`add_trailing_slash`** (default: `true`): Automatically append a trailing slash to all proxied paths. This prevents 307/301 redirects from remote APIs that require trailing slashes (e.g., `/projects` → `/projects/`). Set to `false` to disable this behavior.

- **`follow_protocol_downgrades`** (default: `false`): Control how HTTP redirects are handled. When `false` (recommended), HTTPS→HTTP redirects are blocked for security. When `true`, allows following redirects that downgrade from HTTPS to HTTP (not recommended for production).

**Example:**
```yaml
settings:
  add_trailing_slash: true              # Avoids redirects by adding trailing slash
  follow_protocol_downgrades: false     # Blocks insecure HTTPS->HTTP redirects
```

---

## Remote Configurations

The example below is a remote configuration. 

```yaml 
# api_dock_config/remotes/service1.yaml
name: service1                 # this is the slug that goes in the url (ie: /service1/users)
url: http://api.example.com    # the base-url of the api being proxied
description: This is an api    # included in response for /service1 route

# Here is where we define the routing
routes:
  # routes with identical signatures
  - health                                  # GET  http://api.example.com/health
  - route: users                            # GET  http://api.example.com/users (using explicit method)
    method: get
  - users/{{user_id}}                       # GET  http://api.example.com/users/{{user_id}}
  - route: users/{{user_id}}/posts          # POST http://api.example.com/users/{{user_id}}/posts
    method: post
  # route with a different signature
  - route: users/{{user_id}}/permissions    # GET  http://api.example.com/user-permissions/{{user_id}}
    remote_route: user-permissions/{{user_id}}
    method: get
```

### Variable Placeholders

Routes use double curly braces `{{}}` for variable placeholders:

- `users` - Matches exactly "users"
- `users/{{user_id}}` - Matches "users/123", "users/abc", etc.
- `users/{{user_id}}/profile` - Matches "users/123/profile"
- `{{}}` - Anonymous variable (matches any single path segment)

### String Routes (Simple GET Routes)

```yaml
routes:
  - users                          # GET /users
  - users/{{user_id}}              # GET /users/123
  - users/{{user_id}}/profile      # GET /users/123/profile
  - posts/{{post_id}}              # GET /posts/456
```

### Dictionary Routes (Custom Methods and Mappings)

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

```yaml
name: restricted_config

...

routes:
  ...

# Simple route restrictions (string format)
restricted:
  - admin/{{}}                       # Block all admin routes (single segment wildcard)
  - users/{{user_id}}/private        # Block private user data
  - system/*                         # Block all routes starting with system/ (prefix wildcard)

# Method-aware restrictions (dict format)
restricted:
  - route: "*"
    method: delete                   # Block all DELETE requests
  - route: "stuff/*"
    method: delete                   # Block DELETE to any route starting with stuff/
  - route: "users/{{user_id}}"
    method: patch                    # Block PATCH requests to user routes
```

**Wildcard Patterns:**
- `{{}}` or `*` - Matches any single path segment (e.g., `users/{{}}` matches `users/123`)
- `prefix/*` - Matches all routes starting with prefix/ (e.g., `admin/*` matches `admin/dashboard`, `admin/users/123`, etc.)
- `*` - When used alone (string format), matches any single-segment route
- `{route: "*", method: "X"}` - When used with a method (dict format), matches ALL routes regardless of path length

**Method-Specific Restrictions:**
- Use dict format with `route` and `method` fields to restrict specific HTTP methods
- When `{route: "*", method: "X"}` is used, it blocks the specified method on ALL routes
- Omit `method` field to restrict all methods for a route
- Methods are case-insensitive (DELETE, delete, Delete all work)

**For more details**, see the [Routing and Restrictions Wiki](https://github.com/yourusername/api_dock/wiki/Routing-and-Restrictions).

---

## SQL Database Support

API Dock can also be used to query Databases. For now only parquet support is working but we will be adding other Databases in the future.


### Database Configuration

Database configurations are stored in `config/databases/` directory. Each database defines:
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
    sql: "[[get_permissions]]"
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

**For more details**, see the [SQL Database Support Wiki](https://github.com/yourusername/api_dock/wiki/SQL-Database-Support).

---

## URL Query Parameters

Database routes support declarative URL query parameters via the `query_params` section. This lets you add filtering, sorting, pagination, conditional logic, and direct responses — all driven by the URL query string.

Routes without a `query_params` section work exactly as before (full backward compatibility).

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
          sql: department = '{{department}}'
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
          sql: department = '{{department}}'
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
          sql: name ILIKE '%{{name}}%'
      - age_min:
          sql: age >= {{age_min}}
      - age_max:
          sql: age <= {{age_max}}
      - department:
          sql: department = '{{department}}'
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

# Using RouteMapper in Your Own Projects

The core functionality is available as a standalone `RouteMapper` class that can be integrated into any web framework:

## Basic Integration

```python
from api_dock.route_mapper import RouteMapper

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
        path="users/123"
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

# Requirements

Requirements are managed through a [Pixi](https://pixi.sh/latest) "project" (similar to a conda environment). After pixi is installed use `pixi run <cmd>` to ensure the correct project is being used. For example,

```bash
# launch jupyter
pixi run jupyter lab .

# run a script
pixi run python scripts/hello_world.py
```

The first time `pixi run` is executed the project will be installed (note this means the first run will be a bit slower). Any changes to the project will be updated on the subsequent `pixi run`.  It is unnecessary, but you can run `pixi install` after changes - this will update your local environment, so that it does not need to be updated on the next `pixi run`.

Note, the repo's `pyproject.toml`, and `pixi.lock` files ensure `pixi run` will just work. No need to recreate an environment. Additionally, the `pyproject.toml` file includes `api_dock = { path = ".", editable = true }`. This line is equivalent to `pip install -e .`, so there is no need to pip install this module.

The project was initially created using a `package_names.txt` and the following steps. Note that this should **NOT** be re-run as it will create a new project (potentially changing package versions).

```bash
#
# IMPORTANT: Do NOT run this unless you explicity want to create a new pixi project
#
# 1. initialize pixi project (in this case the pyproject.toml file had already existed)
pixi init . --format pyproject
# 2. add specified python version
pixi add python=3.11
# 3. add packages (note this will use pixi magic to determine/fix package version ranges)
pixi add $(cat package_names.txt)
# 4. add pypi-packages, if any (note this will use pixi magic to determine/fix package version ranges)
pixi add --pypi $(cat pypi_package_names.txt)
```

---

# License

BSD 3-Clause

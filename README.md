# API Box

API Box is a flexible API gateway that allows you to proxy requests to multiple remote APIs and Databases through a single endpoint. The proxy can easily be launched as a FastAPI or Flask app, or integrated into any existing python based API.

## Table of Contents

- [Features](#features)
- [Quick Example](#quick-example)
- [CLI](#cli)
  - [Commands](#commands)
  - [Examples](#examples)
- [CONFIGURATION AND SYNTAX](#configuration-and-syntax)
  - [Main Configuration](#main-configuration-api_box_configconfigyaml)
  - [Remote Configurations](#remote-configurations)
  - [SQL Database Support](#sql-database-support)
- [Using RouteMapper in Your Own Projects](#using-routemapper-in-your-own-projects)
  - [Basic Integration](#basic-integration)
  - [Framework Examples](#framework-examples)
  - [Database Integration](#database-integration)
- [INSTALL/REQUIREMENTS](#installrequirements)
- [License](#license)

## Features

- **Multi-API Proxying**: Route requests to different remote APIs based on configuration
- **SQL Database Support**: Query Parquet files and databases using DuckDB via REST endpoints
- **Cloud Storage Support**: Native support for S3, GCS, HTTPS, and local file paths
- **YAML Configuration**: Simple, human-readable configuration files
- **Access Control**: Define allowed/restricted routes per remote API
- **Version Support**: Handle API versioning in URL paths
- **Flexibility**: Quickly launch FastAPI or Flask apps, or easily integrate into any existing framework

## Quick Example

Suppose we have these 3 config files (and similar ones for service2 and service3)

```yaml 
# api_box_config/config.yaml
name: "My API Box"
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
# api_box_config/remotes/service1.yaml
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
# api_box_config/databases/db_example.yaml
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

Then just run `pixi run api-box start` to launch a new api with following endpoints:

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

API Box provides a modern Click-based CLI:

- **api-box** (default): List all available configurations
- **api-box init [--force]**: Initialize `api_box_config/` directory with default configs
- **api-box start [config_name]**: Start API Box server with optional config name
- **api-box describe [config_name]**: Display formatted configuration with expanded SQL queries


## Examples

```bash
# Initialize local configuration directory
pixi run api-box init

# List available configurations, and available commands
pixi run api-box

# Start API server
# - default configuration (api_box_config/config.yaml) with FastAPI
pixi run api-box start
# - default configuration with Flask (backbone options: fastapi (default) or flask)
pixi run api-box start --backbone flask
# - specify with host and/or port
pixi run api-box start --host 0.0.0.0 --port 9000


# these commands also work for alternative configurations (example: api_box_config/config_v2.yaml)
pixi run api-box start config_v2
pixi run api-box describe config_v2
```

---

# CONFIGURATION AND SYNTAX

Assume our file structure is:

```bash
api_box_config
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

## Main Configuration (`api_box_config/config.yaml`)

The main configuration files are stored in the top level of the CWD's `api_box_config/` directory. By default api-box expects there to be one called `config.yaml`, however configs with different names (such as `config_v2`) can be added and launched as shown in the CLI Examples section.

```yaml
# api_box_config/config.yaml
name: "My API Box"
description: "API proxy for multiple services"
authors: ["Your Name"]

# Remote APIs to proxy
remotes:
  - "service1"           # add configuration in "api_box_config/remotes/service1.yaml"
  - "service2"           # add configuration in "api_box_config/remotes/service2.yaml"
  - "versioned_service"  # add configurations in versions in "api_box_config/remotes/versioned_service/"

# SQL databases to query
databases:
  - "analytics_db"       # adds database configuration in  "api_box_config/databases/analytics_db.yaml"
  - "versioned_db"       # adds database configurations in  "api_box_config/databases/versioned_db/"
```

---

## Remote Configurations

The example below is a remote configuration. 

```yaml 
# api_box_config/remotes/service1.yaml
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

You can restrict access to specific routes using the `restricted` section:

```yaml
name: restricted_config

...

routes:
  ...

restricted:
  - admin                            # Block all admin routes
  - users/{{user_id}}/private        # Block private user data
  - system/{{system_id}}/config      # Block system configuration
```

---

## SQL Database Support

API Box can also be used to query Databases. For now only parquet support is working but we will be adding other Databases in the future.


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


---

# Using RouteMapper in Your Own Projects

The core functionality is available as a standalone `RouteMapper` class that can be integrated into any web framework:

## Basic Integration

```python
from api_box.route_mapper import RouteMapper

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
from api_box.route_mapper import RouteMapper

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
from api_box.route_mapper import RouteMapper

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
from api_box.route_mapper import RouteMapper
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
from api_box.route_mapper import RouteMapper
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
from api_box.route_mapper import RouteMapper
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

# INSTALL/REQUIREMENTS

Requirements are managed through a [Pixi](https://pixi.sh/latest) "project" (similar to a conda environment). After pixi is installed use `pixi run <cmd>` to ensure the correct project is being used. For example,

```bash
# launch jupyter
pixi run jupyter lab .

# run a script
pixi run python scripts/hello_world.py
```

The first time `pixi run` is executed the project will be installed (note this means the first run will be a bit slower). Any changes to the project will be updated on the subsequent `pixi run`.  It is unnecessary, but you can run `pixi install` after changes - this will update your local environment, so that it does not need to be updated on the next `pixi run`.

Note, the repo's `pyproject.toml`, and `pixi.lock` files ensure `pixi run` will just work. No need to recreate an environment. Additionally, the `pyproject.toml` file includes `api_box = { path = ".", editable = true }`. This line is equivalent to `pip install -e .`, so there is no need to pip install this module.

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

CC-BY-4.0

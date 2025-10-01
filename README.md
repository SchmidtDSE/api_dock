# API Box

A FastAPI-based API proxy that routes requests to multiple remote APIs using YAML configuration files.

## Description

API Box is a flexible API gateway that allows you to proxy requests to multiple remote APIs through a single endpoint. It uses YAML configuration files to define remote API connections, routing rules, and access controls. This makes it easy to manage multiple API integrations and provide a unified interface to your applications.

## Features

- **Multi-API Proxying**: Route requests to different remote APIs based on configuration
- **SQL Database Support**: Query Parquet files and databases using DuckDB via REST endpoints
- **Cloud Storage Support**: Native support for S3, GCS, HTTPS, and local file paths
- **YAML Configuration**: Simple, human-readable configuration files
- **Access Control**: Define allowed/restricted routes per remote API
- **Version Support**: Handle API versioning in URL paths
- **Multiple Backends**: Choose between FastAPI or Flask as your web framework
- **Click-Based CLI**: Modern command-line interface with init/start/describe commands
- **Framework-Agnostic Core**: Standalone RouteMapper for integration with any web framework
- **Type Safety**: Full type hints throughout the codebase

## Quick Start

### Installation

## INSTALL/REQUIREMENTS

Requirements are managed through a [Pixi](https://pixi.sh/latest) "project" (similar to a conda environment). After pixi is installed use `pixi run <cmd>` to ensure the correct project is being used. For example,

```bash
# lauch jupyter
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

### Basic Usage

```bash
# Initialize local configuration directory
pixi run api-box init

# List available configurations
pixi run api-box

# Start API Box with default configuration (FastAPI)
pixi run api-box start

# Start with custom configuration
pixi run api-box start my-config

# Describe configuration (shows expanded SQL queries)
pixi run api-box describe
pixi run api-box describe my-config

# Start with Flask backend
pixi run api-box start --backbone flask

# Start on custom host/port
pixi run api-box start --host 0.0.0.0 --port 9000

# Run with debug logging
pixi run api-box start --log-level debug
```

### CLI Commands

API Box provides a modern Click-based CLI:

- **api-box** (default): List all available configurations
- **api-box init [--force]**: Initialize `api_box_config/` directory with default configs
- **api-box start [config_name]**: Start API Box server with optional config name
- **api-box describe [config_name]**: Display formatted configuration with expanded SQL queries

### Backbone Options

API Box supports multiple web framework backends:

- **fastapi** (default): High-performance async framework with automatic OpenAPI docs
- **flask**: Traditional synchronous framework, widely compatible

## Configuration

### Configuration Discovery

API Box searches for configuration files in this order:
1. `api_box_config/` - Local project configurations
2. `config/` - Project configurations
3. Package default configurations

Run `api-box init` to create `api_box_config/` with default configurations.

### Main Configuration (`config/config.yaml`)

```yaml
name: "My API Box"
description: "API proxy for multiple services"
authors: ["Your Name"]

# Remote APIs to proxy
remotes:
  - "service1"
  - "service2"

# SQL databases to query
databases:
  - "analytics_db"
```

### Remote Configuration (`config/remotes/service1.yaml`)

```yaml
name: "service1"
url: "http://localhost:8001"
description: "First remote service"
```

## Routing Syntax

API Box uses a unified routing configuration that supports both simple string routes and complex dictionary-based routes with custom mappings and HTTP methods.

### Bracket Notation

API Box uses two types of double-bracket notation with distinct purposes:

- **`{{variable}}`** - Route URL variables (path parameters in REST endpoints)
  - Example: `users/{{user_id}}` matches `/users/123`
  - Used in route definitions and SQL WHERE clauses

- **`[[reference]]`** - Configuration value references (table names, named queries)
  - Example: `[[users]]` expands to table file path
  - Used only in SQL queries to reference tables/queries from config

### Route Patterns

Routes use double curly braces `{{}}` for variable placeholders:

- `users` - Matches exactly "users"
- `users/{{user_id}}` - Matches "users/123", "users/abc", etc.
- `users/{{user_id}}/profile` - Matches "users/123/profile"
- `{{}}` - Anonymous variable (matches any single path segment)

### Unified Routes Structure

The `routes` section in remote configuration files supports two formats:

#### 1. String Routes (Simple GET Routes)

```yaml
routes:
  - users                          # GET /users
  - users/{{user_id}}              # GET /users/123
  - users/{{user_id}}/profile      # GET /users/123/profile
  - posts/{{post_id}}              # GET /posts/456
```

#### 2. Dictionary Routes (Custom Methods and Mappings)

```yaml
routes:
  # Different HTTP method
  - route: users/{{user_id}}
    method: post                   # POST /users/123

  # Custom remote mapping
  - route: users/{{user_id}}/permissions
    remote_route: user-permissions/{{user_id}}
    method: get                    # Maps local route to different remote endpoint

  # Complex mapping with multiple variables
  - route: search/{{category}}/{{term}}
    remote_route: api/v2/search/{{term}}/in/{{category}}
    method: get
```

#### 3. Mixed Configuration Example

```yaml
name: my_api_remote
url: http://api.example.com
routes:
  - users                                    # Simple GET route
  - users/{{user_id}}                       # Simple GET route with variable
  - route: users/{{user_id}}/posts         # Custom method
    method: post
  - route: users/{{user_id}}/permissions   # Custom mapping
    remote_route: user-perms/{{user_id}}
    method: get
```

### Variable Naming

- For simple routes, variable names can be descriptive (`{{user_id}}`) or anonymous (`{{}}`)
- For custom mappings, variable names **must match** between `route` and `remote_route`
- Variables are case-sensitive and must be consistent

### Route Restrictions

You can restrict access to specific routes using the `restricted` section:

```yaml
restricted:
  - admin                             # Block all admin routes
  - users/{{user_id}}/private        # Block private user data
  - system/{{system_id}}/config      # Block system configuration
```

### Complete Remote Configuration Example

```yaml
name: comprehensive_remote
description: Example showing all routing features
url: http://api.example.com

# Unified routes (mix of strings and dicts)
routes:
  - health                                    # GET /health
  - users                                     # GET /users
  - users/{{user_id}}                        # GET /users/123
  - users/{{user_id}}/profile               # GET /users/123/profile
  - route: users/{{user_id}}/posts          # POST /users/123/posts
    method: post
  - route: users/{{user_id}}/permissions    # Custom mapping
    remote_route: user-permissions/{{user_id}}
    method: get
  - route: search/{{query}}                  # Different remote structure
    remote_route: api/v2/search?q={{query}}
    method: get

# Routes that are explicitly blocked
restricted:
  - admin
  - users/{{user_id}}/private
  - system/{{system_id}}/delete
```

## SQL Database Support

API Box now supports SQL queries against Parquet files and other data sources using DuckDB. Define databases in your configuration and query them through REST endpoints.

### Database Configuration

Database configurations are stored in `config/databases/` directory. Each database defines:
- **tables**: Mapping of table names to file paths (supports S3, GCS, HTTPS, local paths)
- **queries**: Named SQL queries for reuse
- **routes**: REST endpoints mapped to SQL queries

### Database Configuration Example (`databases/db_example.yaml`)

```yaml
name: db_example
description: Example database with Parquet files
authors:
  - API Team

# Table definitions - supports multiple storage backends
tables:
  users: s3://your-bucket/users.parquet              # S3
  permissions: gs://your-bucket/permissions.parquet  # Google Cloud Storage
  posts: https://storage.googleapis.com/bucket/posts.parquet  # HTTPS
  local_data: tables/local_data.parquet              # Local filesystem

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

### Cloud Storage Support

API Box supports querying data from multiple storage backends through DuckDB:

- **S3**: `s3://bucket/path/to/file.parquet`
- **Google Cloud Storage**: `gs://bucket/path/to/file.parquet`
- **HTTPS**: `https://storage.googleapis.com/bucket/file.parquet`
- **Local Files**: `tables/data.parquet` or `/absolute/path/to/file.parquet`

All table paths are passed directly to DuckDB, which handles authentication and data access natively.

### SQL Syntax

#### Table References: `[[table_name]]`

Use double square brackets to reference tables defined in the `tables` section:

```sql
SELECT [[users]].* FROM [[users]]
```

This automatically expands to:

```sql
SELECT users.* FROM 's3://your-bucket/users.parquet' AS users
```

#### Path Parameters: `{{param_name}}`

Use double curly braces for route parameters:

```yaml
route: users/{{user_id}}
sql: SELECT [[users]].* FROM [[users]] WHERE user_id = {{user_id}}
```

When accessing `/db_example/users/123`, `{{user_id}}` is replaced with `'123'`.

#### Named Queries: `[[query_name]]`

Reference named queries from the `queries` section:

```yaml
routes:
  - route: users/{{user_id}}/permissions
    sql: "[[get_permissions]]"
```

### Viewing Configuration

Use the `describe` command to view your configuration with expanded SQL queries:

```bash
pixi run api-box describe

# Output shows:
# - Basic metadata (name, description, authors)
# - List of remotes
# - List of databases with:
#   - Table definitions (name → file path)
#   - Routes with expanded SQL (e.g., [[users]] → 's3://bucket/users.parquet' AS users)
```

## API Usage

Once running, API Box provides:

- `GET /` - API metadata
- `GET /{config_key}` - Get configuration values
- `/{remote_name}/{version}/{path}` - Proxy to remote APIs
- `/{database_name}/{path}` - Query SQL databases

### Example Requests

```bash
# Get API metadata
curl http://localhost:8000/

# Access remote API through proxy
curl http://localhost:8000/service1/latest/users/

# Access specific API version
curl http://localhost:8000/service1/v2/users/123

# Query SQL database
curl http://localhost:8000/db_example/users

# Query with parameters
curl http://localhost:8000/db_example/users/123
curl http://localhost:8000/db_example/users/123/permissions
```

## Using RouteMapper in Your Own Projects

The core functionality is available as a standalone `RouteMapper` class that can be integrated into any web framework:

### Basic Integration

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

### Framework Examples

#### Django Integration
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

#### Custom Framework Integration
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

## Project Structure

```
api_box/
├── api_box/
│   ├── __init__.py           # Module exports
│   ├── cli.py                # Click-based command-line interface
│   ├── config_discovery.py   # Configuration file discovery
│   ├── config.py             # Configuration management
│   ├── database_config.py    # Database configuration loading
│   ├── sql_builder.py        # SQL query builder with substitutions
│   ├── fast_api.py           # FastAPI application
│   ├── flask_api.py          # Flask application
│   └── route_mapper.py       # Standalone route mapping core
├── config/                   # Default configuration files
│   ├── config.yaml           # Main configuration
│   ├── remotes/              # Remote API configurations
│   └── databases/            # Database configurations
├── README.md
└── pyproject.toml
```

## Development

The project follows Python best practices:

- **Type Safety**: Full type hints throughout
- **Documentation**: Comprehensive docstrings
- **Code Style**: PEP 8 compliance
- **Testing**: Ready for unit and integration tests

## License

CC-BY-4.0

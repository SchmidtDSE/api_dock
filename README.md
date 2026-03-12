# API Dock

API Dock is a flexible API gateway that allows you to proxy requests to multiple remote APIs and Databases through a single endpoint. Using API Dock's CLI, the proxy can easily be launched as a FastAPI or Flask app, or integrated into any existing python based API.

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
# api_dock_config/databases/db_example/0.1.yaml
name: db_example
description: "Example DB Version 0.1"
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
- `/db_example/0.1/users`: queries all users in the "users-database"
- `/db_example/0.1/users/{user_id}`: queries all users in the "users-database" with `user.user_id = user_id`
```

Note: the filename is being used for versioning. An endpoint with "latest" is also generated that will numerically order versions by name and serve the most recent version. For example,
`/service1/0.1` uses the config in `/service1/0.1.yaml` and `/service1/latest` will use the most recent version in the `/service1` folder.

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
```

### HTTP behavior Settings

The optional `settings` section controls HTTP behavior:

- **`add_trailing_slash`** (default: `true`): Automatically append a trailing slash to all proxied paths. This prevents 307/301 redirects from remote APIs that require trailing slashes (e.g., `/projects` → `/projects/`). Set to `false` to disable this behavior.

- **`follow_protocol_downgrades`** (default: `false`): Control how HTTP redirects are handled. When `false` (recommended), HTTPS→HTTP redirects are blocked for security. When `true`, allows following redirects that downgrade from HTTPS to HTTP (not recommended for production).

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

Configure cookies to extract from incoming requests and make them available as template variables:

```yaml
# Enable all cookies (default: false)
cookies: true

# Or specify specific cookies to extract
cookies: [session_id, auth_token, user_preferences]

# Disable all cookies (default behavior)
cookies: false
```

When `cookies: true`, all cookies are accepted and available. When `cookies: false` (default), no cookies are processed except authentication cookies when authentication is configured. When providing a list, only specified cookies are extracted.

Cookies can then be accessed in SQL queries using `{{cookies.cookie_name}}`:

```yaml
# Database route using cookies
routes:
  - route: user/profile
    sql: SELECT * FROM [[users]] WHERE session_id = '{{cookies.session_id}}'
```

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
          sql: event_date >= '{{date_from}}'
      - event_type:
          sql: type = '{{event_type}}'
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
    sql: SELECT * FROM [[user_activity]] WHERE user_id = '{{cookies.user_id}}'

  - route: user-settings
    sql: |
      SELECT * FROM [[user_activity]]
      WHERE user_id = '{{cookies.user_id}}'
      AND session_token = '{{cookies.session_token}}'
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

# License

BSD 3-Clause

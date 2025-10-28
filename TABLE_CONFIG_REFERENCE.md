# Table Configuration Reference

API Dock supports two formats for defining tables in database configurations: **simple string format** and **extended dict format** with metadata.

## Quick Comparison

```yaml
# Simple string format (basic)
tables:
  my_table: s3://bucket/file.parquet

# Extended dict format (with metadata)
tables:
  my_table:
    uri: s3://bucket/file.parquet
    region: us-east-2
```

---

## String Format (Simple)

The simplest way to define a table is with a direct URI string.

**Syntax:**
```yaml
tables:
  table_name: <uri>
```

**Example:**
```yaml
tables:
  users: s3://my-bucket/users.parquet
  orders: gs://my-bucket/orders.parquet
  local_data: /path/to/local/data.parquet
```

**Use when:**
- File is public (no authentication needed)
- Using environment variables for authentication
- No special configuration needed

---

## Dict Format (Extended)

For advanced configuration, use the dict format with metadata.

**Syntax:**
```yaml
tables:
  table_name:
    uri: <uri>          # Required: file URI/path
    region: <region>    # Optional: AWS region
    bearer_token: <token>  # Optional: HTTP bearer token
    auth_headers: <dict>   # Optional: HTTP custom headers
```

### Required Fields

#### `uri` or `path`
The file URI or local path. You can use either `uri` or `path` as the key.

```yaml
tables:
  # Using 'uri' key
  data1:
    uri: s3://bucket/file.parquet

  # Using 'path' key (equivalent)
  data2:
    path: s3://bucket/file.parquet
```

### Optional Fields

**Supported Metadata by Storage Backend:**

| Storage Backend | `region` | `service_account` | `key_id` + `secret` | `endpoint` | `bearer_token` | `auth_headers` | `cookies` |
|-----------------|----------|-------------------|---------------------|------------|----------------|----------------|-----------|
| AWS S3 | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| GCS | ❌ | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ |
| Azure | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| HTTP/HTTPS | ❌ | ❌ | ❌ | ❌ | ✅ | ✅ | ✅ |
| Local | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |

**Notes:**
- **AWS S3**: `region` highly recommended to avoid 301 errors
- **GCS**: Multiple auth options - environment credentials, service account, or HMAC keys
- **Azure**: Uses environment credentials only
- **HTTP/HTTPS**: Can use `bearer_token`, `auth_headers`, `cookies`, or combinations

#### `region` (AWS S3 only)

Specifies the AWS region for S3 buckets. **Highly recommended** to avoid 301 redirect errors.

**Priority:** Config region > `AWS_DEFAULT_REGION` env var > `AWS_REGION` env var > DuckDB auto-detect

```yaml
tables:
  # Without region (may cause 301 errors if bucket not in us-east-1)
  data_bad: s3://my-bucket/data.parquet

  # With region (recommended)
  data_good:
    uri: s3://my-bucket/data.parquet
    region: us-east-2
```

**Why it matters:**
```
# Without region configuration:
Error: HTTP Error: 301 (Moved Permanently)
* Provided region is: "us-east-1"
* Correct region is: "us-east-2"

# With region configuration:
✓ Success! Direct connection to correct region
```

#### `service_account` (GCS only)

Path to a Google Cloud service account JSON key file. **Overrides** the `GOOGLE_APPLICATION_CREDENTIALS` environment variable.

```yaml
tables:
  gcs_data:
    uri: gs://my-bucket/data.parquet
    service_account: /path/to/service-account-key.json
```

**When to use:**
- Different tables need different service accounts
- Don't want to set global `GOOGLE_APPLICATION_CREDENTIALS`
- Per-database authentication requirements

#### `key_id` and `secret` (GCS only)

HMAC access key ID and secret for GCS authentication. Both must be provided together.

```yaml
tables:
  gcs_data:
    uri: gs://my-bucket/data.parquet
    key_id: GOOG1E...
    secret: your-hmac-secret-key
```

**When to use:**
- Using HMAC keys instead of service accounts
- Per-table credential isolation
- Testing with different GCS accounts

**Note:** These override `GCS_ACCESS_KEY_ID` and `GCS_SECRET_ACCESS_KEY` environment variables.

#### `endpoint` (GCS only)

Custom endpoint for GCS-compatible storage services (like MinIO with GCS API).

```yaml
tables:
  gcs_compatible_data:
    uri: gs://my-bucket/data.parquet
    endpoint: https://minio.example.com
    key_id: minioadmin
    secret: minioadmin
```

**When to use:**
- Using GCS-compatible object storage (MinIO, etc.)
- Private cloud deployments
- Development/testing environments

#### `bearer_token` (HTTP/HTTPS only)

Bearer token for HTTP Authorization header.

```yaml
tables:
  api_data:
    uri: https://api.example.com/data.parquet
    bearer_token: "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
```

Equivalent to:
```
Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
```

#### `auth_headers` (HTTP/HTTPS only)

Custom HTTP headers for authentication or other purposes.

```yaml
tables:
  api_data:
    uri: https://api.example.com/data.parquet
    auth_headers:
      Authorization: "Bearer xyz"
      X-API-Key: "abc123"
      X-Custom-Header: "value"
```

**Note:** Can be combined with `cookies`. If `bearer_token` is also present, it will be added to the `Authorization` header automatically.

#### `cookies` (HTTP/HTTPS only)

HTTP cookies for session-based authentication. Useful for APIs that use cookie-based sessions.

```yaml
tables:
  session_api:
    uri: https://api.example.com/data.parquet
    cookies:
      __Secure-next-auth.session-token: "your-session-token"
      sessionid: "abc123xyz"
```

**When to use:**
- APIs using session cookies (like NextAuth, Django sessions, etc.)
- After authenticating via web login
- Cookie-based authentication systems

**Example with Wildlife Sound Hub:**
```yaml
tables:
  soundhub_data:
    uri: https://api.dev.wildlifesoundhub.org/projects.parquet
    cookies:
      __Secure-next-auth.session-token: "${SH_SESSION_TOKEN}"
```

**Note:** Can be combined with `auth_headers` for complex authentication scenarios.

---

## Complete Examples

### Example 1: AWS S3 with Region

```yaml
# api_dock_config/databases/wildlife.yaml
name: wildlife
description: Wildlife observation data from S3

tables:
  # Simple format (uses environment AWS_DEFAULT_REGION or AWS_REGION)
  observations_simple: s3://wildlife-data/observations.parquet

  # Dict format with explicit region (recommended)
  observations:
    uri: s3://wildlife-data/observations.parquet
    region: us-west-2

  # Multiple tables from different regions
  west_coast:
    uri: s3://wildlife-west/data.parquet
    region: us-west-2

  east_coast:
    uri: s3://wildlife-east/data.parquet
    region: us-east-1

routes:
  - route: observations
    sql: SELECT * FROM [[observations]]

  - route: all_data
    sql: |
      SELECT 'west' as region, * FROM [[west_coast]]
      UNION ALL
      SELECT 'east' as region, * FROM [[east_coast]]
```

### Example 1b: Google Cloud Storage (GCS)

```yaml
# api_dock_config/databases/gcs_data.yaml
name: gcs_data
description: Wildlife observation data from Google Cloud Storage

tables:
  # Simple format (uses environment GOOGLE_APPLICATION_CREDENTIALS)
  observations_simple: gs://wildlife-gcs-bucket/observations.parquet

  # Dict format with service account
  observations_prod:
    uri: gs://wildlife-prod-bucket/observations.parquet
    service_account: /secrets/gcs-prod-service-account.json

  # Dict format with HMAC keys
  observations_dev:
    uri: gs://wildlife-dev-bucket/observations.parquet
    key_id: GOOG1E...
    secret: your-hmac-secret-key

  # Multiple GCS buckets with different auth
  raw_data:
    uri: gs://wildlife-raw/data.parquet
    service_account: /secrets/gcs-raw-service-account.json

  processed_data:
    uri: gs://wildlife-processed/data.parquet
    service_account: /secrets/gcs-processed-service-account.json

  # GCS-compatible storage (MinIO)
  minio_data:
    uri: gs://test-bucket/data.parquet
    endpoint: https://minio.example.com
    key_id: minioadmin
    secret: minioadmin

  # Mix GCS with other storage backends
  s3_backup:
    uri: s3://wildlife-backup/data.parquet
    region: us-east-2

routes:
  - route: observations
    sql: SELECT * FROM [[observations_prod]]

  - route: all_sources
    sql: |
      SELECT 'raw' as source, * FROM [[raw_data]]
      UNION ALL
      SELECT 'processed' as source, * FROM [[processed_data]]
      UNION ALL
      SELECT 's3_backup' as source, * FROM [[s3_backup]]
```

**GCS Authentication Options:**

**Option 1: Config-based (recommended for multiple accounts)**
```yaml
# Specify in config file (as shown above)
tables:
  data:
    uri: gs://bucket/file.parquet
    service_account: /path/to/service-account.json
```

**Option 2: Environment variables (simple setup)**
```bash
# Service Account
export GOOGLE_APPLICATION_CREDENTIALS="/path/to/service-account-key.json"

# OR HMAC keys
export GCS_ACCESS_KEY_ID="GOOG1E..."
export GCS_SECRET_ACCESS_KEY="..."

# OR gcloud CLI
gcloud auth application-default login

# Start API Dock
pixi run api-dock start
```

**Priority:** Config `service_account` > `GOOGLE_APPLICATION_CREDENTIALS` env var > `gcloud` credentials

### Example 2: Authenticated HTTP API

```yaml
# api_dock_config/databases/external_api.yaml
name: external_api
description: Data from external authenticated APIs

tables:
  # Public endpoint (no auth)
  public_data: https://data.example.com/public/data.parquet

  # Bearer token authentication
  protected_data:
    uri: https://api.example.com/protected/data.parquet
    bearer_token: "your-jwt-token-here"

  # Custom headers authentication
  custom_auth_data:
    uri: https://api.example.com/custom/data.parquet
    auth_headers:
      X-API-Key: "your-api-key"
      X-Client-ID: "your-client-id"

  # Cookie-based session authentication
  session_data:
    uri: https://api.example.com/session/data.parquet
    cookies:
      sessionid: "abc123xyz"
      csrftoken: "csrf-token-here"

  # Wildlife Sound Hub example (NextAuth session)
  soundhub_projects:
    uri: https://api.dev.wildlifesoundhub.org/projects.parquet
    cookies:
      __Secure-next-auth.session-token: "${SH_SESSION_TOKEN}"

  # Combined: cookies + custom headers
  complex_auth:
    uri: https://api.example.com/data.parquet
    cookies:
      session: "session-token"
    auth_headers:
      X-Request-ID: "req-123"
      X-API-Version: "v2"

routes:
  - route: all
    sql: |
      SELECT 'public' as source, * FROM [[public_data]]
      UNION ALL
      SELECT 'protected' as source, * FROM [[protected_data]]
      UNION ALL
      SELECT 'custom' as source, * FROM [[custom_auth_data]]
      UNION ALL
      SELECT 'session' as source, * FROM [[session_data]]
      UNION ALL
      SELECT 'soundhub' as source, * FROM [[soundhub_projects]]
```

**Setup for Wildlife Sound Hub:**
```bash
# Set your session token from environment
export SH_SESSION_TOKEN="your-nextauth-session-token"

# Start API Dock
pixi run api-dock start

# Query Wildlife Sound Hub data
curl http://localhost:8000/external_api/soundhub_projects
```

### Example 3: Multi-Cloud Configuration

```yaml
# api_dock_config/databases/multi_cloud.yaml
name: multi_cloud
description: Data across multiple cloud providers

tables:
  # AWS S3 with region (dict format)
  aws_data:
    uri: s3://my-aws-bucket/data.parquet
    region: us-east-2

  # Google Cloud Storage (dict format for consistency)
  gcs_data:
    uri: gs://my-gcs-bucket/data.parquet

  # Google Cloud Storage (simple string format also works)
  gcs_data_simple: gs://my-gcs-bucket/another-file.parquet

  # Azure Blob Storage (dict format)
  azure_data:
    uri: azure://my-container/data.parquet

  # Public HTTPS (simple format)
  public_data: https://open-data.example.com/data.parquet

  # Authenticated HTTPS (dict format with bearer token)
  private_api:
    uri: https://api.example.com/data.parquet
    bearer_token: "secret-token"

  # Local files (simple format)
  local_cache: /var/cache/data.parquet

routes:
  - route: combined
    sql: |
      SELECT 'aws' as source, * FROM [[aws_data]]
      UNION ALL
      SELECT 'gcs' as source, * FROM [[gcs_data]]
      UNION ALL
      SELECT 'azure' as source, * FROM [[azure_data]]
      UNION ALL
      SELECT 'public' as source, * FROM [[public_data]]
      UNION ALL
      SELECT 'private' as source, * FROM [[private_api]]
      UNION ALL
      SELECT 'local' as source, * FROM [[local_cache]]
```

**Multi-Cloud Authentication Setup:**
```bash
# AWS credentials (with region)
export AWS_ACCESS_KEY_ID="AKIA..."
export AWS_SECRET_ACCESS_KEY="..."
export AWS_DEFAULT_REGION="us-east-2"

# GCS credentials (service account)
export GOOGLE_APPLICATION_CREDENTIALS="/path/to/gcs-service-account.json"

# Azure credentials
export AZURE_STORAGE_CONNECTION_STRING="DefaultEndpointsProtocol=https;..."

# Start API Dock - it will automatically:
# 1. Detect all storage backends (s3, gcs, azure, http, local)
# 2. Install required DuckDB extensions
# 3. Configure authentication for each backend
# 4. Execute queries seamlessly across all sources
pixi run api-dock start
```

### Example 4: Mixed Format in One Config

You can mix simple and extended formats in the same configuration:

```yaml
name: mixed_format
description: Using both string and dict formats

tables:
  # Simple format (public file or using env credentials)
  table1: s3://bucket/file1.parquet
  table2: gs://bucket/file2.parquet

  # Dict format (needs special configuration)
  table3:
    uri: s3://special-bucket/file3.parquet
    region: eu-west-1

  table4:
    uri: https://api.example.com/file4.parquet
    bearer_token: "token123"

  # Back to simple format
  table5: /local/file5.parquet

routes:
  - route: all
    sql: SELECT * FROM [[table1]] UNION ALL SELECT * FROM [[table3]]
```

---

## Configuration Priority

### AWS Region Priority
1. **Config file** `region` field (highest priority)
2. `AWS_DEFAULT_REGION` environment variable
3. `AWS_REGION` environment variable
4. DuckDB auto-detect (lowest priority, may cause 301 errors)

### HTTP Authentication Priority
1. **Config file** `bearer_token` (takes precedence if present)
2. **Config file** `auth_headers`
3. No authentication (public endpoint)

---

## Best Practices

### ✅ DO

**Specify AWS regions explicitly:**
```yaml
tables:
  data:
    uri: s3://bucket/file.parquet
    region: us-west-2  # Always specify for private S3 buckets
```

**Use environment variables for sensitive tokens:**
```yaml
# Instead of hardcoding tokens:
tables:
  data:
    uri: https://api.example.com/data.parquet
    bearer_token: "${API_TOKEN}"  # Use environment variable

# Set environment variable before starting API Dock:
# export API_TOKEN="your-secret-token"
```

**Group tables by backend for clarity:**
```yaml
tables:
  # AWS S3 tables
  aws_table1:
    uri: s3://bucket/file1.parquet
    region: us-east-2

  aws_table2:
    uri: s3://bucket/file2.parquet
    region: us-east-2

  # GCS tables
  gcs_table1: gs://bucket/file3.parquet
  gcs_table2: gs://bucket/file4.parquet

  # HTTP tables
  api_table:
    uri: https://api.example.com/file5.parquet
    bearer_token: "${API_TOKEN}"
```

### ❌ DON'T

**Don't hardcode sensitive credentials:**
```yaml
# BAD - credentials in config file
tables:
  data:
    uri: https://api.example.com/data.parquet
    bearer_token: "secret-token-12345"  # Don't do this!
```

**Don't omit regions for private S3 buckets:**
```yaml
# BAD - may cause 301 errors
tables:
  data: s3://my-private-bucket/file.parquet  # Where is the region?

# GOOD - explicit region
tables:
  data:
    uri: s3://my-private-bucket/file.parquet
    region: us-west-2
```

**Don't use both bearer_token and auth_headers:**
```yaml
# BAD - conflicting auth methods
tables:
  data:
    uri: https://api.example.com/data.parquet
    bearer_token: "token1"
    auth_headers:
      Authorization: "Bearer token2"  # Which one should be used?
```

---

## Troubleshooting

### "301 (Moved Permanently)" Error

**Cause:** AWS region mismatch.

**Solution:** Add `region` field to your table configuration:
```yaml
tables:
  data:
    uri: s3://bucket/file.parquet
    region: us-east-2  # Set to your bucket's actual region
```

### "401 Unauthorized" Error (HTTP)

**Cause:** Missing or invalid authentication.

**Solution:** Add `bearer_token` or `auth_headers`:
```yaml
tables:
  data:
    uri: https://api.example.com/data.parquet
    bearer_token: "your-valid-token"
```

### "403 Forbidden" Error

**Cause:** Insufficient permissions or invalid credentials.

**Solutions:**
1. For S3: Verify IAM permissions or AWS credentials
2. For HTTP: Verify API key/token is valid and has required permissions
3. Check if file path is correct

---

## Migration Guide

### From String to Dict Format

**Before:**
```yaml
tables:
  data: s3://soundhub-dev/parquet/file.parquet
```

**After (with region):**
```yaml
tables:
  data:
    uri: s3://soundhub-dev/parquet/file.parquet
    region: us-east-2
```

**Backward Compatibility:** String format still works! Only switch to dict format if you need metadata like region or authentication.

---

## Summary

| Format | When to Use | Example |
|--------|-------------|---------|
| **String** | Public files, environment auth | `data: s3://bucket/file.parquet` |
| **Dict + region** | Private S3 buckets | `data: {uri: s3://bucket/file.parquet, region: us-east-2}` |
| **Dict + bearer_token** | Authenticated HTTP APIs | `data: {uri: https://api.example.com/file.parquet, bearer_token: "xyz"}` |
| **Dict + auth_headers** | Custom HTTP authentication | `data: {uri: https://api.example.com/file.parquet, auth_headers: {X-API-Key: "abc"}}` |

**Key Points:**
- ✅ Both formats are supported (use what fits your needs)
- ✅ Config metadata takes priority over environment variables
- ✅ Specify `region` for private S3 buckets to avoid 301 errors
- ✅ Use environment variables for sensitive credentials (don't hardcode)
- ✅ Backward compatible (existing string configs still work)

# Cloud Storage Authentication Guide

API Dock supports querying Parquet files from multiple cloud storage providers with automatic authentication. The system detects the storage backend from your table URIs and configures the appropriate authentication method.

## Supported Storage Backends

| Storage Provider | URI Prefix | Public Files | Private Files |
|-----------------|------------|--------------|---------------|
| AWS S3 | `s3://`, `s3a://` | ✅ | ✅ Credential chain |
| Google Cloud Storage | `gs://` | ✅ | ✅ Credential chain |
| Azure Blob Storage | `azure://`, `az://` | ✅ | ✅ Credential chain |
| HTTP/HTTPS | `http://`, `https://` | ✅ | ⚠️ Custom headers |
| Local Files | `/path/` or `./path/` | ✅ | N/A |

---

## Quick Start

### 1. Public Files (No Authentication)

Public files work immediately - just use the URI in your database configuration:

```yaml
# api_dock_config/databases/public_data.yaml
name: public_data
description: Public datasets

tables:
  # Public S3 bucket
  census: s3://us-census-data/population.parquet

  # Public HTTPS endpoint
  weather: https://data.example.com/weather.parquet

routes:
  - route: census
    sql: SELECT * FROM [[census]]
```

### 2. Private Files (Automatic Authentication)

For private files, configure credentials using standard cloud provider methods (environment variables, config files, or IAM roles). API Dock automatically detects and uses these credentials.

```yaml
# api_dock_config/databases/private_data.yaml
name: private_data
description: Private datasets

tables:
  # Private S3 bucket
  users: s3://my-private-bucket/users.parquet

  # Private GCS bucket
  orders: gs://my-private-bucket/orders.parquet

routes:
  - route: users
    sql: SELECT * FROM [[users]] WHERE active = true
```

---

## AWS S3 Authentication

### Method 1: Environment Variables

```bash
export AWS_ACCESS_KEY_ID="AKIAIOSFODNN7EXAMPLE"
export AWS_SECRET_ACCESS_KEY="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
export AWS_DEFAULT_REGION="us-east-2"  # IMPORTANT: Set to your bucket's actual region!
# Alternative: export AWS_REGION="us-east-2"
export AWS_SESSION_TOKEN="..."  # Only needed for temporary credentials
```

**Why Region Matters:**
Setting the correct region is crucial to avoid 301 (Moved Permanently) redirects. If your bucket is in `us-east-2` but you don't set the region (or set it to `us-east-1`), DuckDB will initially try `us-east-1` and get redirected, which causes errors.

**To find your bucket's region:**
```bash
aws s3api get-bucket-location --bucket your-bucket-name
```

### Method 2: AWS Configuration Files (Recommended for Development)

Create or edit `~/.aws/credentials`:

```ini
[default]
aws_access_key_id = AKIAIOSFODNN7EXAMPLE
aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
```

Optionally, create `~/.aws/config`:

```ini
[default]
region = us-east-1
output = json
```

### Method 3: IAM Roles (Recommended for Production)

Attach an IAM role with S3 read permissions to your compute resource:

**For EC2:**
1. Create IAM role with policy:
   ```json
   {
     "Version": "2012-10-17",
     "Statement": [{
       "Effect": "Allow",
       "Action": ["s3:GetObject", "s3:ListBucket"],
       "Resource": [
         "arn:aws:s3:::my-bucket/*",
         "arn:aws:s3:::my-bucket"
       ]
     }]
   }
   ```
2. Attach role to EC2 instance
3. No credentials needed in environment!

**For ECS/EKS:**
- Use task IAM roles (ECS) or pod IAM roles (EKS)

**For Lambda:**
- Add S3 permissions to function execution role

### Method 4: AWS SSO

```bash
aws sso login --profile my-profile
export AWS_PROFILE=my-profile
```

---

## Google Cloud Storage (GCS) Authentication

### Method 1: Config-Based Service Account (Recommended)

Specify the service account directly in your database configuration:

```yaml
# api_dock_config/databases/my_db.yaml
tables:
  gcs_data:
    uri: gs://my-bucket/data.parquet
    service_account: /path/to/service-account-key.json
```

**Benefits:**
- Per-table authentication
- Different buckets can use different service accounts
- No global environment variables needed

### Method 2: Environment Variable Service Account

1. Create a service account with Storage Object Viewer role
2. Download JSON key file
3. Set environment variable:

```bash
export GOOGLE_APPLICATION_CREDENTIALS="/path/to/service-account-key.json"
```

### Method 3: Config-Based HMAC Keys

Specify HMAC credentials in your database configuration:

```yaml
tables:
  gcs_data:
    uri: gs://my-bucket/data.parquet
    key_id: GOOG1E...
    secret: your-hmac-secret-key
```

**Use when:**
- Using HMAC keys instead of service accounts
- Need per-table credential isolation
- Testing with different GCS accounts

### Method 4: Environment Variable HMAC Keys

1. Generate HMAC keys in GCS console
2. Set environment variables:

```bash
export GCS_ACCESS_KEY_ID="GOOG1E..."
export GCS_SECRET_ACCESS_KEY="..."
```

### Method 5: gcloud CLI

```bash
gcloud auth application-default login
```

### Method 6: Workload Identity (GKE)

When running on Google Kubernetes Engine, use Workload Identity:

```bash
# No environment configuration needed
# GKE automatically injects credentials
```

### Method 7: GCS-Compatible Storage (MinIO, etc.)

For GCS-compatible object storage, specify custom endpoint:

```yaml
tables:
  minio_data:
    uri: gs://bucket/data.parquet
    endpoint: https://minio.example.com
    key_id: minioadmin
    secret: minioadmin
```

### Authentication Priority

1. **Config `service_account`** (highest priority)
2. **Config `key_id` + `secret`**
3. `GOOGLE_APPLICATION_CREDENTIALS` environment variable
4. `GCS_ACCESS_KEY_ID` + `GCS_SECRET_ACCESS_KEY` environment variables
5. gcloud CLI credentials
6. Workload Identity (GKE)

---

## Azure Blob Storage Authentication

### Method 1: Connection String

```bash
export AZURE_STORAGE_CONNECTION_STRING="DefaultEndpointsProtocol=https;AccountName=myaccount;AccountKey=...;EndpointSuffix=core.windows.net"
```

### Method 2: Account Name and Key

```bash
export AZURE_STORAGE_ACCOUNT="myaccount"
export AZURE_STORAGE_ACCESS_KEY="..."
```

### Method 3: Managed Identity (Recommended for Azure VMs)

When running on Azure compute (VM, AKS, Functions), managed identity is automatically detected:

```bash
# No environment configuration needed
# Azure automatically provides credentials
```

### Method 4: Azure CLI

```bash
az login
```

---

## HTTP/HTTPS Endpoints

### Public Endpoints

Public HTTP/HTTPS endpoints work without configuration:

```yaml
tables:
  public_data: https://data.example.com/public/file.parquet
```

### Authenticated Endpoints (Future Enhancement)

Support for custom headers and bearer tokens is planned for future releases. For now, authenticated HTTP endpoints are not supported.

---

## Multi-Cloud Example

You can mix storage backends in a single database configuration:

```yaml
# api_dock_config/databases/multi_cloud.yaml
name: multi_cloud
description: Data from multiple cloud providers

tables:
  # AWS S3
  aws_users: s3://my-aws-bucket/users.parquet
  aws_events: s3://my-aws-bucket/events.parquet

  # Google Cloud Storage
  gcs_analytics: gs://my-gcs-bucket/analytics.parquet
  gcs_logs: gs://my-gcs-bucket/logs.parquet

  # Azure
  azure_backups: azure://my-container/backups.parquet

  # HTTP (public)
  reference_data: https://data.example.com/reference.parquet

  # Local
  local_cache: /var/data/cache.parquet

routes:
  - route: users
    sql: SELECT * FROM [[aws_users]]

  - route: all_data
    sql: |
      SELECT 'aws' as source, * FROM [[aws_users]]
      UNION ALL
      SELECT 'gcs' as source, * FROM [[gcs_analytics]]
      UNION ALL
      SELECT 'azure' as source, * FROM [[azure_backups]]
```

The system automatically:
1. Detects that S3, GCS, and Azure backends are needed
2. Installs aws, httpfs (for GCS), and azure DuckDB extensions
3. Configures credential chains for each backend
4. Executes the query seamlessly across all sources

---

## How It Works

### Automatic Backend Detection

When you define tables in your database configuration, API Dock:

1. **Extracts all table URIs** from the configuration
2. **Detects storage backends** using URI patterns:
   - `s3://` or `s3a://` → AWS S3
   - `gs://` → Google Cloud Storage
   - `azure://` → Azure Blob Storage
   - `http://` or `https://` → HTTP/HTTPS
   - Other → Local files
3. **Installs required DuckDB extensions**:
   - `aws` extension for S3
   - `httpfs` extension for GCS and HTTP/HTTPS
   - `azure` extension for Azure
4. **Configures authentication** using credential chains for each backend

### Credential Chain Priority

Each backend tries multiple authentication methods in order:

**AWS S3:**
1. Environment variables
2. AWS config files (~/.aws/)
3. IAM roles (EC2/ECS/EKS/Lambda)
4. SSO credentials

**GCS:**
1. Service account file (GOOGLE_APPLICATION_CREDENTIALS)
2. HMAC keys (GCS_ACCESS_KEY_ID, GCS_SECRET_ACCESS_KEY)
3. gcloud CLI credentials
4. Workload Identity (GKE)

**Azure:**
1. Connection string (AZURE_STORAGE_CONNECTION_STRING)
2. Account name + key
3. Managed Identity
4. Azure CLI credentials

### Graceful Failure Handling

If authentication setup fails (e.g., no credentials configured), the system:
- ✅ Continues execution (doesn't fail)
- ✅ Allows queries to public files
- ✅ Returns helpful error messages for private files

---

## Troubleshooting

### "HTTP Error: 301 (Moved Permanently) - Bad Request - S3 region being set incorrectly"

**Cause**: Your S3 bucket is in a different region than configured (or defaulting to us-east-1).

**Error message example:**
```
Database query error: HTTP Error: Unable to connect to URL "https://bucket.s3.us-east-1.amazonaws.com/file.parquet":
301 (Moved Permanently).
Bad Request - this can be caused by the S3 region being set incorrectly.
* Provided region is: "us-east-1"
* Correct region is: "us-east-2"
```

**Solutions**:
1. Set the correct AWS region environment variable:
   ```bash
   export AWS_DEFAULT_REGION="us-east-2"
   # or
   export AWS_REGION="us-east-2"
   ```

2. Find your bucket's region:
   ```bash
   aws s3api get-bucket-location --bucket your-bucket-name
   ```

3. Update your AWS config file (`~/.aws/config`):
   ```ini
   [default]
   region = us-east-2
   ```

4. Restart API Dock after setting the region

### "Database query error: IO Error: Unable to open file"

**Cause**: File doesn't exist or credentials are not configured.

**Solutions**:
1. Verify the file URI is correct
2. Check if file is public or private
3. If private, configure credentials as shown above
4. Verify bucket/container permissions
5. **For S3**: Check if you're getting a 301 redirect (see above)

### "Secret Validation Failure"

**Cause**: Credentials are configured but invalid.

**Solutions**:
1. Verify credentials are correct
2. Check credential expiration (temporary credentials)
3. Verify IAM/service account permissions
4. Test credentials with cloud provider CLI tools

### "Extension not found"

**Cause**: Required DuckDB extension failed to install.

**Solutions**:
1. Ensure internet connectivity (extensions download on first use)
2. Check DuckDB version (recommend 1.0.0+)
3. Verify disk space for extension installation

### Mixed Storage Backends Not Working

**Cause**: One backend's credentials are missing.

**Solution**:
- Each backend requires its own credentials
- Configure credentials for all backends you're using
- Use `pixi run api-dock describe` to see which backends are detected

---

## Security Best Practices

### ✅ DO

- **Use IAM roles** instead of access keys when possible
- **Use service accounts** with minimum required permissions
- **Rotate credentials** regularly
- **Use managed identities** on cloud platforms
- **Store credentials** in secure credential stores (AWS Secrets Manager, etc.)
- **Use temporary credentials** (STS, federated access)
- **Limit bucket/container access** to specific prefixes

### ❌ DON'T

- **Don't hardcode credentials** in configuration files
- **Don't commit credentials** to version control
- **Don't use root/admin credentials**
- **Don't grant more permissions than needed**
- **Don't share credentials** across environments (dev/staging/prod)

---

## Examples

### Example 1: AWS S3 with IAM Role

```yaml
# api_dock_config/databases/aws_data.yaml
name: aws_data

tables:
  users: s3://my-private-bucket/users.parquet
  events: s3://my-private-bucket/events.parquet

routes:
  - route: users
    sql: SELECT * FROM [[users]]

  - route: user_events/{{user_id}}
    sql: |
      SELECT e.*
      FROM [[events]] e
      JOIN [[users]] u ON e.user_id = u.id
      WHERE u.id = {{user_id}}
```

**Setup (on EC2):**
```bash
# Attach IAM role to EC2 instance with this policy:
{
  "Effect": "Allow",
  "Action": ["s3:GetObject", "s3:ListBucket"],
  "Resource": [
    "arn:aws:s3:::my-private-bucket/*",
    "arn:aws:s3:::my-private-bucket"
  ]
}

# Start API Dock (credentials automatically detected)
pixi run api-dock start
```

### Example 2: GCS with Service Account

```yaml
# api_dock_config/databases/gcs_data.yaml
name: gcs_data

tables:
  logs: gs://my-gcs-bucket/logs.parquet
  metrics: gs://my-gcs-bucket/metrics.parquet

routes:
  - route: recent_logs
    sql: SELECT * FROM [[logs]] WHERE timestamp > current_timestamp - interval '1 day'
```

**Setup:**
```bash
# Set service account key
export GOOGLE_APPLICATION_CREDENTIALS="/path/to/service-account.json"

# Start API Dock
pixi run api-dock start

# Query the data
curl http://localhost:8000/gcs_data/recent_logs
```

### Example 3: Multi-Cloud Application

```yaml
# api_dock_config/databases/hybrid_cloud.yaml
name: hybrid_cloud
description: Application data across AWS and GCS

tables:
  # User data in AWS
  users: s3://company-users/users.parquet

  # Analytics in GCS (cheaper for data warehouse)
  user_analytics: gs://company-analytics/user_behavior.parquet

  # Backups in Azure (geographic redundancy)
  user_backups: azure://company-backups/users_backup.parquet

routes:
  - route: users/{{user_id}}/analytics
    sql: |
      SELECT
        u.name,
        u.email,
        a.page_views,
        a.session_duration
      FROM [[users]] u
      JOIN [[user_analytics]] a ON u.id = a.user_id
      WHERE u.id = {{user_id}}
```

**Setup:**
```bash
# Configure AWS (IAM role on EC2)
# Already configured via EC2 instance role

# Configure GCS
export GOOGLE_APPLICATION_CREDENTIALS="/app/config/gcs-service-account.json"

# Configure Azure
export AZURE_STORAGE_CONNECTION_STRING="DefaultEndpointsProtocol=https;..."

# Start API Dock
pixi run api-dock start

# Query seamlessly across clouds
curl http://localhost:8000/hybrid_cloud/users/12345/analytics
```

---

## Summary

API Dock's cloud storage authentication:

- ✅ **Automatic backend detection** from URIs
- ✅ **Credential chain authentication** (no hardcoded secrets)
- ✅ **Multi-cloud support** (AWS, GCS, Azure, HTTP)
- ✅ **Graceful failure handling** (public files always work)
- ✅ **Production-ready** (IAM roles, managed identities)
- ✅ **Secure by default** (follows cloud provider best practices)

Configure your credentials once using standard cloud provider methods, and API Dock handles the rest!

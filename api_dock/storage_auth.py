"""

Storage Authentication Module for API Dock

Handles authentication setup for various cloud storage backends (AWS S3, GCS, Azure, HTTP/HTTPS)
in DuckDB queries. Supports both public and private files with credential chain authentication.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import re
from typing import Any, Dict, List, Optional, Set


#
# CONSTANTS
#
# Storage backend detection patterns
S3_PATTERN = re.compile(r'^s3[a]?://', re.IGNORECASE)
GCS_PATTERN = re.compile(r'^gs://', re.IGNORECASE)
AZURE_PATTERN = re.compile(r'^az[ure]*://', re.IGNORECASE)
HTTP_PATTERN = re.compile(r'^https?://', re.IGNORECASE)

# Storage backend types
BACKEND_S3 = 's3'
BACKEND_GCS = 'gcs'
BACKEND_AZURE = 'azure'
BACKEND_HTTP = 'http'
BACKEND_LOCAL = 'local'


#
# PUBLIC
#
def detect_storage_backend(uri: str) -> str:
    """Detect the storage backend from a URI.

    Args:
        uri: File URI or path (e.g., "s3://bucket/file", "gs://bucket/file", "/path/to/file")

    Returns:
        Storage backend type: 's3', 'gcs', 'azure', 'http', or 'local'
    """
    if S3_PATTERN.match(uri):
        return BACKEND_S3
    elif GCS_PATTERN.match(uri):
        return BACKEND_GCS
    elif AZURE_PATTERN.match(uri):
        return BACKEND_AZURE
    elif HTTP_PATTERN.match(uri):
        return BACKEND_HTTP
    else:
        return BACKEND_LOCAL


def extract_table_uris(database_config: Dict[str, Any]) -> List[str]:
    """Extract all table URIs from database configuration.

    Supports both string and dict table definitions:
    - String: "table: s3://bucket/file.parquet"
    - Dict: "table: {uri: s3://bucket/file.parquet, region: us-east-2}"

    Args:
        database_config: Database configuration dictionary with 'tables' section.

    Returns:
        List of table URIs/paths.
    """
    tables = database_config.get('tables', {})
    uris = []

    for table_def in tables.values():
        if isinstance(table_def, str):
            uris.append(table_def)
        elif isinstance(table_def, dict):
            uri = table_def.get('uri') or table_def.get('path')
            if uri:
                uris.append(uri)

    return uris


def extract_table_metadata_by_backend(database_config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Extract table metadata grouped by storage backend.

    Args:
        database_config: Database configuration dictionary with 'tables' section.

    Returns:
        Dictionary mapping backend types to their aggregated metadata.
        Example: {'s3': {'region': 'us-east-2'}, 'http': {'auth_headers': {...}}}
    """
    tables = database_config.get('tables', {})
    backend_metadata = {}

    for table_def in tables.values():
        # Get URI and metadata
        if isinstance(table_def, str):
            uri = table_def
            metadata = {}
        elif isinstance(table_def, dict):
            uri = table_def.get('uri') or table_def.get('path')
            metadata = {k: v for k, v in table_def.items() if k not in ['uri', 'path']}
        else:
            continue

        if not uri:
            continue

        # Detect backend and store metadata
        backend = detect_storage_backend(uri)

        if backend not in backend_metadata:
            backend_metadata[backend] = {}

        # Merge metadata (later tables can override earlier ones)
        backend_metadata[backend].update(metadata)

    return backend_metadata


def detect_required_backends(table_uris: List[str]) -> Set[str]:
    """Detect which storage backends are needed for a list of table URIs.

    Args:
        table_uris: List of table URIs/paths.

    Returns:
        Set of required backend types (e.g., {'s3', 'gcs', 'local'})
    """
    backends = set()
    for uri in table_uris:
        backend = detect_storage_backend(uri)
        backends.add(backend)
    return backends


def setup_storage_authentication(conn: Any, backends: Set[str], metadata: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, bool]:
    """Setup authentication for required storage backends in DuckDB connection.

    This function attempts to configure authentication for each required backend.
    It gracefully handles failures, allowing queries to proceed with public files
    or when credentials are not needed.

    Supported backends:
    - S3: Uses AWS credential chain (env vars, config files, IAM roles)
    - GCS: Uses GCS credential chain (service account, HMAC keys)
    - Azure: Uses Azure credential chain (env vars, managed identity)
    - HTTP/HTTPS: Uses httpfs extension (supports public files)

    Args:
        conn: DuckDB connection object.
        backends: Set of required backend types.
        metadata: Optional dictionary mapping backend types to their configuration metadata.
                 Example: {'s3': {'region': 'us-east-2'}, 'http': {'auth_headers': {...}}}

    Returns:
        Dictionary mapping backend names to setup success status.
        True means authentication was configured, False means it failed but
        the query may still work with public files.
    """
    if metadata is None:
        metadata = {}

    results = {}

    # Setup S3 authentication (AWS)
    if BACKEND_S3 in backends:
        s3_metadata = metadata.get(BACKEND_S3, {})
        results[BACKEND_S3] = _setup_s3_auth(conn, s3_metadata)

    # Setup GCS authentication (Google Cloud Storage)
    if BACKEND_GCS in backends:
        gcs_metadata = metadata.get(BACKEND_GCS, {})
        results[BACKEND_GCS] = _setup_gcs_auth(conn, gcs_metadata)

    # Setup Azure authentication
    if BACKEND_AZURE in backends:
        azure_metadata = metadata.get(BACKEND_AZURE, {})
        results[BACKEND_AZURE] = _setup_azure_auth(conn, azure_metadata)

    # Setup HTTP/HTTPS support
    if BACKEND_HTTP in backends:
        http_metadata = metadata.get(BACKEND_HTTP, {})
        results[BACKEND_HTTP] = _setup_http_support(conn, http_metadata)

    # Local files don't need authentication
    if BACKEND_LOCAL in backends:
        results[BACKEND_LOCAL] = True

    return results


#
# INTERNAL
#
def _setup_s3_auth(conn: Any, metadata: Optional[Dict[str, Any]] = None) -> bool:
    """Setup AWS S3 authentication using credential chain.

    Attempts to configure S3 access using AWS credential chain which automatically
    discovers credentials from environment variables, config files, IAM roles, etc.

    Region configuration priority:
    1. metadata['region'] (from database config)
    2. AWS_DEFAULT_REGION or AWS_REGION environment variable
    3. None (DuckDB auto-detect, may cause 301 redirects)

    Args:
        conn: DuckDB connection object.
        metadata: Optional metadata dict that may contain 'region' key.

    Returns:
        True if setup succeeded, False if it failed (but query may still work with public files).
    """
    try:
        import os

        if metadata is None:
            metadata = {}

        conn.execute("INSTALL aws;")
        conn.execute("LOAD aws;")

        # Determine AWS region with priority:
        # 1. Config file metadata (most specific)
        # 2. Environment variables
        # 3. None (auto-detect)
        aws_region = (
            metadata.get('region') or
            os.environ.get('AWS_DEFAULT_REGION') or
            os.environ.get('AWS_REGION')
        )

        # Configure S3 authentication using AWS credential chain
        # This automatically discovers credentials from:
        # - Environment variables (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN)
        # - AWS config files (~/.aws/credentials, ~/.aws/config)
        # - IAM roles (EC2, ECS, EKS, Lambda)
        # - SSO credentials
        # - Other AWS SDK credential providers
        if aws_region:
            # If region is specified, include it in the secret
            conn.execute(f"""
                CREATE OR REPLACE SECRET (
                    TYPE s3,
                    PROVIDER credential_chain,
                    REGION '{aws_region}'
                );
            """)
        else:
            # No region specified, let DuckDB auto-detect
            # Note: This may cause 301 redirects if bucket is in a different region
            conn.execute("""
                CREATE OR REPLACE SECRET (
                    TYPE s3,
                    PROVIDER credential_chain
                );
            """)
        return True
    except Exception:
        # Authentication setup failed, but public S3 files may still work
        return False


def _setup_gcs_auth(conn: Any, metadata: Optional[Dict[str, Any]] = None) -> bool:
    """Setup GCS authentication using credential chain.

    Attempts to configure GCS access using credential chain which automatically
    discovers credentials from environment variables, service account files, etc.

    Supports metadata for advanced configuration:
    - service_account: Path to service account JSON file (overrides GOOGLE_APPLICATION_CREDENTIALS)
    - key_id: HMAC access key ID (overrides GCS_ACCESS_KEY_ID)
    - secret: HMAC secret key (overrides GCS_SECRET_ACCESS_KEY)
    - endpoint: Custom endpoint for GCS-compatible storage

    Args:
        conn: DuckDB connection object.
        metadata: Optional metadata dict with GCS-specific configuration.

    Returns:
        True if setup succeeded, False if it failed (but query may still work with public files).
    """
    try:
        import os

        if metadata is None:
            metadata = {}

        # Install httpfs extension (required for GCS)
        conn.execute("INSTALL httpfs;")
        conn.execute("LOAD httpfs;")

        # Check if explicit credentials are provided in metadata
        key_id = metadata.get('key_id')
        secret = metadata.get('secret')
        service_account = metadata.get('service_account')
        endpoint = metadata.get('endpoint')

        # Priority for service account:
        # 1. Metadata service_account path
        # 2. GOOGLE_APPLICATION_CREDENTIALS env var
        if service_account:
            # Set environment variable for this session
            os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = service_account

        # Configure GCS authentication
        if key_id and secret:
            # Use explicit HMAC credentials from config
            secret_parts = [
                "TYPE gcs",
                f"KEY_ID '{key_id}'",
                f"SECRET '{secret}'"
            ]

            if endpoint:
                secret_parts.append(f"ENDPOINT '{endpoint}'")

            secret_sql = f"CREATE OR REPLACE SECRET ({', '.join(secret_parts)});"
            conn.execute(secret_sql)
        else:
            # Use credential chain (environment variables, service account, etc.)
            # This automatically discovers credentials from:
            # - Environment variables (GCS_ACCESS_KEY_ID, GCS_SECRET_ACCESS_KEY)
            # - Service account files (GOOGLE_APPLICATION_CREDENTIALS)
            # - HMAC keys from GCS settings
            conn.execute("""
                CREATE OR REPLACE SECRET (
                    TYPE gcs,
                    PROVIDER credential_chain
                );
            """)
        return True
    except Exception:
        # Authentication setup failed, but public GCS files may still work
        return False


def _setup_azure_auth(conn: Any, metadata: Optional[Dict[str, Any]] = None) -> bool:
    """Setup Azure Blob Storage authentication using credential chain.

    Attempts to configure Azure access using credential chain which automatically
    discovers credentials from environment variables, managed identity, etc.

    Args:
        conn: DuckDB connection object.
        metadata: Optional metadata dict (currently unused for Azure, reserved for future).

    Returns:
        True if setup succeeded, False if it failed (but query may still work with public files).
    """
    try:
        if metadata is None:
            metadata = {}

        conn.execute("INSTALL azure;")
        conn.execute("LOAD azure;")

        # Configure Azure authentication using credential chain
        # This automatically discovers credentials from:
        # - Environment variables (AZURE_STORAGE_CONNECTION_STRING, AZURE_STORAGE_ACCOUNT, etc.)
        # - Managed Identity (when running on Azure)
        # - Azure CLI credentials
        conn.execute("""
            CREATE OR REPLACE SECRET (
                TYPE azure,
                PROVIDER credential_chain
            );
        """)
        return True
    except Exception:
        # Authentication setup failed, but public Azure files may still work
        return False


def _setup_http_support(conn: Any, metadata: Optional[Dict[str, Any]] = None) -> bool:
    """Setup HTTP/HTTPS support.

    Installs httpfs extension for HTTP/HTTPS access. If metadata contains
    auth_headers or bearer_token, configures HTTP authentication.

    Args:
        conn: DuckDB connection object.
        metadata: Optional metadata dict that may contain:
                 - bearer_token: Bearer token for Authorization header
                 - auth_headers: Dict of custom HTTP headers

    Returns:
        True if setup succeeded, False if it failed.
    """
    try:
        if metadata is None:
            metadata = {}

        # Install httpfs extension (supports HTTP/HTTPS)
        conn.execute("INSTALL httpfs;")
        conn.execute("LOAD httpfs;")

        # Setup HTTP authentication if provided
        bearer_token = metadata.get('bearer_token')
        auth_headers = metadata.get('auth_headers')

        if bearer_token:
            # Use bearer token authentication
            conn.execute(f"""
                CREATE OR REPLACE SECRET http_auth (
                    TYPE http,
                    BEARER_TOKEN '{bearer_token}'
                );
            """)
        elif auth_headers:
            # Use custom headers
            # Convert dict to DuckDB MAP format
            headers_str = ', '.join([f"'{k}': '{v}'" for k, v in auth_headers.items()])
            conn.execute(f"""
                CREATE OR REPLACE SECRET http_auth (
                    TYPE http,
                    EXTRA_HTTP_HEADERS MAP {{{headers_str}}}
                );
            """)

        return True
    except Exception:
        return False

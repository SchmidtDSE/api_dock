"""

Storage Authentication Module for API Dock

Handles authentication setup for various cloud storage backends (AWS S3, GCS, Azure, HTTP/HTTPS)
in DuckDB queries. Supports both public and private files with credential chain authentication.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import logging
import os
import re
from typing import Any, Dict, List, Optional, Set

from api_dock.types import TableReference


#
# CONSTANTS
#
# Storage backend detection patterns
S3_PATTERN = re.compile(r'^s3[a]?://', re.IGNORECASE)
GCS_PATTERN = re.compile(r'^gs://', re.IGNORECASE)
AZURE_PATTERN = re.compile(r'^az[ure]*://', re.IGNORECASE)
HTTP_PATTERN = re.compile(r'^https?://', re.IGNORECASE)

# DuckDB reads a GCS service account file from this (process-wide) variable.
GOOGLE_CREDENTIALS_ENV: str = "GOOGLE_APPLICATION_CREDENTIALS"

logger = logging.getLogger(__name__)

# Storage backend types
BACKEND_S3 = 's3'
BACKEND_GCS = 'gcs'
BACKEND_AZURE = 'azure'
BACKEND_HTTP = 'http'
BACKEND_LOCAL = 'local'

# S3 metadata keys that change how a table's secret is built. A table whose
# value for any of these differs from the connection default gets its own
# path-scoped secret.
S3_SECRET_KEYS: tuple = ('region', 'public')

# Name prefix for per-table, path-scoped S3 secrets.
S3_SCOPED_SECRET_PREFIX: str = 'api_dock_s3_table'

# Characters that start a glob pattern in a URI; a secret scope stops before them.
GLOB_CHARACTERS: str = '*?[{'


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


def setup_table_storage_authentication(
        conn: Any,
        tables: List[TableReference]) -> Dict[str, bool]:
    """Set up storage authentication for a specific set of tables.

    First configures one default secret per backend from the tables' merged
    metadata (the same behavior as setup_storage_authentication). Then, for
    each S3 table whose ``region``/``public`` differs from that default, adds a
    secret scoped to the table's URI prefix, so tables in different regions or
    with different access settings can be read in a single query. DuckDB uses
    the secret with the longest matching scope.

    Args:
        conn: DuckDB connection object.
        tables: Tables the query may read, with effective metadata.

    Returns:
        Dictionary mapping backend names (and scoped secret names) to setup
        success status.
    """
    backends = detect_required_backends([table.uri for table in tables])
    backend_metadata: Dict[str, Dict[str, Any]] = {}
    for table in tables:
        backend_metadata.setdefault(detect_storage_backend(table.uri), {}).update(table.metadata)

    results = setup_storage_authentication(conn, backends, backend_metadata)

    default_s3 = backend_metadata.get(BACKEND_S3, {})
    scoped_secrets: Dict[str, str] = {}
    for table in tables:
        if detect_storage_backend(table.uri) != BACKEND_S3:
            continue
        if not _differs_from_default(table.metadata, default_s3):
            continue
        scope = _secret_scope(table.uri)
        if scope in scoped_secrets:
            continue
        secret_name = f"{S3_SCOPED_SECRET_PREFIX}_{len(scoped_secrets)}"
        scoped_secrets[scope] = secret_name
        results[secret_name] = _setup_s3_auth(
            conn, table.metadata, secret_name=secret_name, scope=scope
        )

    return results


#
# INTERNAL
#
def _differs_from_default(metadata: Dict[str, Any], default: Dict[str, Any]) -> bool:
    """Check whether a table's S3 settings differ from the connection default.

    Keys the table does not set are treated as inherited from the default.

    Args:
        metadata: The table's effective metadata.
        default: The merged default S3 metadata for the connection.

    Returns:
        True if any of S3_SECRET_KEYS is set on the table to a different value.
    """
    return any(
        key in metadata and metadata[key] != default.get(key)
        for key in S3_SECRET_KEYS
    )


def _secret_scope(uri: str) -> str:
    """Return the path prefix a table's scoped secret should cover.

    A plain file URI is its own scope. A glob URI is cut back to the directory
    before the first glob character (``s3://b/owl/**/*.parquet`` -> ``s3://b/owl/``).

    Args:
        uri: The table URI.

    Returns:
        Scope prefix for a DuckDB secret.
    """
    cut = min((uri.find(char) for char in GLOB_CHARACTERS if char in uri), default=-1)
    if cut < 0:
        return uri
    return uri[:uri.rfind('/', 0, cut) + 1]


def _sql_string(value: Any) -> str:
    """Write a value as a DuckDB string literal (single quotes doubled).

    Args:
        value: The value.

    Returns:
        The quoted literal.
    """
    return "'" + str(value).replace("'", "''") + "'"


def _s3_secret_sql(options: List[str], secret_name: Optional[str], scope: Optional[str]) -> str:
    """Build a CREATE OR REPLACE SECRET statement for S3.

    Args:
        options: Secret options after ``TYPE s3`` (e.g. ``"REGION 'us-west-2'"``).
        secret_name: Secret name, or None for DuckDB's default unnamed secret.
        scope: URI prefix the secret applies to, or None for all of S3.

    Returns:
        SQL statement string.
    """
    all_options = ['TYPE s3', *options]
    if scope:
        escaped_scope = scope.replace("'", "''")
        all_options.append(f"SCOPE '{escaped_scope}'")
    name_part = f" {secret_name}" if secret_name else ""
    return f"CREATE OR REPLACE SECRET{name_part} ({', '.join(all_options)});"


def _setup_s3_auth(
        conn: Any,
        metadata: Optional[Dict[str, Any]] = None,
        secret_name: Optional[str] = None,
        scope: Optional[str] = None) -> bool:
    """Setup AWS S3 authentication using credential chain.

    Attempts to configure S3 access using AWS credential chain which automatically
    discovers credentials from environment variables, config files, IAM roles, etc.

    Region configuration priority:
    1. metadata['region'] (from database config)
    2. AWS_DEFAULT_REGION or AWS_REGION environment variable
    3. None (DuckDB auto-detect, may cause 301 redirects)

    Args:
        conn: DuckDB connection object.
        metadata: Optional metadata dict that may contain 'region' key and 'public' flag.
        secret_name: Optional secret name. None replaces DuckDB's default S3 secret;
            a name adds a separate secret alongside it.
        scope: Optional URI prefix the secret applies to (None = all of S3).

    Returns:
        True if setup succeeded, False if it failed (but query may still work with public files).
    """
    try:

        if metadata is None:
            metadata = {}

        conn.execute("INSTALL aws;")
        conn.execute("LOAD aws;")

        # Check if this is explicitly marked as public access
        is_public = metadata.get('public', False)

        # Determine AWS region with priority:
        # 1. Config file metadata (most specific)
        # 2. Environment variables
        # 3. None (auto-detect)
        aws_region = (
            metadata.get('region') or
            os.environ.get('AWS_DEFAULT_REGION') or
            os.environ.get('AWS_REGION')
        )

        region_options = [f"REGION {_sql_string(aws_region)}"] if aws_region else []

        # For public buckets, try anonymous access first
        if is_public:
            try:
                conn.execute(_s3_secret_sql(
                    ["KEY_ID ''", "SECRET ''", *region_options], secret_name, scope
                ))
                return True
            except Exception:
                pass  # Fall through to credential chain

        # Configure S3 authentication using AWS credential chain
        # This automatically discovers credentials from:
        # - Environment variables (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN)
        # - AWS config files (~/.aws/credentials, ~/.aws/config)
        # - IAM roles (EC2, ECS, EKS, Lambda)
        # - SSO credentials
        # - Other AWS SDK credential providers
        # Without a region DuckDB auto-detects it, which may cause 301 redirects
        # if the bucket is in a different region.
        conn.execute(_s3_secret_sql(
            ["PROVIDER credential_chain", *region_options], secret_name, scope
        ))
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
            # DuckDB reads the service account only from the process-wide
            # environment, so one server can use one: set it if unset, and warn
            # (keeping the first) if a table asks for another.
            current = os.environ.get(GOOGLE_CREDENTIALS_ENV)
            if current is None:
                os.environ[GOOGLE_CREDENTIALS_ENV] = str(service_account)
            elif current != str(service_account):
                logger.warning(
                    "Table service_account %s ignored: %s is already %s (one service account "
                    "per server)", service_account, GOOGLE_CREDENTIALS_ENV, current
                )

        # Check if this is explicitly marked as public access
        is_public = metadata.get('public', False)

        # For public buckets, try anonymous access first
        if is_public:
            try:
                # GCS public access doesn't require credentials
                return True
            except Exception:
                pass  # Fall through to credential chain

        # Configure GCS authentication
        if key_id and secret:
            # Use explicit HMAC credentials from config
            secret_parts = [
                "TYPE gcs",
                f"KEY_ID {_sql_string(key_id)}",
                f"SECRET {_sql_string(secret)}",
            ]

            if endpoint:
                secret_parts.append(f"ENDPOINT {_sql_string(endpoint)}")

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
    auth_headers, bearer_token, or cookies, configures HTTP authentication.

    Args:
        conn: DuckDB connection object.
        metadata: Optional metadata dict that may contain:
                 - bearer_token: Bearer token for Authorization header
                 - auth_headers: Dict of custom HTTP headers
                 - cookies: Dict of cookies to send with requests

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
        auth_headers = metadata.get('auth_headers', {})
        cookies = metadata.get('cookies', {})

        # Build headers dict
        headers = dict(auth_headers) if auth_headers else {}

        # Add bearer token to headers if provided
        if bearer_token:
            headers['Authorization'] = f'Bearer {bearer_token}'

        # Add cookies to headers if provided
        # Cookies are sent via the Cookie header
        if cookies:
            cookie_str = '; '.join([f'{k}={v}' for k, v in cookies.items()])
            headers['Cookie'] = cookie_str

        # Create HTTP secret with headers if any are configured
        if headers:
            # Convert dict to DuckDB MAP format
            headers_str = ', '.join(
                f"{_sql_string(k)}: {_sql_string(v)}" for k, v in headers.items()
            )
            conn.execute(f"""
                CREATE OR REPLACE SECRET http_auth (
                    TYPE http,
                    EXTRA_HTTP_HEADERS MAP {{{headers_str}}}
                );
            """)

        return True
    except Exception:
        return False

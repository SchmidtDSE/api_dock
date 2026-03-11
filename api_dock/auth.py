"""

Authentication Module for API Dock

Provides authentication validation for cookies and other authentication tokens.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import json
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple, Union

from api_dock.encryption import create_encryption_provider, decrypt_value_if_needed, EncryptionError


#
# CONSTANTS
#
DEFAULT_CACHE_TTL: int = 300  # 5 minutes
DEFAULT_STATUS_CODE: int = 401


#
# PUBLIC
#
class AuthenticationProvider(ABC):
    """Abstract base class for authentication providers."""

    def __init__(self, failed_response: Optional[Dict[str, Any]] = None):
        """Initialize authentication provider with common settings.

        Args:
            failed_response: Custom response for failed authentication.
        """
        self.failed_response_config = failed_response or {}


    def validate(self, token: str) -> bool:
        """Validate token against a set of valid tokens with string normalization.

        Args:
            token: Token to validate.
            valid_tokens: Set of valid tokens.

        Returns:
            True if token is valid, False otherwise.
        """
        normalized_tokens = self._normalize_token_set(self.expected_values)
        return str(token) in normalized_tokens


    def get_failed_response(self) -> Tuple[int, Any]:
        """Get the response to return when authentication fails.

        Returns:
            Tuple of (status_code, response_body).
        """
        status_code = self.failed_response_config.get("status", DEFAULT_STATUS_CODE)

        # Return configured response body, or default message
        if self.failed_response_config:
            # Return the full response object including status in body
            return (status_code, self.failed_response_config)
        else:
            return (status_code, {"error": "Authentication failed"})

    def _normalize_token_set(self, values: set) -> set:
        """Convert all tokens to strings for consistent comparison.

        Args:
            values: Set of token values.

        Returns:
            Set of string tokens.
        """
        return {str(v) for v in values}


class FixedValueAuth(AuthenticationProvider):
    """Authentication using a single fixed value."""

    def __init__(self, value: str, encrypted: bool = True, encryption_config: Optional[Dict[str, Any]] = None, failed_response: Optional[Dict[str, Any]] = None):
        """Initialize fixed value authentication.

        Args:
            value: The authentication value (encrypted or plaintext).
            encrypted: Whether the value is encrypted.
            encryption_config: Encryption configuration for decryption.
            failed_response: Custom response for failed authentication.

        Raises:
            AuthenticationError: If value cannot be decrypted.
        """
        super().__init__(failed_response)

        try:
            self.expected_values = [decrypt_value_if_needed(value, encrypted, encryption_config)]
        except EncryptionError as e:
            raise AuthenticationError(f"Failed to decrypt authentication value: {str(e)}")


class ListValueAuth(AuthenticationProvider):
    """Authentication using a list of allowed values."""

    def __init__(self, values: List[Union[str, Dict[str, Any]]], encrypted: bool = True, encryption_config: Optional[Dict[str, Any]] = None, failed_response: Optional[Dict[str, Any]] = None):
        """Initialize list value authentication.

        Args:
            values: List of authentication values (encrypted or plaintext).
            encrypted: Whether values are encrypted by default.
            encryption_config: Encryption configuration for decryption.
            failed_response: Custom response for failed authentication.

        Raises:
            AuthenticationError: If values cannot be processed.
        """
        super().__init__(failed_response)
        self.expected_values = set()

        for value_item in values:
            try:
                # Handle both string and dict formats
                if isinstance(value_item, (str, int, float)):
                    # Simple string - use global encrypted setting
                    decrypted = decrypt_value_if_needed(value_item, encrypted, encryption_config)
                    self.expected_values.add(str(decrypted))
                elif isinstance(value_item, dict):
                    # Dict format with individual encryption setting
                    value_str = value_item.get("value", "")
                    value_encrypted = value_item.get("encrypted", encrypted)
                    decrypted = decrypt_value_if_needed(value_str, value_encrypted, encryption_config)
                    self.expected_values.add(str(decrypted))
                else:
                    raise AuthenticationError(f"Invalid value format: {type(value_item)}")
            except EncryptionError as e:
                raise AuthenticationError(f"Failed to decrypt authentication value: {str(e)}")


class FileAuth(AuthenticationProvider):
    """Authentication using values from a text file (one value per line)."""

    def __init__(self, filepath: str, encrypted: bool = True, encryption_config: Optional[Dict[str, Any]] = None, failed_response: Optional[Dict[str, Any]] = None):
        """Initialize file-based authentication.

        Args:
            filepath: Path to file containing authentication values (one per line).
            encrypted: Whether values in file are encrypted.
            encryption_config: Encryption configuration for decryption.
            failed_response: Custom response for failed authentication.

        Raises:
            AuthenticationError: If file cannot be read or values cannot be processed.
        """
        super().__init__(failed_response)
        self.expected_values = set()

        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line or line.startswith('#'):  # Skip empty lines and comments
                        continue

                    try:
                        decrypted = decrypt_value_if_needed(line, encrypted, encryption_config)
                        self.expected_values.add(str(decrypted))
                    except EncryptionError as e:
                        raise AuthenticationError(f"Failed to decrypt value on line {line_num} in '{filepath}': {str(e)}")
        except FileNotFoundError:
            raise AuthenticationError(f"Authentication file not found: {filepath}")
        except IOError as e:
            raise AuthenticationError(f"Failed to read authentication file '{filepath}': {str(e)}")


class AWSSecretsAuth(AuthenticationProvider):
    """Authentication using AWS Secrets Manager."""

    def __init__(self, secret_name: str, region: str = "us-west-2", cache_ttl: int = DEFAULT_CACHE_TTL, failed_response: Optional[Dict[str, Any]] = None):
        """Initialize AWS Secrets authentication.

        Args:
            secret_name: Name of the secret in AWS Secrets Manager.
            region: AWS region.
            cache_ttl: How long to cache the secret values (seconds).
            failed_response: Custom response for failed authentication.

        Raises:
            AuthenticationError: If AWS setup fails.
        """
        super().__init__(failed_response)

        try:
            import boto3
            from botocore.exceptions import ClientError, NoCredentialsError
        except ImportError:
            raise AuthenticationError("boto3 package is required for AWS Secrets authentication")

        self.secret_name = secret_name
        self.region = region
        self.cache_ttl = cache_ttl
        try:
            self.secrets_client = boto3.client('secretsmanager', region_name=region)
            # Test credentials
            self.secrets_client.list_secrets(MaxResults=1)
        except NoCredentialsError:
            raise AuthenticationError("AWS credentials not found")
        except Exception as e:
            raise AuthenticationError(f"Failed to initialize AWS Secrets Manager client: {str(e)}")

        # Cache for secret values
        self._cached_values = set()
        self._cache_time = 0
        self.expected_values = self._get_cached_tokens()


    def _get_cached_tokens(self) -> set:
        """Get authentication tokens from cache or refresh from AWS."""
        current_time = time.time()
        # Check if cache is still valid
        if current_time - self._cache_time < self.cache_ttl and self._cached_values:
            return self._cached_values

        # Refresh cache
        try:
            import boto3
            from botocore.exceptions import ClientError

            response = self.secrets_client.get_secret_value(SecretId=self.secret_name)
            secret_data = response['SecretString']

            # Parse secret data
            try:
                # Try JSON format first (list of tokens or single value)
                parsed_data = json.loads(secret_data)
                if isinstance(parsed_data, (int, float)):
                    parsed_data = str(parsed_data)
                if isinstance(parsed_data, list):
                    self._cached_values = {str(v) for v in parsed_data}
                elif isinstance(parsed_data, str):
                    self._cached_values = {str(parsed_data)}
                elif isinstance(parsed_data, dict):
                    # Extract values from dict (common AWS pattern)
                    self._cached_values = {str(v) for v in parsed_data.values()}
                else:
                    raise AuthenticationError(f"Invalid secret format: {type(parsed_data)}")
            except json.JSONDecodeError:
                # Treat as plain text (single value)
                self._cached_values = {str(secret_data.strip())}
            self._cache_time = current_time
            return self._cached_values

        except ClientError as e:
            error_code = e.response['Error']['Code']
            if error_code == 'ResourceNotFoundException':
                raise AuthenticationError(f"Secret '{self.secret_name}' not found")
            else:
                raise AuthenticationError(f"AWS Secrets error: {str(e)}")
        except Exception as e:
            raise AuthenticationError(f"Failed to retrieve secret: {str(e)}")


class AWSKMSAuth(AuthenticationProvider):
    """Authentication using AWS KMS for encrypted tokens."""

    def __init__(self, tokens: Optional[List[str]] = None, aws_tokens_file: Optional[str] = None, aws_key_id: str = None, aws_region: str = "us-west-2", failed_response: Optional[Dict[str, Any]] = None):
        """Initialize AWS KMS authentication.

        Args:
            tokens: List of encrypted tokens (for inline configuration).
            aws_tokens_file: Path to file containing encrypted tokens (one per line).
            aws_key_id: AWS KMS key ID or ARN.
            aws_region: AWS region.
            failed_response: Custom response for failed authentication.

        Raises:
            AuthenticationError: If AWS setup fails or configuration is invalid.
        """
        try:
            import boto3
            from botocore.exceptions import ClientError, NoCredentialsError
        except ImportError:
            raise AuthenticationError("boto3 package is required for AWS KMS authentication")

        # Validate exactly one token source
        if tokens and aws_tokens_file:
            raise AuthenticationError("Cannot specify both 'aws_tokens' and 'aws_tokens_file'")
        if not tokens and not aws_tokens_file:
            raise AuthenticationError("Must specify either 'aws_tokens' or 'aws_tokens_file'")

        super().__init__(failed_response)

        self.aws_key_id = aws_key_id
        self.aws_region = aws_region

        try:
            self.kms_client = boto3.client('kms', region_name=aws_region)
            # Test credentials
            self.kms_client.list_keys(Limit=1)
        except NoCredentialsError:
            raise AuthenticationError("AWS credentials not found")
        except Exception as e:
            raise AuthenticationError(f"Failed to initialize AWS KMS client: {str(e)}")

        # Load encrypted tokens
        if tokens:
            encrypted_tokens = tokens
        else:
            encrypted_tokens = self._load_tokens_from_file(aws_tokens_file)

        # Decrypt all tokens once during initialization
        self.expected_values = set()
        for encrypted_token in encrypted_tokens:
            try:
                decrypted = self._decrypt_token(encrypted_token)
                self.expected_values.add(str(decrypted))
            except Exception as e:
                raise AuthenticationError(f"Failed to decrypt token: {str(e)}")

    def _load_tokens_from_file(self, filepath: str) -> List[str]:
        """Load encrypted tokens from a file."""
        try:
            tokens = []
            with open(filepath, 'r', encoding='utf-8') as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line or line.startswith('#'):  # Skip empty lines and comments
                        continue
                    tokens.append(line)

            if not tokens:
                raise AuthenticationError(f"No valid tokens found in file '{filepath}'")

            return tokens
        except FileNotFoundError:
            raise AuthenticationError(f"AWS KMS tokens file not found: {filepath}")
        except IOError as e:
            raise AuthenticationError(f"Failed to read AWS KMS tokens file '{filepath}': {str(e)}")


    def _decrypt_token(self, encrypted_token: str) -> str:
        """Decrypt a single token using AWS KMS."""
        try:
            import base64
            from botocore.exceptions import ClientError

            # Decode base64 to get the actual ciphertext blob
            ciphertext_blob = base64.b64decode(encrypted_token.encode('utf-8'))

            response = self.kms_client.decrypt(CiphertextBlob=ciphertext_blob)
            return response['Plaintext'].decode('utf-8')
        except ClientError as e:
            raise AuthenticationError(f"AWS KMS decryption failed: {str(e)}")
        except Exception as e:
            raise AuthenticationError(f"Failed to decrypt with AWS KMS: {str(e)}")


class GCPSecretsAuth(AuthenticationProvider):
    """Authentication using GCP Secret Manager."""

    def __init__(self, project_id: str, secret_name: str, version: str = "latest", cache_ttl: int = DEFAULT_CACHE_TTL, failed_response: Optional[Dict[str, Any]] = None):
        """Initialize GCP Secrets authentication.

        Args:
            project_id: GCP project ID.
            secret_name: Name of the secret in GCP Secret Manager.
            version: Version of the secret to use.
            cache_ttl: How long to cache the secret values (seconds).
            failed_response: Custom response for failed authentication.

        Raises:
            AuthenticationError: If GCP setup fails.
        """
        try:
            from google.cloud import secretmanager
            from google.auth.exceptions import DefaultCredentialsError
        except ImportError:
            raise AuthenticationError("google-cloud-secret-manager package is required for GCP Secrets authentication")

        super().__init__(failed_response)

        self.project_id = project_id
        self.secret_name = secret_name
        self.version = version
        self.cache_ttl = cache_ttl

        try:
            self.client = secretmanager.SecretManagerServiceClient()
            # Test credentials by listing secrets
            parent = f"projects/{project_id}"
            request = secretmanager.ListSecretsRequest(parent=parent, page_size=1)
            self.client.list_secrets(request=request)
        except DefaultCredentialsError:
            raise AuthenticationError("GCP credentials not found")
        except Exception as e:
            raise AuthenticationError(f"Failed to initialize GCP Secret Manager client: {str(e)}")

        # Cache for secret values
        self._cached_values = set()
        self._cache_time = 0
        self.expected_values = self._get_cached_tokens()


    def _get_cached_tokens(self) -> set:
        """Get authentication tokens from cache or refresh from GCP."""
        current_time = time.time()

        # Check if cache is still valid
        if current_time - self._cache_time < self.cache_ttl and self._cached_values:
            return self._cached_values

        # Refresh cache
        try:
            from google.cloud import secretmanager

            name = f"projects/{self.project_id}/secrets/{self.secret_name}/versions/{self.version}"
            response = self.client.access_secret_version(request={"name": name})
            secret_data = response.payload.data.decode("UTF-8")

            # Parse secret data
            try:
                # Try JSON format first
                parsed_data = json.loads(secret_data)
                if isinstance(parsed_data, list):
                    self._cached_values = {str(v) for v in parsed_data}
                elif isinstance(parsed_data, str):
                    self._cached_values = {str(parsed_data)}
                elif isinstance(parsed_data, dict):
                    self._cached_values = {str(v) for v in parsed_data.values()}
                else:
                    raise AuthenticationError(f"Invalid secret format: {type(parsed_data)}")
            except json.JSONDecodeError:
                # Treat as plain text
                self._cached_values = {str(secret_data.strip())}

            self._cache_time = current_time
            return self._cached_values

        except Exception as e:
            raise AuthenticationError(f"Failed to retrieve GCP secret '{self.secret_name}': {str(e)}")


class AuthenticationError(Exception):
    """Exception raised for authentication errors."""
    pass


def create_authentication_provider(config: Dict[str, Any]) -> AuthenticationProvider:
    """Create an authentication provider from configuration.

    Args:
        config: Authentication configuration dictionary.

    Returns:
        Configured authentication provider.

    Raises:
        AuthenticationError: If configuration is invalid or provider cannot be created.
    """
    # Get common settings
    failed_response = config.get("failed_response")
    encryption_config = config.get("encryption")
    encrypted = config.get("encrypted", True)

    # Identify authentication method keys
    method_keys = []
    if "value" in config:
        method_keys.append("value")
    if "values" in config:
        method_keys.append("values")
    if "filepath" in config:
        method_keys.append("filepath")
    if "aws_secret_name" in config:
        method_keys.append("aws_secret_name")
    if "aws_key_id" in config:
        method_keys.append("aws_key_id")
    if "aws_tokens_file" in config:
        method_keys.append("aws_tokens_file")
    if "gcp_project_id" in config:
        method_keys.append("gcp_project_id")

    # Validate exactly one method key is present
    if len(method_keys) == 0:
        raise AuthenticationError("Authentication configuration must specify exactly one of: value, values, filepath, aws_secret_name, aws_key_id, aws_tokens_file, or gcp_project_id")
    elif len(method_keys) > 1:
        raise AuthenticationError(f"Authentication configuration has conflicting method keys: {', '.join(method_keys)}. Only one is allowed.")

    method_key = method_keys[0]

    # Create provider based on the method key
    if method_key == "value":
        value = config.get("value")
        return FixedValueAuth(value, encrypted, encryption_config, failed_response)

    elif method_key == "values":
        values = config.get("values")
        if not isinstance(values, list):
            raise AuthenticationError("'values' must be a list")
        return ListValueAuth(values, encrypted, encryption_config, failed_response)

    elif method_key == "filepath":
        filepath = config.get("filepath")
        return FileAuth(filepath, encrypted, encryption_config, failed_response)

    elif method_key == "aws_secret_name":
        secret_name = config.get("aws_secret_name")
        region = config.get("aws_region", "us-west-2")
        cache_ttl = config.get("refresh_interval", DEFAULT_CACHE_TTL)
        return AWSSecretsAuth(secret_name, region, cache_ttl, failed_response)

    elif method_key == "aws_key_id":
        aws_key_id = config.get("aws_key_id")
        tokens = config.get("aws_tokens")
        if not tokens or not isinstance(tokens, list):
            raise AuthenticationError("AWS KMS authentication requires both 'aws_key_id' and 'aws_tokens' list")

        region = config.get("aws_region", "us-west-2")
        return AWSKMSAuth(tokens=tokens, aws_key_id=aws_key_id, aws_region=region, failed_response=failed_response)

    elif method_key == "aws_tokens_file":
        aws_key_id = config.get("aws_key_id")
        if not aws_key_id:
            raise AuthenticationError("AWS KMS file authentication requires both 'aws_tokens_file' and 'aws_key_id'")

        aws_tokens_file = config.get("aws_tokens_file")
        region = config.get("aws_region", "us-west-2")
        return AWSKMSAuth(aws_tokens_file=aws_tokens_file, aws_key_id=aws_key_id, aws_region=region, failed_response=failed_response)

    elif method_key == "gcp_project_id":
        project_id = config.get("gcp_project_id")
        secret_name = config.get("gcp_secret_name")
        if not secret_name:
            raise AuthenticationError("GCP Secrets authentication requires both 'gcp_project_id' and 'gcp_secret_name'")

        version = config.get("gcp_version", "latest")
        cache_ttl = config.get("refresh_interval", DEFAULT_CACHE_TTL)
        return GCPSecretsAuth(project_id, secret_name, version, cache_ttl, failed_response)

    else:
        raise AuthenticationError(f"Unknown authentication method key: {method_key}")


def validate_authentication(cookies: Dict[str, str], auth_config: Dict[str, Any]) -> Tuple[bool, Optional[int], Optional[Any]]:
    """Validate authentication using cookies and configuration.

    Args:
        cookies: Dictionary of cookie values from the request.
        auth_config: Authentication configuration dictionary.

    Returns:
        Tuple of (is_valid, status_code, response_body).
        If is_valid is True, status_code and response_body are None.
        If is_valid is False, status_code and response_body contain the error response.

    Raises:
        AuthenticationError: If authentication configuration is invalid.
    """
    try:
        # Get the authentication key name
        auth_key = auth_config.get("key")
        if not auth_key:
            raise AuthenticationError("Authentication configuration missing 'key' field")
        # Get the token from cookies
        auth_token = cookies.get(auth_key)
        if not auth_token:
            # Token not provided - create provider to get proper error response
            provider = create_authentication_provider(auth_config)
            status_code, response_body = provider.get_failed_response()
            return (False, status_code, response_body)

        # Create authentication provider and validate
        provider = create_authentication_provider(auth_config)
        is_valid = provider.validate(auth_token)

        if is_valid:
            return (True, None, None)
        else:
            status_code, response_body = provider.get_failed_response()
            return (False, status_code, response_body)
    except AuthenticationError:
        raise
    except Exception as e:
        raise AuthenticationError(f"Authentication validation failed: {str(e)}")
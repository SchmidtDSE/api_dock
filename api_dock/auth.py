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

    @abstractmethod
    def validate(self, token: str) -> bool:
        """Validate an authentication token.

        Args:
            token: The authentication token to validate.

        Returns:
            True if token is valid, False otherwise.
        """
        pass

    @abstractmethod
    def get_failed_response(self) -> Tuple[int, Any]:
        """Get the response to return when authentication fails.

        Returns:
            Tuple of (status_code, response_body).
        """
        pass


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
        self.encrypted = encrypted
        self.encryption_config = encryption_config
        self.failed_response_config = failed_response or {}

        try:
            self.expected_value = decrypt_value_if_needed(value, encrypted, encryption_config)
        except EncryptionError as e:
            raise AuthenticationError(f"Failed to decrypt authentication value: {str(e)}")

    def validate(self, token: str) -> bool:
        """Validate token against fixed value."""
        return token == self.expected_value

    def get_failed_response(self) -> Tuple[int, Any]:
        """Get failed authentication response."""
        status_code = self.failed_response_config.get("status", DEFAULT_STATUS_CODE)

        # Return configured response body, or default message
        if self.failed_response_config:
            # Return the full response object including status in body
            return (status_code, self.failed_response_config)
        else:
            return (status_code, {"error": "Authentication failed"})


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
        self.encryption_config = encryption_config
        self.failed_response_config = failed_response or {}
        self.expected_values = set()

        for value_item in values:
            try:
                # Handle both string and dict formats
                if isinstance(value_item, str):
                    # Simple string - use global encrypted setting
                    decrypted = decrypt_value_if_needed(value_item, encrypted, encryption_config)
                    self.expected_values.add(decrypted)
                elif isinstance(value_item, dict):
                    # Dict format with individual encryption setting
                    value_str = value_item.get("value", "")
                    value_encrypted = value_item.get("encrypted", encrypted)
                    decrypted = decrypt_value_if_needed(value_str, value_encrypted, encryption_config)
                    self.expected_values.add(decrypted)
                else:
                    raise AuthenticationError(f"Invalid value format: {type(value_item)}")
            except EncryptionError as e:
                raise AuthenticationError(f"Failed to decrypt authentication value: {str(e)}")

    def validate(self, token: str) -> bool:
        """Validate token against list of values."""
        return token in self.expected_values

    def get_failed_response(self) -> Tuple[int, Any]:
        """Get failed authentication response."""
        status_code = self.failed_response_config.get("status", DEFAULT_STATUS_CODE)

        # Return configured response body, or default message
        if self.failed_response_config:
            # Return the full response object including status in body
            return (status_code, self.failed_response_config)
        else:
            return (status_code, {"error": "Authentication failed"})


class AWSSecretsAuth(AuthenticationProvider):
    """Authentication using AWS Secrets Manager."""

    def __init__(self, secret_name: str, region: str = "us-east-1", cache_ttl: int = DEFAULT_CACHE_TTL, failed_response: Optional[Dict[str, Any]] = None):
        """Initialize AWS Secrets authentication.

        Args:
            secret_name: Name of the secret in AWS Secrets Manager.
            region: AWS region.
            cache_ttl: How long to cache the secret values (seconds).
            failed_response: Custom response for failed authentication.

        Raises:
            AuthenticationError: If AWS setup fails.
        """
        try:
            import boto3
            from botocore.exceptions import ClientError, NoCredentialsError
        except ImportError:
            raise AuthenticationError("boto3 package is required for AWS Secrets authentication")

        self.secret_name = secret_name
        self.region = region
        self.cache_ttl = cache_ttl
        self.failed_response_config = failed_response or {}

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

    def validate(self, token: str) -> bool:
        """Validate token against AWS Secrets Manager."""
        try:
            valid_tokens = self._get_cached_tokens()
            return token in valid_tokens
        except Exception:
            # If we can't fetch secrets, deny access
            return False

    def get_failed_response(self) -> Tuple[int, Any]:
        """Get failed authentication response."""
        status_code = self.failed_response_config.get("status", DEFAULT_STATUS_CODE)

        # Return configured response body, or default message
        if self.failed_response_config:
            # Return the full response object including status in body
            return (status_code, self.failed_response_config)
        else:
            return (status_code, {"error": "Authentication failed"})

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

            response = self.secrets_client.get_secret_value(SecretName=self.secret_name)
            secret_data = response['SecretString']

            # Parse secret data
            try:
                # Try JSON format first (list of tokens or single value)
                parsed_data = json.loads(secret_data)
                if isinstance(parsed_data, list):
                    self._cached_values = set(parsed_data)
                elif isinstance(parsed_data, str):
                    self._cached_values = {parsed_data}
                elif isinstance(parsed_data, dict):
                    # Extract values from dict (common AWS pattern)
                    self._cached_values = set(parsed_data.values())
                else:
                    raise AuthenticationError(f"Invalid secret format: {type(parsed_data)}")
            except json.JSONDecodeError:
                # Treat as plain text (single value)
                self._cached_values = {secret_data.strip()}

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

        self.project_id = project_id
        self.secret_name = secret_name
        self.version = version
        self.cache_ttl = cache_ttl
        self.failed_response_config = failed_response or {}

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

    def validate(self, token: str) -> bool:
        """Validate token against GCP Secret Manager."""
        try:
            valid_tokens = self._get_cached_tokens()
            return token in valid_tokens
        except Exception:
            # If we can't fetch secrets, deny access
            return False

    def get_failed_response(self) -> Tuple[int, Any]:
        """Get failed authentication response."""
        status_code = self.failed_response_config.get("status", DEFAULT_STATUS_CODE)

        # Return configured response body, or default message
        if self.failed_response_config:
            # Return the full response object including status in body
            return (status_code, self.failed_response_config)
        else:
            return (status_code, {"error": "Authentication failed"})

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
                    self._cached_values = set(parsed_data)
                elif isinstance(parsed_data, str):
                    self._cached_values = {parsed_data}
                elif isinstance(parsed_data, dict):
                    self._cached_values = set(parsed_data.values())
                else:
                    raise AuthenticationError(f"Invalid secret format: {type(parsed_data)}")
            except json.JSONDecodeError:
                # Treat as plain text
                self._cached_values = {secret_data.strip()}

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
    method = config.get("method")
    if not method:
        raise AuthenticationError("Authentication method not specified")

    # Get common settings
    failed_response = config.get("failed_response")
    encryption_config = config.get("encryption")
    encrypted = config.get("encrypted", True)

    if method == "fixed":
        value = config.get("value")
        if not value:
            raise AuthenticationError("Fixed authentication method requires 'value'")

        return FixedValueAuth(value, encrypted, encryption_config, failed_response)

    elif method == "list":
        values = config.get("values")
        if not values or not isinstance(values, list):
            raise AuthenticationError("List authentication method requires 'values' list")

        return ListValueAuth(values, encrypted, encryption_config, failed_response)

    elif method == "aws_secrets":
        secret_name = config.get("secret_name")
        if not secret_name:
            raise AuthenticationError("AWS Secrets method requires 'secret_name'")

        region = config.get("region", "us-east-1")
        cache_ttl = config.get("refresh_interval", DEFAULT_CACHE_TTL)

        return AWSSecretsAuth(secret_name, region, cache_ttl, failed_response)

    elif method == "gcp_secrets":
        project_id = config.get("project_id")
        secret_name = config.get("secret_name")

        if not project_id or not secret_name:
            raise AuthenticationError("GCP Secrets method requires 'project_id' and 'secret_name'")

        version = config.get("version", "latest")
        cache_ttl = config.get("refresh_interval", DEFAULT_CACHE_TTL)

        return GCPSecretsAuth(project_id, secret_name, version, cache_ttl, failed_response)

    else:
        raise AuthenticationError(f"Unknown authentication method: {method}")


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

        # DEBUG: Print authentication debug info
        print(f"🔐 DEBUG AUTH: Expected auth key: '{auth_key}'")
        print(f"🍪 DEBUG AUTH: Received cookies: {list(cookies.keys())}")
        print(f"🔑 DEBUG AUTH: Cookie values: {cookies}")

        # Get the token from cookies
        auth_token = cookies.get(auth_key)
        if not auth_token:
            print(f"❌ DEBUG AUTH: Auth token not found in cookies (looking for key: '{auth_key}')")
            # Token not provided - create provider to get proper error response
            provider = create_authentication_provider(auth_config)
            status_code, response_body = provider.get_failed_response()
            return (False, status_code, response_body)

        print(f"✅ DEBUG AUTH: Found auth token: '{auth_token[:10]}...' (truncated)")

        # Create authentication provider and validate
        provider = create_authentication_provider(auth_config)
        is_valid = provider.validate(auth_token)

        print(f"🔍 DEBUG AUTH: Token validation result: {is_valid}")

        if is_valid:
            return (True, None, None)
        else:
            status_code, response_body = provider.get_failed_response()
            print(f"💥 DEBUG AUTH: Failed response - status: {status_code}, body: {response_body}")
            return (False, status_code, response_body)

    except AuthenticationError:
        # Re-raise authentication errors
        raise
    except Exception as e:
        # Wrap other exceptions
        raise AuthenticationError(f"Authentication validation failed: {str(e)}")
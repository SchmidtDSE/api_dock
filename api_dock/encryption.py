"""

Encryption Module for API Dock

Provides encryption/decryption capabilities for authentication tokens and sensitive data.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import base64
import os
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

try:
    from cryptography.fernet import Fernet
except ImportError:
    Fernet = None

try:
    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError
except ImportError:
    boto3 = None

try:
    from google.cloud import secretmanager
    from google.auth.exceptions import DefaultCredentialsError
except ImportError:
    secretmanager = None


#
# CONSTANTS
#
DEFAULT_KEY_FILE = ".api_dock_key"
DEFAULT_ENV_KEY = "API_DOCK_ENCRYPTION_KEY"


#
# PUBLIC
#
class EncryptionProvider(ABC):
    """Abstract base class for encryption providers."""

    @abstractmethod
    def encrypt(self, plaintext: str) -> str:
        """Encrypt a plaintext string.

        Args:
            plaintext: The string to encrypt.

        Returns:
            Encrypted string (base64 encoded).

        Raises:
            EncryptionError: If encryption fails.
        """
        pass

    @abstractmethod
    def decrypt(self, ciphertext: str) -> str:
        """Decrypt a ciphertext string.

        Args:
            ciphertext: The encrypted string to decrypt (base64 encoded).

        Returns:
            Decrypted plaintext string.

        Raises:
            EncryptionError: If decryption fails.
        """
        pass


class LocalKeyEncryption(EncryptionProvider):
    """Local key encryption using Fernet symmetric encryption."""

    def __init__(self, key_source: Optional[str] = None):
        """Initialize with key from file or environment variable.

        Args:
            key_source: Path to key file or environment variable name.
                       If None, uses default key file.

        Raises:
            EncryptionError: If cryptography package not available or key cannot be loaded.
        """
        if Fernet is None:
            raise EncryptionError("cryptography package is required for local key encryption")

        self.key_source = key_source or DEFAULT_KEY_FILE
        self._fernet = self._load_key()

    def encrypt(self, plaintext: str) -> str:
        """Encrypt plaintext using Fernet."""
        try:
            encrypted_bytes = self._fernet.encrypt(plaintext.encode('utf-8'))
            return base64.b64encode(encrypted_bytes).decode('utf-8')
        except Exception as e:
            raise EncryptionError(f"Failed to encrypt data: {str(e)}")

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt ciphertext using Fernet."""
        try:
            encrypted_bytes = base64.b64decode(ciphertext.encode('utf-8'))
            decrypted_bytes = self._fernet.decrypt(encrypted_bytes)
            return decrypted_bytes.decode('utf-8')
        except Exception as e:
            raise EncryptionError(f"Failed to decrypt data: {str(e)}")

    def _load_key(self) -> 'Fernet':
        """Load encryption key from file or environment."""
        key_data = None

        # Try as file path first
        if os.path.isfile(self.key_source):
            try:
                with open(self.key_source, 'rb') as f:
                    key_data = f.read()
            except Exception as e:
                raise EncryptionError(f"Failed to read key file '{self.key_source}': {str(e)}")

        # Try as environment variable
        elif self.key_source in os.environ:
            key_data = os.environ[self.key_source].encode('utf-8')

        # Try default environment variable
        elif DEFAULT_ENV_KEY in os.environ:
            key_data = os.environ[DEFAULT_ENV_KEY].encode('utf-8')

        else:
            raise EncryptionError(f"Encryption key not found: {self.key_source}")

        try:
            return Fernet(key_data)
        except Exception as e:
            raise EncryptionError(f"Invalid encryption key: {str(e)}")

    @staticmethod
    def generate_key() -> bytes:
        """Generate a new Fernet key.

        Returns:
            32-byte base64-encoded key.

        Raises:
            EncryptionError: If cryptography package not available.
        """
        if Fernet is None:
            raise EncryptionError("cryptography package is required for key generation")

        return Fernet.generate_key()


class EnvKeyEncryption(LocalKeyEncryption):
    """Environment variable encryption - alias for LocalKeyEncryption."""

    def __init__(self, env_var: str = DEFAULT_ENV_KEY):
        """Initialize with environment variable.

        Args:
            env_var: Name of environment variable containing the key.
        """
        super().__init__(env_var)


class AWSKMSEncryption(EncryptionProvider):
    """AWS KMS encryption provider."""

    def __init__(self, key_id: str, region: str = "us-east-1"):
        """Initialize AWS KMS encryption.

        Args:
            key_id: AWS KMS key ID or ARN.
            region: AWS region.

        Raises:
            EncryptionError: If boto3 not available or AWS credentials not found.
        """
        if boto3 is None:
            raise EncryptionError("boto3 package is required for AWS KMS encryption")

        self.key_id = key_id
        self.region = region

        try:
            self.kms_client = boto3.client('kms', region_name=region)
            # Test credentials by listing keys (this will fail if no permissions)
            self.kms_client.list_keys(Limit=1)
        except NoCredentialsError:
            raise EncryptionError("AWS credentials not found")
        except Exception as e:
            raise EncryptionError(f"Failed to initialize AWS KMS client: {str(e)}")

    def encrypt(self, plaintext: str) -> str:
        """Encrypt using AWS KMS."""
        try:
            response = self.kms_client.encrypt(
                KeyId=self.key_id,
                Plaintext=plaintext.encode('utf-8')
            )
            # KMS returns the ciphertext as bytes, encode to base64
            return base64.b64encode(response['CiphertextBlob']).decode('utf-8')
        except ClientError as e:
            raise EncryptionError(f"AWS KMS encryption failed: {str(e)}")
        except Exception as e:
            raise EncryptionError(f"Failed to encrypt with AWS KMS: {str(e)}")

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt using AWS KMS."""
        try:
            # Decode base64 to get the actual ciphertext blob
            ciphertext_blob = base64.b64decode(ciphertext.encode('utf-8'))

            response = self.kms_client.decrypt(CiphertextBlob=ciphertext_blob)
            return response['Plaintext'].decode('utf-8')
        except ClientError as e:
            raise EncryptionError(f"AWS KMS decryption failed: {str(e)}")
        except Exception as e:
            raise EncryptionError(f"Failed to decrypt with AWS KMS: {str(e)}")


class GCPSecretsEncryption(EncryptionProvider):
    """GCP Secret Manager encryption provider (stores encrypted values as secrets)."""

    def __init__(self, project_id: str):
        """Initialize GCP Secret Manager encryption.

        Args:
            project_id: GCP project ID.

        Raises:
            EncryptionError: If google-cloud-secret-manager not available or credentials not found.
        """
        if secretmanager is None:
            raise EncryptionError("google-cloud-secret-manager package is required for GCP encryption")

        self.project_id = project_id

        try:
            self.client = secretmanager.SecretManagerServiceClient()
            # Test credentials by listing secrets
            parent = f"projects/{project_id}"
            request = secretmanager.ListSecretsRequest(parent=parent, page_size=1)
            self.client.list_secrets(request=request)
        except DefaultCredentialsError:
            raise EncryptionError("GCP credentials not found")
        except Exception as e:
            raise EncryptionError(f"Failed to initialize GCP Secret Manager client: {str(e)}")

    def encrypt(self, plaintext: str) -> str:
        """Store value as secret and return secret name (not real encryption)."""
        raise EncryptionError("GCP Secret Manager encryption not implemented - use for secret retrieval only")

    def decrypt(self, secret_name: str) -> str:
        """Retrieve value from secret."""
        try:
            name = f"projects/{self.project_id}/secrets/{secret_name}/versions/latest"
            response = self.client.access_secret_version(request={"name": name})
            return response.payload.data.decode("UTF-8")
        except Exception as e:
            raise EncryptionError(f"Failed to retrieve GCP secret '{secret_name}': {str(e)}")


class EncryptionError(Exception):
    """Exception raised for encryption/decryption errors."""
    pass


def create_encryption_provider(config: Dict[str, Any]) -> EncryptionProvider:
    """Create an encryption provider from configuration.

    Args:
        config: Encryption configuration dictionary.

    Returns:
        Configured encryption provider.

    Raises:
        EncryptionError: If configuration is invalid or provider cannot be created.
    """
    method = config.get("method")
    if not method:
        raise EncryptionError("Encryption method not specified")

    if method == "local_key":
        key_file = config.get("key_file", DEFAULT_KEY_FILE)
        return LocalKeyEncryption(key_file)

    elif method == "env_key":
        env_var = config.get("key_env", DEFAULT_ENV_KEY)
        return EnvKeyEncryption(env_var)

    elif method == "aws_kms":
        key_id = config.get("key_id")
        if not key_id:
            raise EncryptionError("AWS KMS key_id is required")

        region = config.get("region", "us-east-1")
        return AWSKMSEncryption(key_id, region)

    elif method == "gcp_secrets":
        project_id = config.get("project_id")
        if not project_id:
            raise EncryptionError("GCP project_id is required")

        return GCPSecretsEncryption(project_id)

    else:
        raise EncryptionError(f"Unknown encryption method: {method}")


def decrypt_value_if_needed(value: str, encrypted: bool = True, encryption_config: Optional[Dict[str, Any]] = None) -> str:
    """Decrypt a value if it's marked as encrypted.

    Args:
        value: The value to decrypt (if encrypted) or return as-is (if plaintext).
        encrypted: Whether the value is encrypted.
        encryption_config: Encryption configuration for creating the provider.

    Returns:
        Decrypted value if encrypted=True, original value if encrypted=False.

    Raises:
        EncryptionError: If value is encrypted but cannot be decrypted.
    """
    if not encrypted:
        return value

    if encryption_config is None:
        # Try local key encryption by default
        encryption_config = {"method": "local_key"}

    provider = create_encryption_provider(encryption_config)
    return provider.decrypt(value)
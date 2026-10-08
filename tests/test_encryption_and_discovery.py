"""

Tests for encryption (local and environment keys) and config discovery

Covers encrypting and decrypting with a key file or an environment variable,
where keys are looked up (an explicit key file must exist; only the default
key file falls back to ``API_DOCK_ENCRYPTION_KEY``), ``decrypt_value_if_needed``,
encrypted authentication values end to end, and ``find_config`` (local
config first, then the bundled example).

License: BSD 3-Clause

"""
#
# IMPORTS
#
from pathlib import Path

import pytest

from api_dock import config_discovery
from api_dock.auth import validate_authentication
from api_dock.encryption import (
    create_encryption_provider,
    decrypt_value_if_needed,
    DEFAULT_ENV_KEY,
    EncryptionError,
    EnvKeyEncryption,
    LocalKeyEncryption,
)


#
# PUBLIC
#
class TestLocalKey:
    """Fernet encryption with a key file."""

    def test_round_trip(self, tmp_path: Path) -> None:
        key_file = _key_file(tmp_path)
        provider = create_encryption_provider({"method": "local_key", "key_file": str(key_file)})
        ciphertext = provider.encrypt("s3cret")
        assert ciphertext != "s3cret" and provider.decrypt(ciphertext) == "s3cret"

    def test_explicit_missing_file_is_an_error(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(DEFAULT_ENV_KEY, LocalKeyEncryption.generate_key().decode())
        with pytest.raises(EncryptionError, match="key file not found"):
            LocalKeyEncryption(str(tmp_path / "typo.key"))

    def test_default_file_falls_back_to_env(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv(DEFAULT_ENV_KEY, LocalKeyEncryption.generate_key().decode())
        provider = LocalKeyEncryption()
        assert provider.decrypt(provider.encrypt("x")) == "x"

    def test_wrong_key_fails(self, tmp_path: Path) -> None:
        ciphertext = LocalKeyEncryption(str(_key_file(tmp_path, "a.key"))).encrypt("x")
        with pytest.raises(EncryptionError, match="Failed to decrypt"):
            LocalKeyEncryption(str(_key_file(tmp_path, "b.key"))).decrypt(ciphertext)

    def test_invalid_key(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.key"
        bad.write_text("not-a-key")
        with pytest.raises(EncryptionError, match="Invalid encryption key"):
            LocalKeyEncryption(str(bad))


class TestEnvKey:
    """Fernet encryption with a key in an environment variable."""

    def test_round_trip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_KEY", LocalKeyEncryption.generate_key().decode())
        provider = create_encryption_provider({"method": "env_key", "key_env": "MY_KEY"})
        assert provider.decrypt(provider.encrypt("s3cret")) == "s3cret"

    def test_ignores_a_file_named_like_the_variable(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        _key_file(tmp_path, "MY_KEY")
        monkeypatch.delenv("MY_KEY", raising=False)
        with pytest.raises(EncryptionError, match="not set"):
            EnvKeyEncryption("MY_KEY")


class TestDecryptValueIfNeeded:
    """Values are decrypted only when marked encrypted."""

    def test_plain_value(self) -> None:
        assert decrypt_value_if_needed("plain", encrypted=False) == "plain"

    def test_encrypted_value(self, tmp_path: Path) -> None:
        key_file = _key_file(tmp_path)
        config = {"method": "local_key", "key_file": str(key_file)}
        ciphertext = create_encryption_provider(config).encrypt("token")
        assert decrypt_value_if_needed(ciphertext, True, config) == "token"

    def test_unknown_method(self) -> None:
        with pytest.raises(EncryptionError):
            create_encryption_provider({"method": "rot13"})

    def test_encrypted_authentication_value(self, tmp_path: Path) -> None:
        config = {"method": "local_key", "key_file": str(_key_file(tmp_path))}
        ciphertext = create_encryption_provider(config).encrypt("api-token")
        auth = {"key": "k", "value": ciphertext, "encryption": config}
        assert validate_authentication({"k": "api-token"}, auth)[0]
        assert not validate_authentication({"k": ciphertext}, auth)[0]


class TestFindConfig:
    """Config discovery: local folder first, then the bundled example."""

    def test_local_first(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "api_dock_config").mkdir()
        (tmp_path / "api_dock_config" / "config.yaml").write_text("name: local\n")
        assert config_discovery.find_config() == "api_dock_config/config.yaml"
        assert config_discovery.find_config("config.yaml") == "api_dock_config/config.yaml"

    def test_bundled_example(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        found = config_discovery.find_config()
        assert found is not None and found.endswith("example_api_dock_config/config.yaml")

    def test_not_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        assert config_discovery.find_config("no_such_config") is None


#
# INTERNAL
#
def _key_file(root: Path, name: str = "test.key") -> Path:
    path = root / name
    path.write_bytes(LocalKeyEncryption.generate_key())
    return path

"""

Tests for authentication: providers, caching, refresh and method selection

Covers the fixed/list/file providers, constant-time token comparison, the
provider cache (built once per auth config, rebuilt when the config or its
token file changes), secret-store refresh with ``refresh_interval`` (a fake
Secrets Manager client stands in for AWS), the KMS method keys
(``aws_tokens`` / ``aws_tokens_file``, with ``aws_key_id`` optional), and auth
on database routes end to end.

License: BSD 3-Clause

"""
#
# IMPORTS
#
import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict, List

import duckdb
import pytest
import yaml

from api_dock import auth
from api_dock.auth import (
    AuthenticationError,
    AWSSecretsAuth,
    create_authentication_provider,
    FileAuth,
    FixedValueAuth,
    get_authentication_provider,
    ListValueAuth,
    validate_authentication,
)
from api_dock.route_mapper import RouteMapper


#
# PUBLIC
#
class TestProviders:
    """The plain-value providers."""

    def test_fixed_value(self) -> None:
        provider = FixedValueAuth("s3cret", encrypted=False)
        assert provider.validate("s3cret") and not provider.validate("nope")

    def test_list_values(self) -> None:
        provider = ListValueAuth(["a", 7, {"value": "c", "encrypted": False}], encrypted=False)
        assert all(provider.validate(v) for v in ("a", "7", 7, "c"))
        assert not provider.validate("d")

    def test_file(self, tmp_path: Path) -> None:
        path = tmp_path / "tokens.txt"
        path.write_text("# comment\none\n\ntwo\n")
        provider = FileAuth(str(path), encrypted=False)
        assert provider.validate("two") and not provider.validate("# comment")

    def test_constant_time_comparison(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: List[bytes] = []
        real = auth.hmac.compare_digest

        def counting(a: bytes, b: bytes) -> bool:
            calls.append(b)
            return real(a, b)
        monkeypatch.setattr(auth.hmac, "compare_digest", counting)
        provider = ListValueAuth(["a", "b", "c"], encrypted=False)
        assert provider.validate("a")
        assert len(calls) == 3  # every value is compared, even after a match


class TestProviderCache:
    """Providers are built once per auth config."""

    def test_reused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        built = _count_builds(monkeypatch)
        config = {"key": "k", "value": "v1", "encrypted": False}
        for _ in range(5):
            assert validate_authentication({"k": "v1"}, dict(config))[0]
        validate_authentication({}, dict(config))
        assert built == [1]

    def test_changed_config_rebuilds(self) -> None:
        first = get_authentication_provider({"key": "k", "value": "x1", "encrypted": False})
        second = get_authentication_provider({"key": "k", "value": "x2", "encrypted": False})
        assert first is not second and second.validate("x2")

    def test_changed_token_file_rebuilds(self, tmp_path: Path) -> None:
        path = tmp_path / "tokens.txt"
        path.write_text("old\n")
        config = {"key": "k", "filepath": str(path), "encrypted": False}
        assert validate_authentication({"k": "old"}, config)[0]
        path.write_text("new\n")
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        assert validate_authentication({"k": "new"}, config)[0]
        assert not validate_authentication({"k": "old"}, config)[0]


class TestRefresh:
    """Secret-store values are re-read after refresh_interval."""

    def test_values_refresh(self) -> None:
        client = FakeSecrets(["first"])
        provider = _secrets_provider(client, ttl=60)
        assert provider.validate("first")
        client.values = ["second"]
        assert provider.validate("first")          # still cached
        provider._cache_time -= 61                 # interval passed
        assert provider.validate("second") and not provider.validate("first")

    def test_failed_refresh_keeps_values(self, caplog: Any) -> None:
        client = FakeSecrets(["first"])
        provider = _secrets_provider(client, ttl=60)
        client.fail = True
        provider._cache_time -= 61
        with caplog.at_level("WARNING"):
            assert provider.validate("first")
        assert "using the last ones" in caplog.text
        assert client.calls == 2
        assert provider.validate("first") and client.calls == 2   # no retry until the interval

    @pytest.mark.parametrize("secret, expected", [
        ('["a", "b"]', {"a", "b"}), ('{"x": "a"}', {"a"}), ('"a"', {"a"}), ("plain", {"plain"}),
        ("42", {"42"}),
    ])
    def test_secret_formats(self, secret: str, expected: set) -> None:
        provider = _secrets_provider(FakeSecrets(raw=secret), ttl=60)
        assert provider.current_values() == expected


class TestMethodKeys:
    """Which settings choose the method."""

    def test_kms_tokens_file_with_key_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = _fake_kms(monkeypatch)
        create_authentication_provider(
            {"key": "k", "aws_tokens_file": "t.txt", "aws_key_id": "kid"}
        )
        assert captured == [{"aws_tokens_file": "t.txt", "aws_key_id": "kid",
                             "aws_region": "us-west-2", "failed_response": None}]

    def test_kms_tokens_with_and_without_key_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = _fake_kms(monkeypatch)
        create_authentication_provider({"key": "k", "aws_tokens": ["e1"], "aws_key_id": "kid"})
        create_authentication_provider(
            {"key": "k", "aws_tokens": ["e1"], "aws_region": "eu-west-1"}
        )
        assert captured[0]["tokens"] == ["e1"] and captured[0]["aws_key_id"] == "kid"
        assert captured[1]["aws_key_id"] is None and captured[1]["aws_region"] == "eu-west-1"

    @pytest.mark.parametrize("config, message", [
        ({"key": "k"}, "exactly one of"),
        ({"key": "k", "aws_key_id": "kid"}, "needs 'aws_tokens'"),
        ({"key": "k", "aws_tokens": ["e"], "aws_tokens_file": "t"}, "conflicting"),
        ({"key": "k", "value": "a", "values": ["b"]}, "conflicting"),
        ({"key": "k", "aws_tokens": "e1"}, "non-empty list"),
    ])
    def test_invalid(self, config: Dict[str, Any], message: str) -> None:
        with pytest.raises(AuthenticationError, match=message):
            create_authentication_provider(config)


class TestDatabaseRoutes:
    """Authentication on database routes, end to end."""

    @pytest.mark.parametrize("cookies, status", [
        ({"api_token": "good"}, 200), ({"api_token": "bad"}, 403), ({}, 403),
    ])
    def test_cookie_checked(self, tmp_path: Path, cookies: Dict[str, str], status: int) -> None:
        mapper = _mapper(tmp_path)
        response = asyncio.run(mapper.map_database_route("db", "r", {}, cookies))
        assert response.status_code == status
        if status == 403:
            assert json.loads(response.content) == {"status": 403, "message": "no"}

    def test_provider_built_once(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        mapper = _mapper(tmp_path, value="once-only")
        built = _count_builds(monkeypatch)
        for _ in range(4):
            asyncio.run(mapper.map_database_route("db", "r", {}, {"api_token": "once-only"}))
        assert built == [1]


class FakeSecrets:
    """A stand-in for a boto3 Secrets Manager client."""

    def __init__(self, values: List[str] = None, raw: str = None) -> None:
        self.values, self.raw, self.fail, self.calls = values, raw, False, 0

    def get_secret_value(self, SecretId: str) -> Dict[str, str]:  # noqa: N803 (boto3 API)
        self.calls += 1
        if self.fail:
            raise RuntimeError("secrets manager is down")
        return {"SecretString": self.raw if self.raw is not None else json.dumps(self.values)}


#
# INTERNAL
#
def _secrets_provider(client: FakeSecrets, ttl: int) -> AWSSecretsAuth:
    provider = AWSSecretsAuth.__new__(AWSSecretsAuth)
    auth.AuthenticationProvider.__init__(provider)
    provider.secret_name, provider.region, provider.cache_ttl = "s", "us-west-2", ttl
    provider.secrets_client = client
    provider._cached_values, provider._cache_time = set(), 0
    provider.expected_values = provider._get_cached_tokens()
    return provider


def _count_builds(monkeypatch: pytest.MonkeyPatch) -> List[int]:
    auth._PROVIDERS.clear()
    count = [0]
    real = auth.create_authentication_provider

    def counting(config: Dict[str, Any]) -> Any:
        count[0] += 1
        return real(config)
    monkeypatch.setattr(auth, "create_authentication_provider", counting)
    return count


def _fake_kms(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    captured: List[Dict[str, Any]] = []

    class FakeKMS:
        def __init__(self, **kwargs: Any) -> None:
            captured.append(kwargs)
    monkeypatch.setattr(auth, "AWSKMSAuth", FakeKMS)
    return captured


def _mapper(root: Path, value: str = "good") -> RouteMapper:
    duckdb.sql(f"COPY (SELECT 1 AS id) TO '{root / 't.parquet'}' (FORMAT parquet)")
    db = {
        "tables": {"t": str(root / "t.parquet")},
        "routes": [{"route": "r", "sql": "SELECT * FROM [[t]]"}],
        "cookies": ["api_token"],
        "authentication": {"key": "api_token", "value": value, "encrypted": False,
                           "failed_response": {"status": 403, "message": "no"}},
    }
    (root / "databases").mkdir(parents=True, exist_ok=True)
    (root / "databases" / "db.yaml").write_text(yaml.safe_dump(db))
    (root / "config.yaml").write_text(yaml.safe_dump({"name": "x", "databases": ["db"]}))
    return RouteMapper(str(root / "config.yaml"))

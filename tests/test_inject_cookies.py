"""

Tests for cookie injection via dict entries in the cookies list.

String entries in the cookies list forward named client cookies; dict entries inject
cookies into the outgoing request from a literal value or an env var.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import pytest
from unittest.mock import patch

from api_dock.config import filter_cookies_by_config, get_cookies_config, resolve_inject_cookies


#
# PUBLIC
#
class TestGetCookiesConfig:
    """get_cookies_config returns only string entries from the cookies list."""

    def test_string_entries_returned(self):
        config = {"cookies": ["session_id", "auth_token"]}
        assert get_cookies_config(config) == ["session_id", "auth_token"]

    def test_dict_entries_excluded(self):
        config = {"cookies": ["session_id", {"key": "injected", "value": "val"}]}
        assert get_cookies_config(config) == ["session_id"]

    def test_mixed_list_only_strings(self):
        config = {"cookies": [{"key": "tok", "value": "env:TOKEN"}, "name"]}
        assert get_cookies_config(config) == ["name"]


class TestResolveInjectCookies:
    """resolve_inject_cookies resolves dict entries in the cookies list."""

    def test_empty_config_returns_empty(self):
        assert resolve_inject_cookies({}) == {}

    def test_no_dict_entries_returns_empty(self):
        assert resolve_inject_cookies({"cookies": ["session_id"]}) == {}

    def test_literal_value(self):
        config = {"cookies": [{"key": "my-cookie", "value": "abc123"}]}
        assert resolve_inject_cookies(config) == {"my-cookie": "abc123"}

    def test_env_prefix_resolves_var(self):
        config = {"cookies": [{"key": "__Secure-authjs.session-token", "value": "env:MY_TOKEN"}]}
        with patch.dict("os.environ", {"MY_TOKEN": "secret-value"}):
            result = resolve_inject_cookies(config)
        assert result == {"__Secure-authjs.session-token": "secret-value"}

    def test_env_prefix_missing_var_returns_empty_string(self):
        config = {"cookies": [{"key": "tok", "value": "env:DOES_NOT_EXIST_XYZ"}]}
        with patch.dict("os.environ", {}, clear=True):
            result = resolve_inject_cookies(config)
        assert result == {"tok": ""}

    def test_shorthand_no_value_uses_key_as_env_var(self):
        """key with no value is equivalent to value: env:<key>."""
        config = {"cookies": [{"key": "MY_SESSION_TOKEN"}]}
        with patch.dict("os.environ", {"MY_SESSION_TOKEN": "tok123"}):
            result = resolve_inject_cookies(config)
        assert result == {"MY_SESSION_TOKEN": "tok123"}

    def test_shorthand_equivalence(self):
        explicit = {"cookies": [{"key": "MY_VAR", "value": "env:MY_VAR"}]}
        shorthand = {"cookies": [{"key": "MY_VAR"}]}
        with patch.dict("os.environ", {"MY_VAR": "resolved"}):
            assert resolve_inject_cookies(explicit) == resolve_inject_cookies(shorthand)

    def test_non_list_cookies_returns_empty(self):
        assert resolve_inject_cookies({"cookies": True}) == {}

    def test_string_entries_ignored(self):
        config = {"cookies": ["plain-string", {"key": "real", "value": "v"}]}
        assert resolve_inject_cookies(config) == {"real": "v"}


class TestFilterCookiesWithInjection:
    """filter_cookies_by_config merges forwarded and injected cookies."""

    def test_dict_entry_injected_into_outgoing(self):
        config = {"cookies": [{"key": "__Secure-authjs.session-token", "value": "env:TOK"}]}
        with patch.dict("os.environ", {"TOK": "my-session"}):
            result = filter_cookies_by_config({}, config)
        assert result == {"__Secure-authjs.session-token": "my-session"}

    def test_string_and_dict_entries_combined(self):
        """String entries forward from client; dict entries inject."""
        client_cookies = {"session_id": "abc", "irrelevant": "xyz"}
        config = {
            "cookies": [
                "session_id",
                {"key": "__Secure-authjs.session-token", "value": "env:TOK"},
            ]
        }
        with patch.dict("os.environ", {"TOK": "injected-tok"}):
            result = filter_cookies_by_config(client_cookies, config)
        assert result["session_id"] == "abc"
        assert result["__Secure-authjs.session-token"] == "injected-tok"
        assert "irrelevant" not in result

    def test_injection_only_no_client_cookies_forwarded(self):
        client_cookies = {"session_id": "abc"}
        config = {"cookies": [{"key": "injected", "value": "literal"}]}
        result = filter_cookies_by_config(client_cookies, config)
        assert result == {"injected": "literal"}
        assert "session_id" not in result

    def test_injected_value_overrides_client_cookie_with_same_name(self):
        client_cookies = {"tok": "client-value"}
        config = {"cookies": ["tok", {"key": "tok", "value": "server-value"}]}
        result = filter_cookies_by_config(client_cookies, config)
        assert result["tok"] == "server-value"

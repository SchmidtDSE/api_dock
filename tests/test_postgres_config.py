"""

Tests for checking and resolving PostgreSQL database configs.

``check_database_config`` runs when the route mapper is created and needs no
PostgreSQL driver: it checks the ``backend:`` key, the ``connection:`` mapping,
resource limits, ``env:`` references and table names. When the mapper starts,
``env:`` values are read and the connection fields are turned into a libpq
connection string.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import copy
from typing import Any, Dict

import pytest

from api_dock.database_config import check_database_config, get_backend_name
from api_dock.postgres_backend import build_settings
from api_dock.postgres_config import (
    connection_options,
    pool_limits,
    resolve_connection_fields,
)


#
# CONSTANTS
#
POSTGRES_CONFIG: Dict[str, Any] = {
    "name": "shop",
    "backend": "postgres",
    "connection": {
        "host": "db.example.com",
        "dbname": "shop",
        "user": "reader",
        "password": "env:SHOP_PASSWORD",
        "sslmode": "verify-full",
    },
    "tables": {"items": "catalog.items", "notes": "notes"},
    "routes": [{"route": "items", "sql": "SELECT * FROM [[items]]"}],
}


#
# FIXTURES
#
def _config(**changes: Any) -> Dict[str, Any]:
    """Copy the PostgreSQL config and apply top-level changes.

    Args:
        **changes: Keys to set; a value of None removes the key.

    Returns:
        The changed config.
    """
    config = copy.deepcopy(POSTGRES_CONFIG)
    for key, value in changes.items():
        if value is None:
            config.pop(key, None)
        else:
            config[key] = value
    return config


def _with_connection(**changes: Any) -> Dict[str, Any]:
    """Copy the PostgreSQL config and change its connection fields.

    Args:
        **changes: Connection fields to set.

    Returns:
        The changed config.
    """
    config = _config()
    config["connection"].update(changes)
    return config


def _check_fails(config: Dict[str, Any], *expected: str) -> None:
    """Assert that a config fails the startup check with text in its message.

    Args:
        config: Database config.
        *expected: Text the error message must contain.
    """
    with pytest.raises(ValueError) as error:
        check_database_config(config)
    for text in expected:
        assert text in str(error.value)


#
# PUBLIC
#
class TestBackendKey:
    """The backend key selects DuckDB or PostgreSQL."""

    def test_no_key_means_duckdb(self) -> None:
        """A config without backend: is a DuckDB config."""
        assert get_backend_name({"name": "x"}) == "duckdb"

    def test_postgres(self) -> None:
        """backend: postgres selects PostgreSQL."""
        assert get_backend_name(POSTGRES_CONFIG) == "postgres"

    def test_unknown_backend_stops_startup(self) -> None:
        """Any other value is an error naming the value."""
        _check_fails(_config(backend="mysql"), "mysql", "duckdb", "postgres")

    def test_valid_config_passes(self) -> None:
        """The example PostgreSQL config passes the check."""
        check_database_config(_config())


class TestConnectionCheck:
    """The connection mapping is checked when the mapper is created."""

    def test_missing_connection(self) -> None:
        """backend: postgres without connection: is an error."""
        _check_fails(_config(connection=None), "connection")

    def test_connection_not_a_mapping(self) -> None:
        """connection: must be a mapping of fields."""
        _check_fails(_config(connection="host=db"), "connection", "mapping")

    @pytest.mark.parametrize(
        "key", ["options", "default_transaction_read_only", "statement_timeout"])
    def test_settings_api_dock_owns_are_refused(self, key: str) -> None:
        """Fields api_dock sets on every connection can't be set in the config."""
        _check_fails(_with_connection(**{key: "x"}), key)

    @pytest.mark.parametrize("value", [None, True, ["a"], {"a": 1}])
    def test_field_values_are_text_or_numbers(self, value: Any) -> None:
        """A connection value must be a string or an integer."""
        _check_fails(_with_connection(host=value), "host")

    @pytest.mark.parametrize("value", ["env:", "env:1ABC", "env:A-B"])
    def test_bad_env_reference(self, value: str) -> None:
        """env: must be followed by an environment variable name."""
        _check_fails(_with_connection(password=value), "password", "env:")

    @pytest.mark.parametrize("value", [0, -1, True, 1.5, "5"])
    def test_bad_connect_timeout(self, value: Any) -> None:
        """connect_timeout must be a positive integer."""
        _check_fails(_with_connection(connect_timeout=value), "connect_timeout")

    def test_connect_timeout_from_env_is_checked_later(self) -> None:
        """An env: connect_timeout passes the startup check; its value is checked at start."""
        check_database_config(_with_connection(connect_timeout="env:CONNECT_TIMEOUT"))


class TestResourceLimits:
    """pool: and statement_timeout_ms: are checked and given defaults."""

    def test_defaults(self) -> None:
        """Omitted limits use their defaults."""
        assert pool_limits(_config()) == {
            "min_size": 1, "max_size": 4, "timeout": 5, "max_waiting": 16,
            "startup_timeout": 3, "statement_timeout_ms": 10000,
        }

    def test_overrides(self) -> None:
        """Given limits replace the defaults."""
        config = _config(
            pool={"min_size": 2, "max_size": 8, "timeout": 2.5, "max_waiting": 3,
                  "startup_timeout": 0.5},
            statement_timeout_ms=500,
        )
        check_database_config(config)
        assert pool_limits(config) == {
            "min_size": 2, "max_size": 8, "timeout": 2.5, "max_waiting": 3,
            "startup_timeout": 0.5, "statement_timeout_ms": 500,
        }

    def test_pool_not_a_mapping(self) -> None:
        """pool: must be a mapping."""
        _check_fails(_config(pool=4), "pool")

    def test_unknown_pool_key(self) -> None:
        """Unknown pool keys stop startup."""
        _check_fails(_config(pool={"max_lifetime": 60}), "max_lifetime")

    @pytest.mark.parametrize("key", ["min_size", "max_size", "max_waiting"])
    @pytest.mark.parametrize("value", [0, -1, True, 1.5, "2"])
    def test_bad_sizes(self, key: str, value: Any) -> None:
        """Sizes and the queue limit must be positive integers."""
        _check_fails(_config(pool={key: value}), key)

    @pytest.mark.parametrize("key", ["timeout", "startup_timeout"])
    @pytest.mark.parametrize("value", [0, -1, True, float("inf"), float("nan"), "5"])
    def test_bad_timeouts(self, key: str, value: Any) -> None:
        """Pool timeouts must be finite positive numbers."""
        _check_fails(_config(pool={key: value}), key)

    def test_min_size_above_max_size(self) -> None:
        """min_size can't be larger than max_size."""
        _check_fails(_config(pool={"min_size": 5, "max_size": 4}), "min_size", "max_size")

    @pytest.mark.parametrize("value", [0, -5, True, 1.5, "100"])
    def test_bad_statement_timeout(self, value: Any) -> None:
        """statement_timeout_ms must be a positive integer."""
        _check_fails(_config(statement_timeout_ms=value), "statement_timeout_ms")


class TestTableNames:
    """PostgreSQL tables are plain schema.table or table names."""

    def test_reserved_word_is_accepted(self) -> None:
        """Names are quoted when used, so reserved words work."""
        check_database_config(_config(tables={"order": "catalog.order"}))

    def test_uri_mapping_is_refused(self) -> None:
        """The DuckDB {uri: ...} form is not accepted for PostgreSQL."""
        _check_fails(_config(tables={"items": {"uri": "catalog.items"}}), "items")

    @pytest.mark.parametrize("name", [
        "Catalog.items", "catalog.Items", "a.b.c", "catalog.", "1items", "my-table",
        '"catalog"."items"', "x" * 64,
    ])
    def test_bad_table_names(self, name: str) -> None:
        """Mixed case, quoting, three parts and other characters are refused."""
        _check_fails(_config(tables={"items": name}), "items")

    def test_longest_name_is_accepted(self) -> None:
        """A component of 63 bytes is within PostgreSQL's limit."""
        check_database_config(_config(tables={"items": "x" * 63}))

    @pytest.mark.parametrize("alias", ["Items", "my-items", "x" * 64])
    def test_bad_table_keys(self, alias: str) -> None:
        """Table keys are used as aliases and follow the same rules."""
        _check_fails(_config(tables={alias: "catalog.items"}), alias)


class TestEscapeStrings:
    """For PostgreSQL configs, E'...' strings are recognised by the quote check."""

    def test_variable_in_escape_string(self) -> None:
        """A variable inside E'...' is refused."""
        config = _config(routes=[{"route": "r", "sql": "SELECT E'a{{x}}'"}])
        _check_fails(config, "r")

    def test_escaped_quote_does_not_hide_later_variable(self) -> None:
        """E'it\\'s' ends at its real closing quote, so a later quoted variable is found."""
        config = _config(routes=[{"route": "r", "sql": "SELECT E'it\\'s', '{{x}}'"}])
        _check_fails(config, "r")

    def test_variable_after_escape_string(self) -> None:
        """A variable after an E'...' string with an escaped quote is accepted."""
        config = _config(routes=[{"route": "r", "sql": "SELECT E'it\\'s' || {{x}}"}])
        check_database_config(config)


class TestResolveConnection:
    """At start, env: values are read and connect_timeout is checked."""

    def test_env_values_are_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """env:VAR is replaced by the variable's value; connect_timeout defaults to 5."""
        monkeypatch.setenv("SHOP_PASSWORD", "s3cret")
        fields = resolve_connection_fields(_config())
        assert fields["password"] == "s3cret"
        assert fields["connect_timeout"] == "5"
        assert fields["host"] == "db.example.com"

    def test_missing_env_names_variable_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A missing variable is an error naming the variable."""
        monkeypatch.delenv("SHOP_PASSWORD", raising=False)
        with pytest.raises(ValueError, match="SHOP_PASSWORD"):
            resolve_connection_fields(_config())

    @pytest.mark.parametrize("value", ["abc", "0", "-3", "1.5"])
    def test_bad_connect_timeout_from_env(
            self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        """connect_timeout read from the environment must be a positive integer."""
        monkeypatch.setenv("SHOP_PASSWORD", "s3cret")
        monkeypatch.setenv("CONNECT_TIMEOUT", value)
        config = _with_connection(connect_timeout="env:CONNECT_TIMEOUT")
        with pytest.raises(ValueError, match="connect_timeout"):
            resolve_connection_fields(config)


class TestBuildSettings:
    """Connection fields become a libpq connection string; api_dock's own options are separate."""

    @pytest.mark.parametrize("key,value", [
        ("sslmode", "verfy-full"), ("sslmode", "VERIFY-FULL"), ("sslmode", ""),
        ("port", "abc"), ("port", "5432,abc"), ("port", "0"),
        ("port", "65536"), ("port", "5_432"), ("port", "５４３２"),
    ])
    @pytest.mark.parametrize("from_env", [False, True])
    def test_invalid_connection_values(
            self, monkeypatch: pytest.MonkeyPatch, key: str, value: str,
            from_env: bool) -> None:
        """Reject invalid resolved values without including them in the error."""
        monkeypatch.setenv("SHOP_PASSWORD", "s3cret")
        monkeypatch.setenv("CONNECTION_VALUE", value)
        field = "env:CONNECTION_VALUE" if from_env else value
        with pytest.raises(ValueError, match=key) as error:
            build_settings(_with_connection(**{key: field}))
        if value:
            assert value not in str(error.value)
        assert "s3cret" not in str(error.value)

    @pytest.mark.parametrize("sslmode,port", [
        (mode, "5432") for mode in
        ["disable", "allow", "prefer", "require", "verify-ca", "verify-full"]
    ] + [("prefer", port) for port in ["", "5432,", ",5433", "1,65535", " +5432 "]])
    def test_valid_connection_values(
            self, monkeypatch: pytest.MonkeyPatch, sslmode: str, port: str) -> None:
        """Keep libpq's SSL modes, multiple ports and empty default-port entries."""
        monkeypatch.setenv("SHOP_PASSWORD", "s3cret")
        build_settings(_with_connection(host="db1,db2", sslmode=sslmode, port=port))

    def test_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The connection string has the fields; limits use the defaults."""
        monkeypatch.setenv("SHOP_PASSWORD", "s3cret")
        settings = build_settings(_config())
        assert "host=db.example.com" in settings.conninfo
        assert "password=s3cret" in settings.conninfo
        assert "connect_timeout=5" in settings.conninfo
        assert "options" not in settings.conninfo
        assert (settings.max_size, settings.statement_timeout_ms) == (4, 10000)

    @pytest.mark.parametrize("key", ["autocommit", "prepare_threshold", "row_factory", "hostt"])
    def test_unknown_libpq_option(self, monkeypatch: pytest.MonkeyPatch, key: str) -> None:
        """psycopg arguments and misspellings are not libpq options and stop startup."""
        monkeypatch.setenv("SHOP_PASSWORD", "s3cret")
        with pytest.raises(ValueError) as error:
            build_settings(_with_connection(**{key: "true"}))
        assert key in str(error.value)
        assert "s3cret" not in str(error.value)

    def test_connection_options(self) -> None:
        """Every connection is read-only, has a statement timeout and uses UTC."""
        assert connection_options(2500) == (
            "-c default_transaction_read_only=on -c statement_timeout=2500 -c TimeZone=UTC"
        )

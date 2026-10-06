"""

PostgreSQL Configuration Module for API Dock

Checks and resolves the settings of database configs with
``backend: postgres``. Nothing here imports the PostgreSQL driver, so configs
can be checked without it installed.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import math
import os
import re
from dataclasses import dataclass
from typing import Any, Dict


#
# CONSTANTS
#
CONNECTION_KEY: str = "connection"
POOL_KEY: str = "pool"
STATEMENT_TIMEOUT_KEY: str = "statement_timeout_ms"
CONNECT_TIMEOUT_KEY: str = "connect_timeout"
ENV_PREFIX: str = "env:"

# Connection settings api_dock sets on every connection (see
# connection_options), so a config can't change them.
RESERVED_CONNECTION_KEYS: frozenset = frozenset({
    "options", "default_transaction_read_only", "statement_timeout",
})

DEFAULT_CONNECT_TIMEOUT: int = 5
DEFAULT_STATEMENT_TIMEOUT_MS: int = 10000

# pool: keys and their defaults. Sizes are integers; timeouts are seconds.
DEFAULT_POOL_LIMITS: Dict[str, Any] = {
    "min_size": 1,
    "max_size": 4,
    "timeout": 5,
    "max_waiting": 16,
    "startup_timeout": 3,
}
POOL_INTEGER_KEYS: frozenset = frozenset({"min_size", "max_size", "max_waiting"})

ENV_NAME: re.Pattern[str] = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
SSL_MODES: frozenset = frozenset({
    "disable", "allow", "prefer", "require", "verify-ca", "verify-full",
})

# A lower-case PostgreSQL name that needs no quoting to keep its case.
IDENTIFIER: re.Pattern[str] = re.compile(r'[a-z_][a-z0-9_]*')
MAX_IDENTIFIER_BYTES: int = 63


#
# PUBLIC
#
@dataclass(frozen=True)
class PostgresSettings:
    """Resolved settings for one PostgreSQL connection pool.

    Attributes:
        conninfo: libpq connection string. It may hold a password, so it is
            never logged or shown in errors.
        min_size: Connections kept open.
        max_size: Most connections open at once.
        timeout: Seconds a request may wait for a connection.
        max_waiting: Most requests waiting for a connection at once.
        startup_timeout: Seconds the startup check waits for a connection.
        statement_timeout_ms: Milliseconds a query may run.
    """

    conninfo: str
    min_size: int
    max_size: int
    timeout: float
    max_waiting: int
    startup_timeout: float
    statement_timeout_ms: int


def check_postgres_config(database_config: Dict[str, Any]) -> None:
    """Validate connection fields, limits and tables without reading the environment.

    Raises ValueError for invalid configuration; no PostgreSQL driver is needed.
    """
    _check_connection(database_config.get(CONNECTION_KEY))
    _check_pool(database_config.get(POOL_KEY, {}))
    if STATEMENT_TIMEOUT_KEY in database_config:
        _check_positive_integer(STATEMENT_TIMEOUT_KEY, database_config[STATEMENT_TIMEOUT_KEY])
    for alias, name in database_config.get("tables", {}).items():
        _check_table(alias, name)


def pool_limits(database_config: Dict[str, Any]) -> Dict[str, Any]:
    """Return pool limits and statement_timeout_ms with defaults applied."""
    limits = {**DEFAULT_POOL_LIMITS, **database_config.get(POOL_KEY, {})}
    limits[STATEMENT_TIMEOUT_KEY] = database_config.get(
        STATEMENT_TIMEOUT_KEY, DEFAULT_STATEMENT_TIMEOUT_MS
    )
    return limits


def resolve_connection_fields(database_config: Dict[str, Any]) -> Dict[str, str]:
    """Resolve env: values and validate connection timeouts, SSL mode and ports.

    Return text values, with connect_timeout defaulting to 5 seconds. Missing
    environment variables or invalid values raise ValueError without exposing values.
    """
    fields = {CONNECT_TIMEOUT_KEY: DEFAULT_CONNECT_TIMEOUT, **database_config[CONNECTION_KEY]}
    resolved = {key: _resolve_value(key, value) for key, value in fields.items()}
    timeout = resolved[CONNECT_TIMEOUT_KEY]
    if not (timeout.isdigit() and int(timeout) > 0):
        raise ValueError(f"connection: '{CONNECT_TIMEOUT_KEY}' must be a positive integer")
    _check_resolved_connection(resolved)
    return resolved


def connection_options(statement_timeout_ms: int) -> str:
    """Build libpq options for read-only transactions, the query timeout and UTC."""
    return (
        "-c default_transaction_read_only=on "
        f"-c statement_timeout={int(statement_timeout_ms)} -c TimeZone=UTC"
    )


#
# INTERNAL
#
def _check_resolved_connection(fields: Dict[str, str]) -> None:
    """Check SSL mode and ports locally; make_conninfo only checks syntax and keys."""
    # An empty value is refused: libpq would silently use its default, prefer.
    if "sslmode" in fields and fields["sslmode"] not in SSL_MODES:
        raise ValueError("connection: 'sslmode' must be one of " + ", ".join(sorted(SSL_MODES)))
    for port in fields.get("port", "").split(","):
        # Empty entries select libpq's default port in a multi-host connection.
        if port == "":
            continue
        if not re.fullmatch(r"\s*[+-]?[0-9]+\s*", port, flags=re.ASCII):
            raise ValueError("connection: 'port' must contain integer port numbers")
        if not 1 <= int(port) <= 65535:
            raise ValueError("connection: 'port' must be between 1 and 65535")


def _check_connection(connection: Any) -> None:
    """Reject a missing connection mapping, reserved keys or invalid field values."""
    if not isinstance(connection, dict):
        raise ValueError(
            f"backend 'postgres' needs a '{CONNECTION_KEY}' mapping of libpq connection fields"
        )
    for key, value in connection.items():
        if key in RESERVED_CONNECTION_KEYS:
            raise ValueError(
                f"connection: '{key}' can't be set; api_dock sets read-only "
                "transactions, the statement timeout and the time zone itself"
            )
        _check_connection_value(str(key), value)


def _check_connection_value(key: str, value: Any) -> None:
    """Reject unsupported value types, malformed env: references and invalid timeouts."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"connection: '{key}' must be text or an integer")
    if isinstance(value, str) and value.startswith(ENV_PREFIX):
        if not ENV_NAME.fullmatch(value[len(ENV_PREFIX):]):
            raise ValueError(
                f"connection: '{key}' must be written env:NAME with an environment variable name"
            )
    elif key == CONNECT_TIMEOUT_KEY:
        _check_positive_integer(f"connection: '{key}'", value)


def _check_pool(pool: Any) -> None:
    """Validate pool keys, positive limits and min_size <= max_size."""
    if not isinstance(pool, dict):
        raise ValueError(f"'{POOL_KEY}' must be a mapping")
    for key, value in pool.items():
        if key not in DEFAULT_POOL_LIMITS:
            raise ValueError(
                f"pool: unknown key '{key}'; allowed keys are {sorted(DEFAULT_POOL_LIMITS)}"
            )
        if key in POOL_INTEGER_KEYS:
            _check_positive_integer(f"pool: '{key}'", value)
        else:
            _check_positive_number(f"pool: '{key}'", value)
    limits = {**DEFAULT_POOL_LIMITS, **pool}
    if limits["min_size"] > limits["max_size"]:
        raise ValueError("pool: 'min_size' can't be larger than 'max_size'")


def _check_positive_integer(name: str, value: Any) -> None:
    """Require a positive integer, excluding booleans."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _check_positive_number(name: str, value: Any) -> None:
    """Require a finite positive number, excluding booleans."""
    is_number = isinstance(value, (int, float)) and not isinstance(value, bool)
    if not (is_number and math.isfinite(value) and value > 0):
        raise ValueError(f"{name} must be a finite positive number of seconds")


def _check_table(alias: Any, name: Any) -> None:
    """Require a lower-case alias and a string table name with an optional schema."""
    rule = (
        "must be a lower-case name of letters, digits and underscores, "
        f"at most {MAX_IDENTIFIER_BYTES} bytes"
    )
    if not _is_identifier(alias):
        raise ValueError(f"table key '{alias}' {rule}")
    if not isinstance(name, str):
        raise ValueError(f"table '{alias}' must be a 'schema.table' or 'table' name")
    parts = name.split(".")
    if len(parts) > 2 or not all(_is_identifier(part) for part in parts):
        raise ValueError(
            f"table '{alias}': '{name}' must be 'schema.table' or 'table'; each part {rule}"
        )


def _is_identifier(name: Any) -> bool:
    """Check the allowed identifier characters and PostgreSQL byte limit."""
    if not isinstance(name, str) or len(name.encode()) > MAX_IDENTIFIER_BYTES:
        return False
    return IDENTIFIER.fullmatch(name) is not None


def _resolve_value(key: str, value: Any) -> str:
    """Return a field as text, resolving env:NAME if present.

    An unset variable raises ValueError naming the field and variable, never its value.
    """
    text = str(value)
    if not text.startswith(ENV_PREFIX):
        return text
    variable = text[len(ENV_PREFIX):]
    if variable not in os.environ:
        raise ValueError(
            f"connection: '{key}' reads environment variable {variable}, which is not set"
        )
    return os.environ[variable]

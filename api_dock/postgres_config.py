"""

PostgreSQL Config Module for API Dock

Checks and resolves the named PostgreSQL connections in the shared database
config (``database.connections`` in ``databases/config.yaml``) and the
PostgreSQL table names that point at them. Nothing here needs the PostgreSQL
driver, so configs are checked even when ``api_dock[postgres]`` isn't installed.

A connection entry holds libpq connection fields (host, port, dbname, user,
password, sslmode, ...), plus two api_dock keys: ``pool`` (connection pool
limits) and ``statement_timeout_ms``. A value written ``env:NAME`` is read from
the environment variable NAME.

Adapted from PR #6 (PostgreSQL backend).

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
POOL_KEY: str = "pool"
STATEMENT_TIMEOUT_KEY: str = "statement_timeout_ms"
CONNECT_TIMEOUT_KEY: str = "connect_timeout"
ENV_PREFIX: str = "env:"

# Keys of a connection entry that are api_dock settings, not libpq fields.
API_DOCK_CONNECTION_KEYS: frozenset = frozenset({POOL_KEY, STATEMENT_TIMEOUT_KEY})

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
        fields: libpq connection fields with ``env:`` values resolved. They may
            hold a password, so they are never logged or shown in errors.
        min_size: Connections kept open.
        max_size: Most connections open at once.
        timeout: Seconds a request may wait for a connection.
        max_waiting: Most requests waiting for a connection at once.
        startup_timeout: Seconds the startup check waits for a connection.
        statement_timeout_ms: Milliseconds a query may run.
    """

    fields: Dict[str, str]
    min_size: int
    max_size: int
    timeout: float
    max_waiting: int
    startup_timeout: float
    statement_timeout_ms: int


def check_connection_config(name: str, entry: Any) -> None:
    """Validate one connection entry without reading the environment.

    Args:
        name: Connection name (its key in ``database.connections``).
        entry: The connection entry.

    Raises:
        ValueError: If the name, a field, the pool limits or the statement
            timeout is invalid. The message names the connection, never a value.
    """
    prefix = f"connection '{name}'"
    if not IDENTIFIER.fullmatch(str(name)):
        raise ValueError(f"{prefix}: name must be lower case letters, digits and underscores")
    if not isinstance(entry, dict) or not entry:
        raise ValueError(f"{prefix} must be a mapping of libpq connection fields")
    try:
        for key, value in libpq_fields(entry).items():
            if key in RESERVED_CONNECTION_KEYS:
                raise ValueError(
                    f"'{key}' can't be set; api_dock sets read-only transactions, the "
                    "statement timeout and the time zone itself"
                )
            _check_connection_value(str(key), value)
        _check_pool(entry.get(POOL_KEY, {}))
        if STATEMENT_TIMEOUT_KEY in entry:
            _check_positive_integer(f"'{STATEMENT_TIMEOUT_KEY}'", entry[STATEMENT_TIMEOUT_KEY])
    except ValueError as error:
        raise ValueError(f"{prefix}: {error}") from error


def check_postgres_table_name(alias: str, table: Any) -> None:
    """Validate a PostgreSQL table name (``schema.table`` or ``table``).

    Args:
        alias: The table's key in the config, for error messages.
        table: The PostgreSQL table name.

    Raises:
        ValueError: If the name isn't one or two lower-case identifiers.
    """
    rule = (
        "must be a lower-case name of letters, digits and underscores, "
        f"at most {MAX_IDENTIFIER_BYTES} bytes"
    )
    if not isinstance(table, str):
        raise ValueError(f"table '{alias}' must be a 'schema.table' or 'table' name")
    parts = table.split(".")
    if len(parts) > 2 or not all(_is_identifier(part) for part in parts):
        raise ValueError(
            f"table '{alias}': '{table}' must be 'schema.table' or 'table'; each part {rule}"
        )


def libpq_fields(entry: Dict[str, Any]) -> Dict[str, Any]:
    """The libpq connection fields of a connection entry (without api_dock keys).

    Args:
        entry: The connection entry.

    Returns:
        The entry without ``pool`` and ``statement_timeout_ms``.
    """
    return {k: v for k, v in entry.items() if k not in API_DOCK_CONNECTION_KEYS}


def resolve_settings(entry: Dict[str, Any]) -> PostgresSettings:
    """Resolve ``env:`` values and defaults into the settings for a pool.

    Args:
        entry: A connection entry that passed check_connection_config.

    Returns:
        PostgresSettings with text connection fields (``connect_timeout``
        defaults to 5 s) and pool limits with defaults applied.

    Raises:
        ValueError: If an environment variable is missing or a resolved value
            (connect_timeout, sslmode, port) is invalid. Never shows values.
    """
    fields = {CONNECT_TIMEOUT_KEY: DEFAULT_CONNECT_TIMEOUT, **libpq_fields(entry)}
    resolved = {key: _resolve_value(key, value) for key, value in fields.items()}
    timeout = resolved[CONNECT_TIMEOUT_KEY]
    if not (timeout.isdigit() and int(timeout) > 0):
        raise ValueError(f"'{CONNECT_TIMEOUT_KEY}' must be a positive integer")
    _check_resolved_connection(resolved)
    limits = {**DEFAULT_POOL_LIMITS, **entry.get(POOL_KEY, {})}
    return PostgresSettings(
        fields=resolved,
        statement_timeout_ms=entry.get(STATEMENT_TIMEOUT_KEY, DEFAULT_STATEMENT_TIMEOUT_MS),
        **limits,
    )


def conninfo(fields: Dict[str, str]) -> str:
    """Build a libpq connection string (``key='value' ...``) from resolved fields.

    Used to attach PostgreSQL connections to DuckDB, which takes a libpq
    connection string. Values are quoted with ``\\`` and ``'`` escaped.

    Args:
        fields: Resolved connection fields (see resolve_settings), plus any
            extra fields such as ``options``.

    Returns:
        The connection string. It may hold a password: never log it.
    """
    def quote(value: str) -> str:
        """Quote one value for a libpq connection string."""
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"

    return " ".join(f"{key}={quote(str(value))}" for key, value in fields.items())


def connection_options(statement_timeout_ms: int) -> str:
    """Build libpq options for read-only transactions, the query timeout and UTC.

    Args:
        statement_timeout_ms: Milliseconds a query may run.

    Returns:
        The libpq ``options`` string set on every connection.
    """
    return (
        "-c default_transaction_read_only=on "
        f"-c statement_timeout={int(statement_timeout_ms)} -c TimeZone=UTC"
    )


#
# INTERNAL
#
def _check_resolved_connection(fields: Dict[str, str]) -> None:
    """Check SSL mode and ports, which libpq would otherwise accept or default.

    Args:
        fields: Resolved text connection fields.

    Raises:
        ValueError: If sslmode or a port is invalid.
    """
    # An empty value is refused: libpq would silently use its default, prefer.
    if "sslmode" in fields and fields["sslmode"] not in SSL_MODES:
        raise ValueError("'sslmode' must be one of " + ", ".join(sorted(SSL_MODES)))
    for port in fields.get("port", "").split(","):
        # Empty entries select libpq's default port in a multi-host connection.
        if port == "":
            continue
        if not re.fullmatch(r"\s*[+-]?[0-9]+\s*", port, flags=re.ASCII):
            raise ValueError("'port' must contain integer port numbers")
        if not 1 <= int(port) <= 65535:
            raise ValueError("'port' must be between 1 and 65535")


def _check_connection_value(key: str, value: Any) -> None:
    """Reject unsupported value types, malformed env: references and bad timeouts.

    Args:
        key: Field name.
        value: Field value.

    Raises:
        ValueError: If the value is invalid.
    """
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"'{key}' must be text or an integer")
    if isinstance(value, str) and value.startswith(ENV_PREFIX):
        if not ENV_NAME.fullmatch(value[len(ENV_PREFIX):]):
            raise ValueError(f"'{key}' must be written env:NAME with an environment variable name")
    elif key == CONNECT_TIMEOUT_KEY:
        _check_positive_integer(f"'{key}'", value)


def _check_pool(pool: Any) -> None:
    """Validate pool keys, positive limits and min_size <= max_size.

    Args:
        pool: The connection's ``pool`` mapping.

    Raises:
        ValueError: If a key or limit is invalid.
    """
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
    """Require a positive integer, excluding booleans.

    Args:
        name: Setting name, for the message.
        value: Value to check.

    Raises:
        ValueError: If the value isn't a positive integer.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _check_positive_number(name: str, value: Any) -> None:
    """Require a finite positive number, excluding booleans.

    Args:
        name: Setting name, for the message.
        value: Value to check.

    Raises:
        ValueError: If the value isn't a finite positive number.
    """
    is_number = isinstance(value, (int, float)) and not isinstance(value, bool)
    if not (is_number and math.isfinite(value) and value > 0):
        raise ValueError(f"{name} must be a finite positive number of seconds")


def _is_identifier(name: Any) -> bool:
    """Check the allowed identifier characters and PostgreSQL's byte limit.

    Args:
        name: Candidate identifier.

    Returns:
        True if it's a lower-case identifier of at most 63 bytes.
    """
    if not isinstance(name, str) or len(name.encode()) > MAX_IDENTIFIER_BYTES:
        return False
    return IDENTIFIER.fullmatch(name) is not None


def _resolve_value(key: str, value: Any) -> str:
    """Return a field as text, resolving env:NAME if present.

    Args:
        key: Field name, for the message.
        value: Field value.

    Returns:
        The value as text.

    Raises:
        ValueError: If the referenced environment variable isn't set (names the
            field and variable, never a value).
    """
    text = str(value)
    if not text.startswith(ENV_PREFIX):
        return text
    variable = text[len(ENV_PREFIX):]
    if variable not in os.environ:
        raise ValueError(f"'{key}' reads environment variable {variable}, which is not set")
    return os.environ[variable]

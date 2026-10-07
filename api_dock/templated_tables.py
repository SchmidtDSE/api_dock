"""

Templated Tables Module for API Dock

A DuckDB table is templated if its ``uri:`` contains ``{{``. Its URI is one
resolver value, ``{{<resolver>.<column>}}``. api_dock binds the URI as a value
of a reader such as ``read_parquet(?)``, so the URI never becomes SQL text.
Before binding, the URI must match an ``allow:`` prefix and must not contain a
``..`` segment or a wildcard character. ``files:`` is then added to its end.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote

from api_dock.storage_auth import detect_storage_backend


#
# CONSTANTS
#
URI_KEY: str = "uri"
PATH_KEY: str = "path"
FORMAT_KEY: str = "format"
FILES_KEY: str = "files"
ALLOW_KEY: str = "allow"

# Table keys that only a templated table can have.
TEMPLATED_TABLE_KEYS: Tuple[str, ...] = (FORMAT_KEY, FILES_KEY, ALLOW_KEY)

# The format selects the reader. The URI's extension does not, so a resolver
# can't change how a file is read.
READERS: Dict[str, str] = {
    "parquet": "read_parquet",
    "csv": "read_csv",
    "json": "read_json",
}

# A templated uri: is exactly one {{<resolver>.<column>}}.
URI_PATTERN: re.Pattern[str] = re.compile(r'\{\{([A-Za-z_][A-Za-z0-9_]*)\.([^{}.]+)\}\}')

# DuckDB reads these characters in a path as wildcards.
WILDCARD_CHARACTERS: str = "*?["

# A [[table]] reference; group 1 is the table name.
TABLE_REFERENCE_PATTERN: re.Pattern[str] = re.compile(r'\[\[([^\]]+)\]\]')

# A [[table]] reference is a table source if FROM or JOIN is in this many
# characters before it. Otherwise it is the table's alias.
SOURCE_CONTEXT_LENGTH: int = 20


#
# PUBLIC
#
class TableUriError(ValueError):
    """Raised when a resolved table URI can't be used.

    Attributes:
        table: The table name.
        reason: Why the URI was refused.
    """

    def __init__(self, table: str, reason: str) -> None:
        """Create the error for one table.

        Args:
            table: The table name.
            reason: Why the URI was refused.
        """
        self.table = table
        self.reason = reason
        super().__init__(f"table '{table}': {reason}")


def is_templated_table(table_def: Any) -> bool:
    """Return whether a table definition has a ``{{`` in its URI or path.

    A plain string definition counts as a URI.

    Args:
        table_def: A ``tables:`` entry.

    Returns:
        True if the table is templated.
    """
    if isinstance(table_def, str):
        return "{{" in table_def
    if not isinstance(table_def, dict):
        return False
    return any(
        isinstance(table_def.get(key), str) and "{{" in table_def[key]
        for key in (URI_KEY, PATH_KEY)
    )


def table_value_name(table_def: Dict[str, Any]) -> str:
    """Return the ``<resolver>.<column>`` name of a checked templated table's URI.

    Args:
        table_def: A templated table, as checked by check_table().

    Returns:
        The variable name in its ``uri:``.
    """
    return table_def[URI_KEY][2:-2]


def table_source(table_name: str, table_def: Dict[str, Any], variable: str) -> str:
    """Write the reader call that reads a templated table, with its alias.

    Args:
        table_name: The table name, used as its alias.
        table_def: A checked templated table.
        variable: Name of the variable that holds the bound URI.

    Returns:
        SQL text such as ``read_parquet({{variable}}) AS readings``.
    """
    reader = READERS[table_def[FORMAT_KEY]]
    return f"{reader}({{{{{variable}}}}}) AS {table_name}"


def bound_uri(table_name: str, table_def: Dict[str, Any], uri: Optional[str]) -> str:
    """Check a resolved URI and add the table's ``files:`` pattern.

    Args:
        table_name: The table name, for errors.
        table_def: A checked templated table.
        uri: The resolver value.

    Returns:
        The URI to bind.

    Raises:
        TableUriError: If the URI is NULL, has a ``..`` segment (before or
            after percent-decoding) or a wildcard character, or matches no
            ``allow:`` prefix.
    """
    if uri is None:
        raise TableUriError(table_name, "the URI is NULL")
    if _has_parent_segment(uri) or _has_parent_segment(unquote(uri)):
        raise TableUriError(table_name, "the URI contains a '..' segment")
    if any(character in uri for character in WILDCARD_CHARACTERS):
        raise TableUriError(table_name, "the URI contains a wildcard character")
    if not any(_matches_prefix(uri, prefix) for prefix in table_def[ALLOW_KEY]):
        raise TableUriError(table_name, "the URI matches no allow: prefix")
    files = table_def.get(FILES_KEY)
    if files is None:
        return uri
    return (uri if uri.endswith("/") else uri + "/") + files


def check_table(table_name: str, table_def: Any, is_postgres: bool) -> None:
    """Check the templated-table rules of one ``tables:`` entry.

    Args:
        table_name: The table name.
        table_def: The table definition.
        is_postgres: Whether the database uses PostgreSQL.

    Raises:
        ValueError: If a rule is broken. The message gives the reason.
    """
    if not is_templated_table(table_def):
        if isinstance(table_def, dict) and any(key in table_def for key in TEMPLATED_TABLE_KEYS):
            raise ValueError("format:, files: and allow: need a templated uri:")
        return
    if is_postgres:
        raise ValueError("a PostgreSQL database can't have a templated table")
    if isinstance(table_def, str):
        raise ValueError("a templated table needs format: (parquet, csv or json)")
    if PATH_KEY in table_def:
        raise ValueError("a templated table can't use path:")
    if not URI_PATTERN.fullmatch(table_def[URI_KEY]):
        raise ValueError("a templated uri: must be exactly one {{<resolver>.<column>}}")
    if table_def.get(FORMAT_KEY) not in READERS:
        raise ValueError("a templated table needs format: (parquet, csv or json)")
    _check_allow(table_def.get(ALLOW_KEY))
    if FILES_KEY in table_def:
        _check_files(table_def[FILES_KEY])


def templated_table_names(database_config: Dict[str, Any]) -> List[str]:
    """List the names of a config's templated tables.

    Args:
        database_config: Database configuration.

    Returns:
        Table names, in config order.
    """
    tables = database_config.get("tables", {})
    if not isinstance(tables, dict):
        return []
    return [name for name, table_def in tables.items() if is_templated_table(table_def)]


def is_table_source(sql: str, start: int) -> bool:
    """Return whether the [[table]] reference at start is a table source.

    Args:
        sql: SQL template.
        start: Position of the reference's first character.

    Returns:
        True if FROM or JOIN comes shortly before the reference.
    """
    context = sql[max(0, start - SOURCE_CONTEXT_LENGTH):start].upper()
    return 'FROM' in context or 'JOIN' in context


def table_sources(sql: str) -> List[str]:
    """List the tables that an SQL template reads, as [[table]] sources.

    Args:
        sql: SQL template.

    Returns:
        Table names, in template order.
    """
    return [
        match.group(1) for match in TABLE_REFERENCE_PATTERN.finditer(sql)
        if is_table_source(sql, match.start())
    ]


def table_names_in(sql: str) -> List[str]:
    """List every [[table]] reference in an SQL template, source or alias.

    Args:
        sql: SQL template.

    Returns:
        Table names, in template order.
    """
    return TABLE_REFERENCE_PATTERN.findall(sql)


#
# INTERNAL
#
def _check_allow(allow: Any) -> None:
    """Refuse a missing allow: list, a bad prefix, or prefixes of mixed storage types."""
    if not isinstance(allow, list) or not allow:
        raise ValueError("a templated table needs allow:, a non-empty list of URI prefixes")
    for prefix in allow:
        if not isinstance(prefix, str):
            raise ValueError("allow: entries must be text")
        if not prefix:
            raise ValueError("allow: '' is empty")
        if _has_parent_segment(prefix):
            raise ValueError(f"allow: '{prefix}' contains a '..' segment")
        if any(character in prefix for character in WILDCARD_CHARACTERS):
            raise ValueError(f"allow: '{prefix}' contains a wildcard character")
    storage_types = sorted({detect_storage_backend(prefix) for prefix in allow})
    if len(storage_types) > 1:
        raise ValueError(
            f"allow: prefixes use more than one storage type ({', '.join(storage_types)})"
        )


def _check_files(files: Any) -> None:
    """Refuse a files: pattern that is not relative text inside the URI's folder."""
    if not isinstance(files, str):
        raise ValueError("files: must be text")
    if not files:
        raise ValueError("files: can't be empty")
    if files.startswith("/"):
        raise ValueError("files: can't start with '/'")
    if _has_parent_segment(files):
        raise ValueError("files: can't contain a '..' segment")


def _has_parent_segment(path: str) -> bool:
    """Return whether a path has a ``..`` segment between slashes."""
    return ".." in path.split("/")


def _matches_prefix(uri: str, prefix: str) -> bool:
    """Return whether a URI is a prefix, or continues it after a slash.

    So ``s3://b/pub`` matches ``s3://b/pub/x`` but not ``s3://b/pub-secret/x``.
    """
    if uri == prefix:
        return True
    if not uri.startswith(prefix):
        return False
    return prefix.endswith("/") or uri[len(prefix)] == "/"

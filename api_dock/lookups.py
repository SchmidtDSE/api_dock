"""

Lookups for API Dock

A lookup is a named query that api_dock runs itself (at startup, then every
``refresh`` interval or on demand) and whose rows fill in config: a ``from:``
entry in ``slugs`` or ``versions`` becomes one entry per row, with
``{{row.<column>}}`` filled in, and ``{{lookup.<name>.<column>}}`` reads a
one-row lookup anywhere in a config. Lookups are defined under ``lookups:`` in
``databases/config.yaml`` and ``remotes/config.yaml``:

    lookups:
      model_runs:
        sql: SELECT model, version, uri FROM [[core.model_runs]]
        refresh: 7d
        allow: ["s3://bucket/runs/"]
      deployments:
        remote: core
        path: deployments
        rows: data.items

A SQL lookup runs on DuckDB (which attaches PostgreSQL connections read-only),
so it can read any table the shared config defines. An HTTP lookup reads JSON
from a remote (its ``url`` and injected cookies) or a ``url``.

Lookup values never become SQL text: templates aren't allowed in SQL fields,
values used in a table ``uri`` or remote ``url`` must start with one of the
lookup's ``allow`` prefixes, and generated names and versions must be plain.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import contextvars
import copy
import hmac
import itertools
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple
from urllib.parse import unquote


#
# CONSTANTS
#
LOOKUPS_KEY: str = "lookups"
FROM_KEY: str = "from"

# Source keys: exactly one per lookup.
SQL_KEY: str = "sql"
URL_KEY: str = "url"
REMOTE_KEY: str = "remote"
SOURCE_KEYS: Tuple[str, ...] = (SQL_KEY, URL_KEY, REMOTE_KEY)

# Other lookup keys.
REFRESH_KEY: str = "refresh"
REQUIRED_KEY: str = "required"
ALLOW_KEY: str = "allow"
PATH_KEY: str = "path"
PARAMS_KEY: str = "params"
HEADERS_KEY: str = "headers"
ROWS_KEY: str = "rows"
VERSION_KEY: str = "version"
TIMEOUT_KEY: str = "timeout"
DESCRIPTION_KEY: str = "description"
HTTP_KEYS: frozenset = frozenset({PATH_KEY, PARAMS_KEY, HEADERS_KEY, ROWS_KEY, TIMEOUT_KEY})
COMMON_KEYS: frozenset = frozenset({REFRESH_KEY, REQUIRED_KEY, ALLOW_KEY, DESCRIPTION_KEY})

# Seconds an HTTP lookup waits by default.
DEFAULT_HTTP_TIMEOUT: float = 30.0

# "{{row.column}}" / "{{lookup.name.column}}" (dotted paths read nested JSON).
TEMPLATE_PATTERN: re.Pattern = re.compile(r"\{\{\s*(row|lookup)\.([^{}\s]+)\s*\}\}")
ROW_SCOPE: str = "row"
LOOKUP_SCOPE: str = "lookup"

# Config keys whose values are SQL (or written into responses); lookup values
# are never allowed inside them.
FORBIDDEN_KEYS: frozenset = frozenset({
    "routes", "query_params", "queries", "sql", "sql_append", "multivalue_sql",
    "response", "missing_response", "conditional",
})

# Keys whose values are URIs/URLs: lookup values in them must match ``allow``.
URI_KEYS: frozenset = frozenset({"uri", "url"})

# Lookup names; generated database names/versions and schema names.
LOOKUP_NAME_PATTERN: re.Pattern = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PLAIN_VALUE_PATTERN: re.Pattern = re.compile(r"^[A-Za-z0-9._-]+$")
SCHEMA_NAME_PATTERN: re.Pattern = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Characters never allowed in a URI/URL filled from a lookup.
UNSAFE_URI_CHARACTERS: frozenset = frozenset("'\"\\`")

# "30s", "15m", "12h", "7d", "2w" (or a number of seconds).
DURATION_PATTERN: re.Pattern = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*$")
DURATION_UNITS: Dict[str, float] = {
    "": 1.0, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0, "w": 604800.0,
}

# Prefix for environment-variable values (HTTP headers).
ENV_PREFIX: str = "env:"

# settings.lookups keys (the manual refresh endpoint).
REFRESH_ROUTE_KEY: str = "refresh_route"
TOKEN_KEY: str = "token"
BEARER_PREFIX: str = "Bearer "

# At most this many expanded configs are memoized.
MEMO_SIZE: int = 64

logger = logging.getLogger(__name__)

_STORES: Dict[str, "LookupStore"] = {}
_STORES_LOCK = threading.Lock()
_ACTIVE_STORE: contextvars.ContextVar = contextvars.ContextVar(
    "api_dock_lookup_store", default=None
)
_GENERATIONS = itertools.count(1)
_MEMO: Dict[Tuple[Any, ...], Any] = {}
_MEMO_LOCK = threading.Lock()


#
# PUBLIC
#
class LookupFailure(ValueError):
    """A lookup failed to run or returned something unusable."""


class RowTemplateError(ValueError):
    """A row can't fill a template (missing column, refused value, bad name)."""


@dataclass
class LookupSpec:
    """A parsed lookup definition.

    Attributes:
        name: Lookup name.
        source: The file that defines it (for messages).
        sql: SQL for a SQL lookup, else None.
        url: URL for an HTTP lookup, else None.
        remote: Remote name for an HTTP lookup through a remote, else None.
        version: Remote version (versioned remotes), or None.
        path: Path appended to the remote's url, or None.
        params: Query parameters for an HTTP lookup.
        headers: Request headers for an HTTP lookup (``env:`` values allowed).
        rows: Dotted path to the row list in an HTTP response, or None.
        timeout: Seconds an HTTP lookup waits.
        refresh: Seconds between refreshes, or None (startup and manual only).
        required: Whether a failure at startup stops startup.
        allow: Prefixes a URI/URL filled from this lookup must start with.
    """

    name: str
    source: str
    sql: Optional[str] = None
    url: Optional[str] = None
    remote: Optional[str] = None
    version: Optional[str] = None
    path: Optional[str] = None
    params: Dict[str, Any] = field(default_factory=dict)
    headers: Dict[str, str] = field(default_factory=dict)
    rows: Optional[str] = None
    timeout: float = DEFAULT_HTTP_TIMEOUT
    refresh: Optional[float] = None
    required: bool = False
    allow: List[str] = field(default_factory=list)

    @property
    def kind(self) -> str:
        """``sql`` or ``http``."""
        return SQL_KEY if self.sql is not None else "http"


@dataclass
class LookupResult:
    """The latest state of one lookup.

    Attributes:
        rows: Rows from the last successful run, or None if it never succeeded.
        fetched_at: Epoch seconds of the last successful run, or None.
        attempted_at: Epoch seconds of the last attempt, or None.
        error: Message from the last failed attempt (cleared on success).
    """

    rows: Optional[List[Dict[str, Any]]] = None
    fetched_at: Optional[float] = None
    attempted_at: Optional[float] = None
    error: Optional[str] = None


@dataclass
class LookupEndpoint:
    """The manual refresh endpoint from ``settings.lookups``.

    Attributes:
        route: Path served, without slashes at either end (e.g. "admin/lookups").
        token: Bearer token requests must send.
    """

    route: str
    token: str


@dataclass
class LookupContext:
    """What running a lookup needs beyond its spec.

    Attributes:
        config_dir: Base config directory.
        main_config: The main config (for remote lookups).
        duckdb_options: The ``settings.duckdb`` mapping, or None.
    """

    config_dir: str
    main_config: Dict[str, Any] = field(default_factory=dict)
    duckdb_options: Any = None


class LookupStore:
    """Lookup definitions and their latest rows for one config directory.

    Config loaders read rows from the store; RouteMapper runs lookups and
    refreshes them. ``generation`` changes whenever rows change, so expanded
    configs can be memoized on it.
    """

    def __init__(self, config_dir: str) -> None:
        """Create an empty store.

        Args:
            config_dir: Base config directory.
        """
        self.config_dir = config_dir
        self.specs: Dict[str, LookupSpec] = {}
        self.results: Dict[str, LookupResult] = {}
        # Generations are unique across stores, so expanded configs can be memoized on
        # (file, generation): a trial store and the store it's committed to share them.
        self.generation = 0
        self.context = LookupContext(config_dir)
        self._lock = threading.Lock()
        self._refreshing = False

    def configure(self, specs: Dict[str, LookupSpec], context: LookupContext) -> None:
        """Set the lookup definitions and run context (rows are kept by name).

        Args:
            specs: Lookup name -> spec.
            context: Run context.
        """
        with self._lock:
            self.specs = dict(specs)
            self.context = context
            self.results = {name: self.results.get(name, LookupResult()) for name in specs}
            self.generation = next(_GENERATIONS)

    def rows(self, name: str) -> Optional[List[Dict[str, Any]]]:
        """Rows from a lookup's last successful run.

        Args:
            name: Lookup name.

        Returns:
            The rows, or None if the lookup isn't defined or never succeeded.
        """
        result = self.results.get(name)
        return None if result is None else result.rows

    def set_rows(self, name: str, rows: List[Dict[str, Any]]) -> None:
        """Set a lookup's rows directly (no run and no checks), e.g. from an embedding app.

        Args:
            name: A defined lookup.
            rows: The rows.

        Raises:
            ValueError: If the lookup isn't defined.
        """
        if name not in self.specs:
            raise ValueError(f"Unknown lookup: {name}")
        now = time.time()
        with self._lock:
            self.results[name] = LookupResult(rows=list(rows), fetched_at=now, attempted_at=now)
            self.generation = next(_GENERATIONS)

    def due(self, now: Optional[float] = None) -> List[str]:
        """Lookups whose refresh interval has passed.

        Args:
            now: Current epoch seconds (default: time.time()).

        Returns:
            Names of lookups with a ``refresh`` interval that are due.
        """
        now = time.time() if now is None else now
        return [
            name for name, spec in self.specs.items()
            if spec.refresh is not None and now >= self._next_run(name, spec)
        ]

    def seconds_until_due(self, now: Optional[float] = None) -> Optional[float]:
        """Seconds until the next lookup is due.

        Args:
            now: Current epoch seconds (default: time.time()).

        Returns:
            Seconds (0 if one is due now), or None if no lookup refreshes.
        """
        now = time.time() if now is None else now
        times = [
            self._next_run(name, spec) for name, spec in self.specs.items()
            if spec.refresh is not None
        ]
        return max(0.0, min(times) - now) if times else None

    def run(self,
            names: Optional[List[str]] = None,
            validate: Optional[Callable[[], None]] = None) -> Dict[str, Any]:
        """Run lookups and keep their rows if the resulting config is valid.

        Each lookup runs on its own; one that fails keeps its previous rows.
        The new rows of every lookup that ran are then checked together:
        ``validate`` runs with a trial store holding them (other requests keep
        seeing the current rows). If it raises, all of them keep their previous
        rows. Only one run happens at a time; a run started meanwhile does nothing.

        Args:
            names: Lookups to run (default: all).
            validate: Check of the whole config; raises if the new rows make it
                invalid.

        Returns:
            Report: ``{"refreshed": [...], "failed": {name: message},
            "rejected": message or None, "skipped": bool}``.

        Raises:
            ValueError: If a name isn't a defined lookup.
        """
        targets = list(self.specs) if names is None else list(names)
        unknown = [name for name in targets if name not in self.specs]
        if unknown:
            raise ValueError(f"Unknown lookup(s): {', '.join(unknown)}")
        report: Dict[str, Any] = {"refreshed": [], "failed": {}, "rejected": None, "skipped": False}

        with self._lock:
            if self._refreshing:
                report["skipped"] = True
                return report
            self._refreshing = True
        try:
            candidates: Dict[str, List[Dict[str, Any]]] = {}
            now = time.time()
            for name in targets:
                try:
                    candidates[name] = run_lookup(self.specs[name], self.context)
                except Exception as error:
                    message = _error_message(error)
                    report["failed"][name] = message
                    logger.warning("Lookup '%s' failed: %s", name, message)

            with self._lock:
                for name in targets:
                    self.results[name].attempted_at = now
                    if name in report["failed"]:
                        self.results[name].error = report["failed"][name]
            if not candidates:
                return report

            trial = self._trial(candidates, now)
            if validate is not None:
                token = _ACTIVE_STORE.set(trial)
                try:
                    validate()
                except Exception as error:
                    message = _error_message(error)
                    report["rejected"] = message
                    with self._lock:
                        for name in candidates:
                            self.results[name].error = f"new rows rejected: {message}"
                    logger.warning(
                        "Lookup rows for %s rejected (kept the previous rows): %s",
                        ", ".join(candidates), message
                    )
                    return report
                finally:
                    _ACTIVE_STORE.reset(token)

            with self._lock:
                self.results = trial.results
                self.generation = trial.generation
            report["refreshed"] = list(candidates)
            return report
        finally:
            with self._lock:
                self._refreshing = False

    def status(self) -> List[Dict[str, Any]]:
        """Describe every lookup (for the status endpoint and CLI).

        Returns:
            One dict per lookup: name, kind, source, rows, fetched_at,
            attempted_at, next_refresh (epoch seconds or None) and error.
        """
        report = []
        for name, spec in self.specs.items():
            result = self.results.get(name, LookupResult())
            report.append({
                "name": name,
                "kind": spec.kind,
                "source": spec.source,
                "rows": None if result.rows is None else len(result.rows),
                "fetched_at": result.fetched_at,
                "attempted_at": result.attempted_at,
                "next_refresh": (
                    self._next_run(name, spec) if spec.refresh is not None else None
                ),
                "error": result.error,
            })
        return report

    def _next_run(self, name: str, spec: LookupSpec) -> float:
        result = self.results.get(name) or LookupResult()
        if result.fetched_at is None:
            # Never succeeded: retry after the interval from the last attempt.
            return (result.attempted_at or 0.0) + (spec.refresh or 0.0)
        return result.fetched_at + (spec.refresh or 0.0)

    def _trial(self, candidates: Dict[str, List[Dict[str, Any]]], now: float) -> "LookupStore":
        trial = LookupStore(self.config_dir)
        trial.specs = self.specs
        trial.context = self.context
        with self._lock:
            trial.results = {name: copy.copy(result) for name, result in self.results.items()}
        for name, rows in candidates.items():
            trial.results[name] = LookupResult(rows=rows, fetched_at=now, attempted_at=now)
        trial.generation = next(_GENERATIONS)
        return trial


def get_store(config_dir: Optional[str] = None) -> LookupStore:
    """The lookup store for a config directory (a trial store while validating).

    Args:
        config_dir: Base config directory. If None, uses default.

    Returns:
        The LookupStore.
    """
    if config_dir is None:
        from api_dock.config import DEFAULT_CONFIG_DIR
        config_dir = DEFAULT_CONFIG_DIR
    key = os.path.abspath(config_dir)
    active = _ACTIVE_STORE.get()
    if active is not None and os.path.abspath(active.config_dir) == key:
        return active
    with _STORES_LOCK:
        store = _STORES.get(key)
        if store is None:
            store = LookupStore(config_dir)
            _STORES[key] = store
        return store


def parse_duration(value: Any, label: str = REFRESH_KEY) -> Optional[float]:
    """Parse a refresh interval.

    Args:
        value: Seconds (number), a string like ``"15m"`` / ``"7d"`` / ``"2w"``,
            or None / False / 0 for no refresh.
        label: Name used in error messages.

    Returns:
        Seconds, or None for no refresh.

    Raises:
        ValueError: If the value isn't a valid duration.
    """
    if value is None or value is False or value == 0:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a duration like '7d', or false")
    if isinstance(value, (int, float)):
        if value < 0:
            raise ValueError(f"{label} must not be negative")
        return float(value)
    match = DURATION_PATTERN.match(str(value))
    if not match:
        raise ValueError(f"{label} must be a duration like 30s, 15m, 12h, 7d or 2w, got {value!r}")
    seconds = float(match.group(1)) * DURATION_UNITS[match.group(2)]
    return seconds or None


def parse_lookups(raw: Any, source: str) -> Dict[str, LookupSpec]:
    """Parse and check a ``lookups`` mapping.

    Args:
        raw: The ``lookups`` value from a shared config file (or None).
        source: The file it came from (for messages).

    Returns:
        Lookup name -> LookupSpec.

    Raises:
        ValueError: If the mapping or an entry is invalid.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"'{LOOKUPS_KEY}' in {source} must be a mapping of name -> lookup")
    specs = {}
    for name, entry in raw.items():
        label = f"{source}: lookup '{name}'"
        if not isinstance(name, str) or not LOOKUP_NAME_PATTERN.match(name):
            raise ValueError(f"{label}: names must be letters, digits and underscores")
        if not isinstance(entry, dict):
            raise ValueError(f"{label} must be a mapping")
        specs[name] = _parse_lookup(name, entry, source, label)
    return specs


def load_lookup_specs(config_dir: Optional[str] = None) -> Dict[str, LookupSpec]:
    """Collect the lookups from ``databases/config.yaml`` and ``remotes/config.yaml``.

    Args:
        config_dir: Base config directory. If None, uses default.

    Returns:
        Lookup name -> LookupSpec.

    Raises:
        ValueError: If a lookup is invalid or a name is defined in both files.
    """
    from api_dock.config import load_static_remote_config
    from api_dock.database_config import load_static_shared_config

    import yaml

    try:
        specs = dict(load_static_shared_config(config_dir).get(LOOKUPS_KEY) or {})
    except (ValueError, yaml.YAMLError) as error:
        raise ValueError(f"Shared database config (databases/config.yaml): {error}") from error
    try:
        remote_specs = load_static_remote_config(config_dir).get(LOOKUPS_KEY) or {}
    except (ValueError, yaml.YAMLError) as error:
        raise ValueError(f"Shared remote config (remotes/config.yaml): {error}") from error
    for name, spec in remote_specs.items():
        if name in specs:
            raise ValueError(
                f"Lookup '{name}' is defined in both {specs[name].source} and {spec.source}"
            )
        specs[name] = spec
    return specs


def parse_lookup_settings(raw: Any) -> Optional[LookupEndpoint]:
    """Parse ``settings.lookups`` (the manual refresh endpoint).

    Args:
        raw: The ``settings.lookups`` mapping, or None.

    Returns:
        The endpoint, or None if no ``refresh_route`` is set.

    Raises:
        ValueError: If the mapping is invalid, has no ``token``, or its
            ``env:`` token variable isn't set.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("settings.lookups must be a mapping")
    unknown = sorted(set(raw) - {REFRESH_ROUTE_KEY, TOKEN_KEY})
    if unknown:
        raise ValueError(f"settings.lookups: unknown key(s) {', '.join(unknown)}")
    route = raw.get(REFRESH_ROUTE_KEY)
    if route is None:
        return None
    if not isinstance(route, str) or not route.strip("/ "):
        raise ValueError(f"settings.lookups.{REFRESH_ROUTE_KEY} must be a path like /admin/lookups")
    token = raw.get(TOKEN_KEY)
    if not isinstance(token, str) or not token.strip():
        raise ValueError(
            f"settings.lookups.{TOKEN_KEY} is required (e.g. env:API_DOCK_ADMIN_TOKEN)"
        )
    try:
        token = _env_value(token, f"settings.lookups.{TOKEN_KEY}")
    except LookupFailure as error:
        raise ValueError(str(error)) from error
    if not token:
        raise ValueError(f"settings.lookups.{TOKEN_KEY} is empty")
    return LookupEndpoint(route=route.strip().strip("/"), token=token)


def check_endpoint_token(authorization: Optional[str], token: str) -> bool:
    """Whether an Authorization header carries the endpoint's bearer token.

    Args:
        authorization: The request's Authorization header, or None.
        token: The expected token.

    Returns:
        True if it is ``Bearer <token>`` (compared in constant time).
    """
    if not authorization or not authorization.startswith(BEARER_PREFIX):
        return False
    return hmac.compare_digest(authorization[len(BEARER_PREFIX):].strip().encode(), token.encode())


def run_lookup(spec: LookupSpec, context: LookupContext) -> List[Dict[str, Any]]:
    """Run a lookup and return its rows.

    Args:
        spec: The lookup.
        context: Run context.

    Returns:
        Rows as dicts with JSON-safe values.

    Raises:
        Exception: Whatever the source raises (callers report the message).
    """
    if spec.kind == SQL_KEY:
        return _run_sql(spec, context)
    return _run_http(spec, context)


def has_templates(value: Any) -> bool:
    """Whether a config value contains ``{{row.*}}`` or ``{{lookup.*}}``.

    Args:
        value: Any config value.

    Returns:
        True if a string anywhere in it has a lookup template.
    """
    return any(TEMPLATE_PATTERN.search(text) for text, _ in _strings(value, ()))


def check_template_positions(value: Any, label: str, allow_row: bool = False) -> None:
    """Refuse lookup templates where they aren't allowed.

    Args:
        value: A config value.
        label: Where it came from (for messages).
        allow_row: Whether ``{{row.*}}`` is allowed (inside a ``from:`` entry).

    Raises:
        ValueError: If a template is inside an SQL/response field, or a
            ``{{row.*}}`` template is outside a ``from:`` entry.
    """
    for text, path in _strings(value, ()):
        for match in TEMPLATE_PATTERN.finditer(text):
            location = ".".join(str(part) for part in path) or "value"
            if any(part in FORBIDDEN_KEYS for part in path):
                raise ValueError(
                    f"{label}: lookup values can't be used in {location} (SQL and responses)"
                )
            if match.group(1) == ROW_SCOPE and not allow_row:
                raise ValueError(f"{label}: {{{{row.*}}}} is only allowed in a 'from:' entry "
                                 f"({location})")


def fill_templates(value: Any, row: Optional[Dict[str, Any]], store: LookupStore,
                   key_path: Tuple[Any, ...] = ()) -> Any:
    """Fill ``{{row.*}}`` and ``{{lookup.*}}`` templates in a config value.

    A string that is exactly one template takes the value itself (so numbers
    stay numbers); otherwise each template is written as text (None as "").
    A mapping key whose value is exactly one template that is null is left out
    (so a row can leave out an optional table: ``{uri: "{{row.x}}"}`` with a
    null ``x`` drops that table).
    URI/URL values (``uri``/``url`` keys, and plain-string table definitions)
    filled from a lookup must match that lookup's ``allow`` prefixes.

    Args:
        value: A config value (copied, never changed).
        row: The row for ``{{row.*}}`` (None outside a ``from:`` entry).
        store: Store for ``{{lookup.*}}`` values (and ``allow`` prefixes).
        key_path: Keys leading to value (used to spot URI fields).

    Returns:
        The filled value.

    Raises:
        RowTemplateError: If the row lacks a column or a value is refused.
        ValueError: If a ``{{lookup.*}}`` lookup isn't defined, has no rows yet
            or doesn't have exactly one row.
    """
    if isinstance(value, dict):
        filled_dict = {}
        for key, item in value.items():
            if _is_whole_template(item) and _resolves_to_none(item, row, store):
                continue  # a key set to exactly one null value is left out
            filled_dict[key] = fill_templates(item, row, store, key_path + (key,))
        return {k: v for k, v in filled_dict.items() if not _is_emptied_table(k, v, value)}
    if isinstance(value, list):
        return [fill_templates(v, row, store, key_path + (i,)) for i, v in enumerate(value)]
    if not isinstance(value, str) or not TEMPLATE_PATTERN.search(value):
        return value

    used: Set[str] = set()

    def lookup_value(match: re.Match) -> Any:
        scope, path = match.group(1), match.group(2)
        if scope == ROW_SCOPE:
            if row is None:
                raise ValueError("{{row.*}} is only allowed in a 'from:' entry")
            used.add(row.get("__lookup__", ""))
            return _read_path(row, path, "row")
        name, _, column = path.partition(".")
        used.add(name)
        return _read_path(_single_row(name, store), column, f"lookup '{name}'")

    whole = TEMPLATE_PATTERN.fullmatch(value.strip())
    if whole:
        filled = lookup_value(whole)
    else:
        filled = TEMPLATE_PATTERN.sub(lambda m: _text(lookup_value(m)), value)

    if _is_uri_field(key_path):
        _check_allowed(filled, [name for name in used if name], store, key_path)
    return filled


def expand_entries(entries: List[Any], store: LookupStore, label: str,
                   check: Optional[Callable[[Dict[str, Any]], None]] = None
                   ) -> Iterator[Tuple[Dict[str, Any], Optional[str]]]:
    """Expand ``from:`` entries in a list; other entries pass through.

    A ``{from: <lookup>, ...}`` entry yields one filled copy per row (without
    ``from``). A row that can't fill the entry (missing column, refused value,
    or a failed ``check``) is skipped with a warning. A lookup with no rows
    yet yields nothing.

    Args:
        entries: The list (e.g. ``slugs`` or a ``versions`` list).
        store: The lookup store.
        label: Where the list came from (for messages).
        check: Extra check on each filled entry; raises RowTemplateError to
            skip the row.

    Yields:
        (entry, lookup name) pairs; lookup name is None for static entries.

    Raises:
        ValueError: If a ``from:`` names an unknown lookup or a template is in
            a forbidden position.
    """
    for entry in entries:
        if not isinstance(entry, dict) or FROM_KEY not in entry:
            if isinstance(entry, dict):
                # Nested from: entries (versions) are checked when they're expanded.
                check_template_positions(
                    {k: v for k, v in entry.items() if generated_versions(v) is None}, label
                )
            yield entry, None
            continue
        name = entry[FROM_KEY]
        if name not in store.specs:
            raise ValueError(f"{label}: 'from: {name}' is not a defined lookup")
        template = {k: v for k, v in entry.items() if k != FROM_KEY}
        check_template_positions(template, label, allow_row=True)
        for index, row in enumerate(store.rows(name) or []):
            tagged = {**row, "__lookup__": name}
            try:
                filled = fill_templates(template, tagged, store)
                if check is not None:
                    check(filled)
            except RowTemplateError as error:
                logger.warning("%s: lookup '%s' row %d skipped: %s", label, name, index, error)
                continue
            yield filled, name


def generated_versions(versions: Any) -> Optional[List[Any]]:
    """A ``versions`` value as a list, if it has ``from:`` entries.

    Args:
        versions: A ``versions`` value: a list of entries, or one
            ``{from: <lookup>, ...}`` mapping.

    Returns:
        The entries as a list if any is a ``from:`` entry, else None.
    """
    if isinstance(versions, dict) and FROM_KEY in versions:
        return [versions]
    if isinstance(versions, list) and any(
            isinstance(entry, dict) and FROM_KEY in entry for entry in versions):
        return versions
    return None


def check_plain(value: Any, what: str) -> str:
    """Check a generated name or version.

    Args:
        value: The generated value.
        what: What it is (for messages).

    Returns:
        The value as a string.

    Raises:
        RowTemplateError: If it is empty or has characters other than letters,
            digits, ``.``, ``_`` and ``-``.
    """
    text = "" if value is None else str(value).strip()
    if not PLAIN_VALUE_PATTERN.match(text):
        raise RowTemplateError(f"{what} {text!r} must be letters, digits, '.', '_' or '-'")
    return text


def check_schema_name(value: Any) -> str:
    """Check a generated schema name (it becomes an SQL identifier).

    Args:
        value: The generated value.

    Returns:
        The name.

    Raises:
        RowTemplateError: If it isn't a plain identifier.
    """
    text = "" if value is None else str(value)
    if not SCHEMA_NAME_PATTERN.match(text):
        raise RowTemplateError(f"schema name {text!r} must be letters, digits and underscores")
    return text


def memoized(key: Tuple[Any, ...], build: Callable[[], Any]) -> Any:
    """Return a cached build result for a key (deep-copied, so callers may change it).

    Args:
        key: Cache key (include file mtime and store generation).
        build: Builds the value on a miss.

    Returns:
        A copy of the cached value.
    """
    with _MEMO_LOCK:
        if key in _MEMO:
            return copy.deepcopy(_MEMO[key])
    value = build()
    with _MEMO_LOCK:
        if len(_MEMO) >= MEMO_SIZE:
            _MEMO.clear()
        _MEMO[key] = value
    return copy.deepcopy(value)


def file_key(path: str) -> Tuple[Any, ...]:
    """A cache key part for a file's current contents.

    Args:
        path: File path.

    Returns:
        (path, mtime_ns, size), or (path, None, None) if it doesn't exist.
    """
    try:
        stat = os.stat(path)
    except OSError:
        return (path, None, None)
    return (os.path.abspath(path), stat.st_mtime_ns, stat.st_size)


#
# INTERNAL
#
def _parse_lookup(name: str, entry: Dict[str, Any], source: str, label: str) -> LookupSpec:
    sources = [key for key in SOURCE_KEYS if key in entry]
    if len(sources) != 1:
        raise ValueError(f"{label} needs exactly one of {', '.join(SOURCE_KEYS)}")
    kind = sources[0]
    allowed = COMMON_KEYS | {kind} | (HTTP_KEYS if kind != SQL_KEY else set())
    if kind == REMOTE_KEY:
        allowed = allowed | {VERSION_KEY}
    unknown = sorted(set(entry) - allowed)
    if unknown:
        raise ValueError(f"{label}: unknown key(s) {', '.join(unknown)}")

    spec = LookupSpec(name=name, source=source)
    value = entry[kind]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}: '{kind}' must be a non-empty string")
    setattr(spec, kind, value.strip() if kind != SQL_KEY else value)
    if kind == SQL_KEY:
        from api_dock.sql_builder import VARIABLE_PATTERN
        if VARIABLE_PATTERN.search(value):
            raise ValueError(f"{label}: lookup SQL can't use {{{{variables}}}}")
    if VERSION_KEY in entry:
        spec.version = str(entry[VERSION_KEY])
    if PATH_KEY in entry:
        if not isinstance(entry[PATH_KEY], str):
            raise ValueError(f"{label}: '{PATH_KEY}' must be a string")
        spec.path = entry[PATH_KEY]
    for key in (PARAMS_KEY, HEADERS_KEY):
        if key in entry:
            if not isinstance(entry[key], dict):
                raise ValueError(f"{label}: '{key}' must be a mapping")
            setattr(spec, key, {str(k): v for k, v in entry[key].items()})
    if ROWS_KEY in entry:
        if not isinstance(entry[ROWS_KEY], str) or not entry[ROWS_KEY].strip():
            raise ValueError(f"{label}: '{ROWS_KEY}' must be a dotted path like data.items")
        spec.rows = entry[ROWS_KEY].strip()
    if TIMEOUT_KEY in entry:
        timeout = entry[TIMEOUT_KEY]
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError(f"{label}: '{TIMEOUT_KEY}' must be a positive number of seconds")
        spec.timeout = float(timeout)
    spec.refresh = parse_duration(entry.get(REFRESH_KEY), f"{label}: {REFRESH_KEY}")
    required = entry.get(REQUIRED_KEY, False)
    if not isinstance(required, bool):
        raise ValueError(f"{label}: '{REQUIRED_KEY}' must be true or false")
    spec.required = required
    allow = entry.get(ALLOW_KEY) or []
    if isinstance(allow, str):
        allow = [allow]
    if not isinstance(allow, list) or not all(isinstance(p, str) and p.strip() for p in allow):
        raise ValueError(f"{label}: '{ALLOW_KEY}' must be a list of URI/URL prefixes")
    spec.allow = [prefix.strip() for prefix in allow]
    return spec


def _run_sql(spec: LookupSpec, context: LookupContext) -> List[Dict[str, Any]]:
    from api_dock.database_backends import DuckDBBackend
    from api_dock.database_config import (
        load_static_shared_config,
        SCHEMA_GROUPS_KEY,
        SHARED_CONFIG_KEY,
        SHARED_CONNECTIONS_KEY,
    )
    from api_dock.sql_builder import build_sql_query_with_tables
    from api_dock.types import SqlContext

    shared_file = load_static_shared_config(context.config_dir)
    shared = shared_file.get(SHARED_CONFIG_KEY) or {}
    sql, values, tables = build_sql_query_with_tables(
        {SQL_KEY: spec.sql}, {}, shared_config=shared,
        context=SqlContext(schema_groups=shared_file.get(SCHEMA_GROUPS_KEY) or {}),
    )
    backend = DuckDBBackend(context.duckdb_options, shared.get(SHARED_CONNECTIONS_KEY) or {})
    columns, rows = backend._execute_in_thread(sql, values, tables)
    return [dict(zip(columns, (_json_safe(v) for v in row))) for row in rows]


def _run_http(spec: LookupSpec, context: LookupContext) -> List[Dict[str, Any]]:
    import httpx

    from api_dock.config import find_remote_config, resolve_inject_cookies

    cookies: Dict[str, str] = {}
    if spec.remote is not None:
        remote_config = find_remote_config(
            spec.remote, context.main_config, context.config_dir, version=spec.version
        )
        base = remote_config.get(URL_KEY)
        if not base:
            raise LookupFailure(f"remote '{spec.remote}' has no url")
        url = base.rstrip("/") + ("/" + spec.path.lstrip("/") if spec.path else "")
        cookies = resolve_inject_cookies(remote_config)
    else:
        url = spec.url.rstrip("/") + ("/" + spec.path.lstrip("/") if spec.path else "")
    headers = {name: _env_value(value, f"header {name}") for name, value in spec.headers.items()}

    with httpx.Client(timeout=spec.timeout, follow_redirects=True, cookies=cookies) as client:
        response = client.get(url, params=spec.params, headers=headers)
    if response.status_code >= 400:
        raise LookupFailure(f"HTTP {response.status_code} from {url}")
    try:
        body = response.json()
    except ValueError as error:
        raise LookupFailure(f"{url} didn't return JSON") from error

    data = body
    if spec.rows:
        for part in spec.rows.split("."):
            if not isinstance(data, dict) or part not in data:
                raise LookupFailure(f"'{spec.rows}' not found in the response from {url}")
            data = data[part]
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
        where = f" at '{spec.rows}'" if spec.rows else " (set 'rows')"
        raise LookupFailure(f"the response from {url} isn't a list of objects{where}")
    return [dict(row) for row in data]


def _env_value(value: Any, label: str) -> str:
    text = str(value)
    if not text.startswith(ENV_PREFIX):
        return text
    name = text[len(ENV_PREFIX):]
    if name not in os.environ:
        raise LookupFailure(f"{label}: environment variable {name} is not set")
    return os.environ[name]


def _json_safe(value: Any) -> Any:
    from api_dock.route_mapper import _make_json_safe
    return _make_json_safe(value)


def _strings(value: Any, path: Tuple[Any, ...]) -> Iterator[Tuple[str, Tuple[Any, ...]]]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item, path + (key,))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, path + (index,))
    elif isinstance(value, str):
        yield value, path


def _read_path(data: Dict[str, Any], path: str, label: str) -> Any:
    if path in data:
        return data[path]
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise RowTemplateError(f"{label} has no column '{path}'")
        current = current[part]
    return current


def _single_row(name: str, store: LookupStore) -> Dict[str, Any]:
    if name not in store.specs:
        raise ValueError(f"{{{{lookup.{name}.*}}}}: '{name}' is not a defined lookup")
    rows = store.rows(name)
    if rows is None:
        error = (store.results.get(name) or LookupResult()).error
        raise ValueError(f"lookup '{name}' has no rows yet" + (f" ({error})" if error else ""))
    if len(rows) != 1:
        raise ValueError(f"{{{{lookup.{name}.*}}}} needs exactly one row; '{name}' has {len(rows)}")
    return {**rows[0], "__lookup__": name}


def _is_whole_template(value: Any) -> bool:
    return isinstance(value, str) and TEMPLATE_PATTERN.fullmatch(value.strip()) is not None


def _resolves_to_none(value: str, row: Optional[Dict[str, Any]], store: LookupStore) -> bool:
    match = TEMPLATE_PATTERN.fullmatch(value.strip())
    scope, path = match.group(1), match.group(2)
    try:
        if scope == ROW_SCOPE:
            return row is not None and _read_path(row, path, "row") is None
        name, _, column = path.partition(".")
        return _read_path(_single_row(name, store), column, f"lookup '{name}'") is None
    except ValueError:
        return False  # reported when the value is filled


def _is_emptied_table(key: Any, filled: Any, original: Dict[str, Any]) -> bool:
    """A table definition whose only key (its uri) was left out: drop the table too."""
    source = original.get(key)
    return (isinstance(source, dict) and source and isinstance(filled, dict) and not filled)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _is_uri_field(key_path: Tuple[Any, ...]) -> bool:
    if not key_path:
        return False
    last = key_path[-1]
    if last in URI_KEYS:
        return True
    # A plain-string table definition: tables.<name> = "<uri>"
    return len(key_path) >= 2 and key_path[-2] == "tables" and isinstance(last, str)


def _check_allowed(value: Any, lookups: List[str], store: LookupStore,
                   key_path: Tuple[Any, ...]) -> None:
    location = ".".join(str(part) for part in key_path)
    if not isinstance(value, str):
        raise RowTemplateError(f"{location} must be text, got {value!r}")
    for name in lookups:
        spec = store.specs.get(name)
        prefixes = spec.allow if spec is not None else []
        if not prefixes:
            raise ValueError(
                f"lookup '{name}' fills {location}, so it needs an 'allow' list of prefixes"
            )
        _check_uri_value(value, prefixes, name, location)


def _check_uri_value(value: str, prefixes: List[str], name: str, location: str) -> None:
    if any(char in UNSAFE_URI_CHARACTERS or ord(char) < 32 or ord(char) == 127
           for char in value):
        raise RowTemplateError(f"{location} {value!r} from lookup '{name}' has unsafe characters")
    for text in (value, unquote(value), unquote(unquote(value))):
        if ".." in text.replace("\\", "/").split("/"):
            raise RowTemplateError(f"{location} {value!r} from lookup '{name}' contains '..'")
    if not any(_has_prefix(value, prefix) for prefix in prefixes):
        raise RowTemplateError(
            f"{location} {value!r} from lookup '{name}' doesn't start with an allowed prefix"
        )


def _has_prefix(value: str, prefix: str) -> bool:
    if value == prefix:
        return True
    if not value.startswith(prefix):
        return False
    return prefix.endswith(("/", ":")) or value[len(prefix)] in "/?#"


def _error_message(error: BaseException) -> str:
    message = str(error) or type(error).__name__
    return message.splitlines()[0][:500]

"""

Types Module for API Dock

Shared type definitions used across the proxy response and database pipelines.

License: BSD 3-Clause

"""

#
# IMPORTS
#
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union


#
# PUBLIC
#
@dataclass
class PreparedRequest:
    """Resolved upstream request info, ready for execution.

    Returned by RouteMapper.prepare_remote_request() when all validation passes.
    Contains everything needed to issue the upstream HTTP call. All values are
    already filtered and resolved: URL is fully built, query params are filtered,
    cookies are filtered, and follow_redirects is resolved from settings.

    Attributes:
        url: Fully resolved upstream URL including path.
        method: HTTP method string (GET, POST, etc.).
        headers: Request headers to forward upstream.
        params: Filtered query parameters to forward. A value may be a list to
            forward a repeated key (e.g. ?id=1&id=2).
        cookies: Filtered cookies to forward.
        body: Request body bytes, or None for non-body methods.
        follow_redirects: Whether httpx should follow 3xx automatically.
        timeout: httpx request timeout in seconds, or None for no timeout.
            Resolved from the `timeout` setting (default 10s).
    """

    url: str
    method: str
    headers: Dict[str, str]
    params: Dict[str, Union[str, List[str]]]
    cookies: Dict[str, str]
    body: Optional[bytes]
    follow_redirects: bool
    timeout: Optional[float] = None


@dataclass
class ProxyResponse:
    """Complete response from a proxied upstream request.

    This is the return type of RouteMapper.map_route() and map_database_route(),
    forming the typed contract between the proxy layer and framework adapters
    (FastAPI, Flask). Carrying raw bytes means adapters can return the response
    without re-encoding or re-serializing content.

    Upstream 4xx/5xx responses are passed through verbatim — they are NOT
    treated as failures at this layer. Only api_dock-level errors (bad config,
    unknown remote, blocked route) set error_message.

    Attributes:
        status_code: HTTP status code. May be from the upstream or from
            api_dock itself (e.g. 404 for unknown remote, 403 for blocked route).
        content: Raw response body bytes. Never None; use b"" for empty bodies.
        content_type: MIME type string, e.g. "application/json" or "image/png".
            Sourced from the upstream Content-Type header. Set to
            "application/json" for api_dock-generated responses.
        headers: Upstream response headers that should be forwarded to the
            client. Hop-by-hop headers (Connection, Transfer-Encoding, etc.)
            are already stripped. Content-Type and Content-Length are also
            excluded — Content-Type is in the content_type field above, and
            Content-Length is recalculated by the framework from the content.
            Relevant headers here include Cache-Control, ETag, Last-Modified,
            Vary, Location (for 3xx redirects), and Content-Disposition.
        error_message: Set only for api_dock-level errors (e.g. "Remote 'x'
            not found"). None for successfully proxied responses, including
            upstream 4xx/5xx which are passed through as-is.
    """

    status_code: int
    content: bytes
    content_type: str
    headers: Dict[str, str] = field(default_factory=dict)
    error_message: Optional[str] = None


@dataclass
class ListingSpec:
    """A resolved catalog-listing endpoint to expose.

    Produced by ``listings.resolve_listing_specs()`` from the main config's
    ``expose`` section. Each spec becomes one GET route that returns the
    models/versions of databases, remotes, or both ("sources").

    Attributes:
        kind: One of "databases", "remotes", or "sources" (both combined).
        route: URL path for the endpoint, without a leading slash
            (e.g. "databases" or "list/databases").
        as_dict: If True, each entry is ``{"model": ..., "version": ...}``;
            if False, each entry is a ``"model/version"`` string.
        include: True (all), False (none), or a list of selectors naming which
            models to include, optionally with a ``versions`` filter.
    """

    kind: str
    route: str
    as_dict: bool
    include: Any = True


@dataclass
class TableReference:
    """A resolved ``[[table]]`` reference used by a database route's SQL.

    Produced by ``database_config.resolve_table_reference()``. Tables can come
    from the version config's ``tables``, a schema in the shared
    ``databases/config.yaml``, or that file's global tables.

    Attributes:
        name: Table name (e.g. "detections").
        uri: For a file table, the file path/URI it reads from; for a
            PostgreSQL table, its name in PostgreSQL (``schema.table``).
        metadata: Effective storage metadata (region, public, ...) with the
            shared ``meta`` defaults applied and the table's own keys winning.
        schema: Shared-config schema the table belongs to, if any.
        qualified: True when referenced as ``[[schema.table]]``. Qualified
            tables are exposed as DuckDB views (``schema.table``) rather than
            inlined as ``'<uri>' AS table``.
        connection: Name of the PostgreSQL connection (``database.connections``)
            for a PostgreSQL table; None for a file table.
    """

    name: str
    uri: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    schema: Optional[str] = None
    qualified: bool = False
    connection: Optional[str] = None

    @property
    def is_postgres(self) -> bool:
        """True for a table on a PostgreSQL connection."""
        return self.connection is not None

    @property
    def sql_name(self) -> str:
        """Name the table is addressed by in SQL (``schema.table`` if qualified)."""
        if self.qualified and self.schema:
            return f"{self.schema}.{self.name}"
        return self.name


@dataclass
class SqlContext:
    """Request context for building a database route's SQL.

    Supplies what ``[[*.table]]`` / ``[[group.table]]`` unions, the route's
    ``source_columns``, and ``{{self.*}}`` placeholders need beyond the version
    config itself.

    Attributes:
        name: Database slug being queried (e.g. "birdnet"); ``{{self.name}}``.
        version: Resolved version, or None if unversioned; ``{{self.version}}``.
        schema_groups: Shared ``schema_groups`` mapping (group -> schema names).
        schema_sources: Schema name -> (name, version) of the single
            database/version that uses it. Schemas used by none or several are
            absent, so their ``name``/``version`` source columns are NULL.
        connection: The PostgreSQL connection the query runs natively on, or
            None when it runs on DuckDB. Decides how ``[[table]]`` is written.
    """

    name: Optional[str] = None
    version: Optional[str] = None
    schema_groups: Dict[str, List[str]] = field(default_factory=dict)
    schema_sources: Dict[str, Tuple[str, Optional[str]]] = field(default_factory=dict)
    connection: Optional[str] = None

"""

Types Module for API Dock

Shared type definitions used across the proxy response pipeline.

License: BSD 3-Clause

"""

#
# IMPORTS
#
from dataclasses import dataclass, field
from typing import Dict, Optional


#
# PUBLIC
#
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

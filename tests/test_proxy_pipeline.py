"""

Tests for the proxy response pipeline covering all five soundhub issues.

Issue 1 — Binary content types (image, audio) returned correctly.
Issue 2 — 3xx redirects passed through to client when follow_redirects=False.
Issue 3 — Cache-Control and other upstream headers forwarded.
Issue 4 — Upstream 4xx/5xx error bodies passed through verbatim.
Issue 5 — Responses not re-serialized; raw bytes returned as-is.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from api_dock.route_mapper import _filter_response_headers, HOP_BY_HOP_HEADERS, RouteMapper
from api_dock.types import ProxyResponse


#
# PUBLIC
#
class TestFilterResponseHeaders:
    """Tests for the hop-by-hop header stripping logic."""

    def test_strips_hop_by_hop_headers(self):
        raw = {
            "connection": "keep-alive",
            "transfer-encoding": "chunked",
            "content-encoding": "gzip",
            "content-length": "1234",
            "content-type": "application/json",
            "keep-alive": "timeout=5",
        }
        filtered = _filter_response_headers(raw)
        for header in HOP_BY_HOP_HEADERS:
            assert header not in filtered, f"{header} should have been stripped"

    def test_keeps_cache_and_etag_headers(self):
        raw = {
            "cache-control": "max-age=3600, public",
            "etag": '"d41d8cd98f00b204e9800998ecf8427e"',
            "last-modified": "Wed, 21 Oct 2015 07:28:00 GMT",
            "vary": "Accept-Encoding",
            "content-type": "application/json",
            "content-length": "42",
        }
        filtered = _filter_response_headers(raw)
        assert filtered["cache-control"] == "max-age=3600, public"
        assert filtered["etag"] == '"d41d8cd98f00b204e9800998ecf8427e"'
        assert filtered["last-modified"] == "Wed, 21 Oct 2015 07:28:00 GMT"
        assert filtered["vary"] == "Accept-Encoding"

    def test_keeps_location_header(self):
        """Location header must survive for 3xx redirects."""
        raw = {
            "location": "https://s3.amazonaws.com/bucket/key?sig=abc",
            "content-type": "text/html",
            "content-length": "0",
        }
        filtered = _filter_response_headers(raw)
        assert "location" in filtered
        assert filtered["location"] == "https://s3.amazonaws.com/bucket/key?sig=abc"

    def test_empty_headers(self):
        assert _filter_response_headers({}) == {}

    def test_case_insensitive_stripping(self):
        """httpx normalises headers to lowercase; verify our set matches."""
        raw = {"Content-Type": "text/plain", "Cache-Control": "no-cache"}
        filtered = _filter_response_headers(raw)
        # Content-Type is in HOP_BY_HOP (lowercase match check)
        # Cache-Control is NOT — it should survive
        assert "Cache-Control" in filtered


class TestRouteMapperProxyIssues:
    """Integration-style tests for issues 1-5 using a mocked httpx client."""

    def _make_mock_response(self, status_code, content, headers):
        """Build a minimal httpx.Response-like mock."""
        mock = MagicMock(spec=httpx.Response)
        mock.status_code = status_code
        mock.content = content
        mock.headers = httpx.Headers(headers)
        return mock

    def _make_route_mapper(self, settings=None):
        """Build a RouteMapper with a minimal config that has one remote."""
        rm = RouteMapper.__new__(RouteMapper)
        rm.remote_names = ["core"]
        rm.database_names = []
        rm.config = {
            "remotes": ["core"],
            "settings": settings or {},
        }
        rm.settings = settings or {}
        return rm

    def _patch_httpx(self, mock_response):
        """Return a context manager that patches httpx.AsyncClient to return mock_response."""
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.request = AsyncMock(return_value=mock_response)
        return patch("api_dock.route_mapper.httpx.AsyncClient", return_value=mock_client)

    # -----------------------------------------------------------------------
    # Helpers for config loading that map_route calls internally
    # -----------------------------------------------------------------------

    def _patch_config(self):
        """Patch config functions so map_route can find the 'core' remote."""
        remote_cfg = {"url": "https://api.example.com", "name": "core"}
        patches = [
            patch("api_dock.route_mapper.is_versioned_remote", return_value=False),
            patch("api_dock.route_mapper.is_route_allowed", return_value=True),
            patch("api_dock.route_mapper.find_remote_config", return_value=remote_cfg),
            patch("api_dock.route_mapper.filter_remote_query_params", side_effect=lambda qp, *a, **kw: qp),
            patch("api_dock.route_mapper.find_route_mapping", return_value=None),
        ]
        from api_dock.config import filter_cookies_by_config
        patches.append(patch("api_dock.route_mapper.filter_cookies_by_config", return_value={}))
        return patches

    @pytest.mark.anyio
    async def test_issue1_binary_content_type_preserved(self):
        """Issue 1: Binary (image/png) content returned with correct type, not corrupted."""
        png_bytes = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0x00])
        mock_resp = self._make_mock_response(
            200, png_bytes, {"content-type": "image/png"}
        )
        rm = self._make_route_mapper()

        config_patches = self._patch_config()
        for p in config_patches:
            p.start()
        try:
            with self._patch_httpx(mock_resp):
                result = await rm.map_route("core", "spectrograms/1.png", "GET")
        finally:
            for p in config_patches:
                p.stop()

        assert isinstance(result, ProxyResponse)
        assert result.status_code == 200
        assert result.content == png_bytes
        assert result.content_type == "image/png"

    @pytest.mark.anyio
    async def test_issue1_audio_content_type_preserved(self):
        """Issue 1: Audio (audio/mpeg) content returned with correct type."""
        audio_bytes = b"\xff\xfb\x90\x00" * 100
        mock_resp = self._make_mock_response(
            200, audio_bytes, {"content-type": "audio/mpeg"}
        )
        rm = self._make_route_mapper()

        config_patches = self._patch_config()
        for p in config_patches:
            p.start()
        try:
            with self._patch_httpx(mock_resp):
                result = await rm.map_route("core", "audio/clip.mp3", "GET")
        finally:
            for p in config_patches:
                p.stop()

        assert result.content == audio_bytes
        assert result.content_type == "audio/mpeg"

    @pytest.mark.anyio
    async def test_issue2_redirect_passed_through_when_not_following(self):
        """Issue 2: 302 with Location is returned to client when follow_redirects=False."""
        presigned_url = "https://mybucket.s3.amazonaws.com/file.wav?X-Amz-Signature=abc123"
        mock_resp = self._make_mock_response(
            302,
            b"",
            {"content-type": "text/html", "location": presigned_url},
        )
        rm = self._make_route_mapper(settings={"follow_redirects": False})

        config_patches = self._patch_config()
        for p in config_patches:
            p.start()
        try:
            with self._patch_httpx(mock_resp):
                result = await rm.map_route("core", "audio/clip.wav", "GET")
        finally:
            for p in config_patches:
                p.stop()

        assert result.status_code == 302
        assert "location" in result.headers
        assert result.headers["location"] == presigned_url

    @pytest.mark.anyio
    async def test_issue3_cache_control_forwarded(self):
        """Issue 3: Cache-Control, ETag, and Last-Modified are forwarded."""
        mock_resp = self._make_mock_response(
            200,
            b'{"data": []}',
            {
                "content-type": "application/json",
                "cache-control": "max-age=300, public",
                "etag": '"abc123"',
                "last-modified": "Mon, 01 Jan 2024 00:00:00 GMT",
            },
        )
        rm = self._make_route_mapper()

        config_patches = self._patch_config()
        for p in config_patches:
            p.start()
        try:
            with self._patch_httpx(mock_resp):
                result = await rm.map_route("core", "detections/", "GET")
        finally:
            for p in config_patches:
                p.stop()

        assert result.headers.get("cache-control") == "max-age=300, public"
        assert result.headers.get("etag") == '"abc123"'
        assert result.headers.get("last-modified") == "Mon, 01 Jan 2024 00:00:00 GMT"

    @pytest.mark.anyio
    async def test_issue4_upstream_401_body_passed_through(self):
        """Issue 4: Upstream 4xx error body passed through verbatim, not wrapped."""
        upstream_body = b'{"detail": "Missing cookie: `__Secure-authjs.session-token`"}'
        mock_resp = self._make_mock_response(
            401,
            upstream_body,
            {"content-type": "application/json"},
        )
        rm = self._make_route_mapper()

        config_patches = self._patch_config()
        for p in config_patches:
            p.start()
        try:
            with self._patch_httpx(mock_resp):
                result = await rm.map_route("core", "detections/", "GET")
        finally:
            for p in config_patches:
                p.stop()

        assert result.status_code == 401
        assert result.content == upstream_body
        # Must NOT be wrapped in {"error": "Remote API returned 401"}
        parsed = json.loads(result.content)
        assert "detail" in parsed
        assert "error" not in parsed

    @pytest.mark.anyio
    async def test_issue4_upstream_500_body_passed_through(self):
        """Issue 4: Upstream 5xx errors also passed through verbatim."""
        upstream_body = b'{"error": "internal server error", "trace_id": "xyz"}'
        mock_resp = self._make_mock_response(
            500,
            upstream_body,
            {"content-type": "application/json"},
        )
        rm = self._make_route_mapper()

        config_patches = self._patch_config()
        for p in config_patches:
            p.start()
        try:
            with self._patch_httpx(mock_resp):
                result = await rm.map_route("core", "detections/", "GET")
        finally:
            for p in config_patches:
                p.stop()

        assert result.status_code == 500
        assert result.content == upstream_body

    @pytest.mark.anyio
    async def test_issue5_json_not_reparsed(self):
        """Issue 5: JSON response content is raw bytes, not re-serialized."""
        # Use a JSON body with specific whitespace/ordering that would be
        # changed if we parsed and re-dumped it.
        original_json_bytes = b'{"b":  2,  "a":  1}'
        mock_resp = self._make_mock_response(
            200,
            original_json_bytes,
            {"content-type": "application/json"},
        )
        rm = self._make_route_mapper()

        config_patches = self._patch_config()
        for p in config_patches:
            p.start()
        try:
            with self._patch_httpx(mock_resp):
                result = await rm.map_route("core", "detections/", "GET")
        finally:
            for p in config_patches:
                p.stop()

        assert result.content == original_json_bytes

    @pytest.mark.anyio
    async def test_issue5_content_encoding_stripped(self):
        """Issue 5: content-encoding header stripped (httpx decompresses; value would be wrong)."""
        mock_resp = self._make_mock_response(
            200,
            b"decompressed body",
            {
                "content-type": "application/json",
                "content-encoding": "gzip",
                "cache-control": "no-cache",
            },
        )
        rm = self._make_route_mapper()

        config_patches = self._patch_config()
        for p in config_patches:
            p.start()
        try:
            with self._patch_httpx(mock_resp):
                result = await rm.map_route("core", "data/", "GET")
        finally:
            for p in config_patches:
                p.stop()

        assert "content-encoding" not in result.headers
        assert result.headers.get("cache-control") == "no-cache"


class TestApiDockLevelErrors:
    """Tests for api_dock-generated error responses (not upstream errors)."""

    @pytest.mark.anyio
    async def test_unknown_remote_returns_404(self):
        rm = RouteMapper.__new__(RouteMapper)
        rm.remote_names = ["core"]
        rm.database_names = []
        rm.config = {}
        rm.settings = {}

        result = await rm.map_route("nonexistent", "some/path", "GET")
        assert result.status_code == 404
        assert result.error_message is not None
        parsed = json.loads(result.content)
        assert "error" in parsed

    @pytest.mark.anyio
    async def test_blocked_route_returns_403(self):
        rm = RouteMapper.__new__(RouteMapper)
        rm.remote_names = ["core"]
        rm.database_names = []
        rm.config = {}
        rm.settings = {}

        with patch("api_dock.route_mapper.is_versioned_remote", return_value=False), \
             patch("api_dock.route_mapper.is_route_allowed", return_value=False), \
             patch("api_dock.route_mapper.find_remote_config", return_value={"url": "http://x.com"}), \
             patch("api_dock.route_mapper.filter_remote_query_params", side_effect=lambda qp, *a, **kw: qp), \
             patch("api_dock.route_mapper.filter_cookies_by_config", return_value={}):
            result = await rm.map_route("core", "admin/delete", "DELETE")

        assert result.status_code == 403
        assert result.error_message is not None

    @pytest.mark.anyio
    async def test_connection_error_returns_502(self):
        rm = RouteMapper.__new__(RouteMapper)
        rm.remote_names = ["core"]
        rm.database_names = []
        rm.config = {}
        rm.settings = {}

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.request = AsyncMock(
            side_effect=httpx.ConnectError("Connection refused")
        )

        remote_cfg = {"url": "https://api.example.com", "name": "core"}
        with patch("api_dock.route_mapper.is_versioned_remote", return_value=False), \
             patch("api_dock.route_mapper.is_route_allowed", return_value=True), \
             patch("api_dock.route_mapper.find_remote_config", return_value=remote_cfg), \
             patch("api_dock.route_mapper.filter_remote_query_params", side_effect=lambda qp, *a, **kw: qp), \
             patch("api_dock.route_mapper.find_route_mapping", return_value=None), \
             patch("api_dock.route_mapper.filter_cookies_by_config", return_value={}), \
             patch("api_dock.route_mapper.httpx.AsyncClient", return_value=mock_client):
            result = await rm.map_route("core", "data/", "GET")

        assert result.status_code == 502
        assert result.error_message is not None

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
from fastapi.responses import StreamingResponse

from api_dock.fast_api import _filter_streaming_response_headers, _stream_upstream
from api_dock.route_mapper import (
    _filter_response_headers,
    _resolve_timeout,
    DEFAULT_TIMEOUT,
    HOP_BY_HOP_HEADERS,
    RouteMapper,
)
from api_dock.types import PreparedRequest, ProxyResponse


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


class TestStreamingResponseHeaders:
    """Tests for _filter_streaming_response_headers — the streaming-path header filter."""

    def test_keeps_content_encoding(self):
        """Content-Encoding must survive so the client can decompress raw bytes."""
        raw = {
            "content-encoding": "gzip",
            "cache-control": "max-age=300",
            "content-type": "application/json",
            "content-length": "1234",
        }
        filtered = _filter_streaming_response_headers(raw)
        assert "content-encoding" in filtered
        assert filtered["content-encoding"] == "gzip"

    def test_strips_content_length(self):
        """Content-Length must be stripped — streaming lets Starlette compute it."""
        raw = {"content-length": "5000", "content-encoding": "gzip"}
        filtered = _filter_streaming_response_headers(raw)
        assert "content-length" not in filtered

    def test_strips_connection_and_transfer_encoding(self):
        raw = {
            "connection": "keep-alive",
            "transfer-encoding": "chunked",
            "cache-control": "no-cache",
        }
        filtered = _filter_streaming_response_headers(raw)
        assert "connection" not in filtered
        assert "transfer-encoding" not in filtered
        assert filtered.get("cache-control") == "no-cache"

    def test_strips_content_type(self):
        """Content-Type is passed separately as media_type to StreamingResponse."""
        raw = {"content-type": "application/octet-stream", "x-custom": "value"}
        filtered = _filter_streaming_response_headers(raw)
        assert "content-type" not in filtered
        assert filtered.get("x-custom") == "value"

    def test_empty_headers(self):
        assert _filter_streaming_response_headers({}) == {}


class TestPrepareRemoteRequest:
    """Tests for RouteMapper.prepare_remote_request()."""

    def _make_route_mapper(self, settings=None):
        rm = RouteMapper.__new__(RouteMapper)
        rm.remote_names = ["core"]
        rm.database_names = []
        rm.config = {
            "remotes": ["core"],
            "settings": settings or {},
        }
        rm.settings = settings or {}
        return rm

    def _patch_config(self):
        remote_cfg = {"url": "https://api.example.com", "name": "core"}
        return [
            patch("api_dock.route_mapper.is_versioned_remote", return_value=False),
            patch("api_dock.route_mapper.is_route_allowed", return_value=True),
            patch("api_dock.route_mapper.find_remote_config", return_value=remote_cfg),
            patch(
                "api_dock.route_mapper.filter_remote_query_params",
                side_effect=lambda qp, *a, **kw: qp,
            ),
            patch("api_dock.route_mapper.find_route_mapping", return_value=None),
            patch("api_dock.route_mapper.filter_cookies_by_config", return_value={}),
        ]

    @pytest.mark.anyio
    async def test_returns_prepared_request_on_success(self):
        """Successful validation returns a PreparedRequest, not a ProxyResponse."""
        rm = self._make_route_mapper()
        patches = self._patch_config()
        for p in patches:
            p.start()
        try:
            result = await rm.prepare_remote_request("core", "detections/", "GET",
                                                     query_params={"confidence": ".5"})
        finally:
            for p in patches:
                p.stop()

        assert isinstance(result, PreparedRequest)
        assert result.url == "https://api.example.com/detections/"
        assert result.method == "GET"
        assert result.follow_redirects is True
        assert result.timeout == DEFAULT_TIMEOUT

    @pytest.mark.anyio
    async def test_returns_error_for_unknown_remote(self):
        rm = self._make_route_mapper()
        result = await rm.prepare_remote_request("nonexistent", "data/", "GET")
        assert isinstance(result, ProxyResponse)
        assert result.status_code == 404
        assert result.error_message is not None

    @pytest.mark.anyio
    async def test_follow_redirects_false_propagated(self):
        rm = self._make_route_mapper(settings={"follow_redirects": False})
        patches = self._patch_config()
        for p in patches:
            p.start()
        try:
            result = await rm.prepare_remote_request("core", "files/clip.wav", "GET")
        finally:
            for p in patches:
                p.stop()

        assert isinstance(result, PreparedRequest)
        assert result.follow_redirects is False

    @pytest.mark.anyio
    async def test_timeout_setting_propagated(self):
        rm = self._make_route_mapper(settings={"timeout": 30})
        patches = self._patch_config()
        for p in patches:
            p.start()
        try:
            result = await rm.prepare_remote_request("core", "data/", "GET")
        finally:
            for p in patches:
                p.stop()

        assert isinstance(result, PreparedRequest)
        assert result.timeout == 30.0

    @pytest.mark.anyio
    async def test_timeout_null_disables(self):
        """A null timeout setting disables the timeout (httpx timeout=None)."""
        rm = self._make_route_mapper(settings={"timeout": None})
        patches = self._patch_config()
        for p in patches:
            p.start()
        try:
            result = await rm.prepare_remote_request("core", "data/", "GET")
        finally:
            for p in patches:
                p.stop()

        assert isinstance(result, PreparedRequest)
        assert result.timeout is None


class TestResolveTimeout:
    """Tests for _resolve_timeout — config value → httpx timeout seconds."""

    def test_default_passthrough(self):
        assert _resolve_timeout(DEFAULT_TIMEOUT) == DEFAULT_TIMEOUT

    def test_integer_coerced_to_float(self):
        assert _resolve_timeout(30) == 30.0

    def test_none_disables(self):
        assert _resolve_timeout(None) is None

    def test_false_disables(self):
        assert _resolve_timeout(False) is None


class TestStreamUpstream:
    """Integration tests for _stream_upstream — the streaming proxy path.

    These exercise the actual fix for the two reported issues: large responses
    are streamed (not buffered) and compressed responses keep their
    Content-Encoding header because aiter_raw() forwards raw bytes.
    """

    def _make_prepared(self, follow_redirects=True):
        return PreparedRequest(
            url="https://api.example.com/detections/",
            method="GET",
            headers={},
            params={},
            cookies={},
            body=None,
            follow_redirects=follow_redirects,
        )

    def _make_streaming_client(self, status_code, chunks, headers):
        """Build a mock httpx.AsyncClient whose send(stream=True) streams chunks."""
        upstream = MagicMock()
        upstream.status_code = status_code
        upstream.headers = httpx.Headers(headers)

        async def _aiter_raw():
            for chunk in chunks:
                yield chunk

        upstream.aiter_raw = _aiter_raw
        upstream.aclose = AsyncMock()

        client = MagicMock()
        client.build_request = MagicMock(return_value=MagicMock())
        client.send = AsyncMock(return_value=upstream)
        client.aclose = AsyncMock()
        return client

    @pytest.mark.anyio
    async def test_content_encoding_preserved_and_body_streamed(self):
        """Issue 1: raw gzip bytes stream through and Content-Encoding survives."""
        gzip_chunks = [b"\x1f\x8b\x08\x00", b"raw-compressed-payload"]
        client = self._make_streaming_client(
            200,
            gzip_chunks,
            {
                "content-type": "application/json",
                "content-encoding": "gzip",
                "cache-control": "max-age=60",
            },
        )
        with patch("api_dock.fast_api.httpx.AsyncClient", return_value=client):
            response = await _stream_upstream(self._make_prepared())

        assert isinstance(response, StreamingResponse)
        assert response.status_code == 200
        assert response.headers.get("content-encoding") == "gzip"
        assert response.headers.get("cache-control") == "max-age=60"
        assert response.media_type == "application/json"

        body = b"".join([chunk async for chunk in response.body_iterator])
        assert body == b"".join(gzip_chunks)
        client.aclose.assert_awaited()

    @pytest.mark.anyio
    async def test_large_body_streamed_in_chunks(self):
        """Issue 2: a large body is yielded chunk-by-chunk, never buffered whole."""
        chunks = [b"x" * 65536 for _ in range(16)]  # 1 MiB across 16 chunks
        client = self._make_streaming_client(
            200, chunks, {"content-type": "application/octet-stream"}
        )
        with patch("api_dock.fast_api.httpx.AsyncClient", return_value=client):
            response = await _stream_upstream(self._make_prepared())

        received = [chunk async for chunk in response.body_iterator]
        assert len(received) == 16
        assert b"".join(received) == b"".join(chunks)

    @pytest.mark.anyio
    async def test_status_and_location_passed_through(self):
        """A 302 redirect (follow_redirects=False) forwards status + Location."""
        location = "https://s3.amazonaws.com/bucket/key?X-Amz-Signature=abc"
        client = self._make_streaming_client(
            302, [b""], {"content-type": "text/html", "location": location}
        )
        with patch("api_dock.fast_api.httpx.AsyncClient", return_value=client):
            response = await _stream_upstream(self._make_prepared(follow_redirects=False))

        assert response.status_code == 302
        assert response.headers.get("location") == location
        # Drain the body so the generator's finally runs and closes the client.
        _ = [chunk async for chunk in response.body_iterator]
        client.aclose.assert_awaited()

    @pytest.mark.anyio
    async def test_missing_content_type_defaults_to_octet_stream(self):
        """Upstream without a Content-Type falls back to application/octet-stream."""
        client = self._make_streaming_client(200, [b"data"], {})
        with patch("api_dock.fast_api.httpx.AsyncClient", return_value=client):
            response = await _stream_upstream(self._make_prepared())

        assert response.media_type == "application/octet-stream"
        _ = [chunk async for chunk in response.body_iterator]

    @pytest.mark.anyio
    async def test_connection_error_returns_502(self):
        """A connection failure before any bytes returns a plain 502 JSON Response."""
        client = MagicMock()
        client.build_request = MagicMock(return_value=MagicMock())
        client.send = AsyncMock(side_effect=httpx.ConnectError("refused"))
        client.aclose = AsyncMock()
        with patch("api_dock.fast_api.httpx.AsyncClient", return_value=client):
            response = await _stream_upstream(self._make_prepared())

        assert not isinstance(response, StreamingResponse)
        assert response.status_code == 502
        assert response.media_type == "application/json"
        parsed = json.loads(response.body)
        assert "error" in parsed
        client.aclose.assert_awaited()

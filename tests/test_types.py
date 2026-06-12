"""

Tests for the ProxyResponse and PreparedRequest types.

License: BSD 3-Clause

"""

#
# IMPORTS
#
from api_dock.types import PreparedRequest, ProxyResponse


#
# PUBLIC
#
class TestProxyResponse:
    """Tests for ProxyResponse dataclass."""

    def test_minimal_construction(self):
        resp = ProxyResponse(status_code=200, content=b"hello", content_type="text/plain")
        assert resp.status_code == 200
        assert resp.content == b"hello"
        assert resp.content_type == "text/plain"
        assert resp.headers == {}
        assert resp.error_message is None

    def test_with_headers(self):
        headers = {"cache-control": "max-age=3600", "etag": '"abc123"'}
        resp = ProxyResponse(
            status_code=200,
            content=b"{}",
            content_type="application/json",
            headers=headers,
        )
        assert resp.headers["cache-control"] == "max-age=3600"
        assert resp.headers["etag"] == '"abc123"'

    def test_error_response(self):
        resp = ProxyResponse(
            status_code=404,
            content=b'{"error": "not found"}',
            content_type="application/json",
            error_message="not found",
        )
        assert resp.status_code == 404
        assert resp.error_message == "not found"

    def test_headers_default_is_independent(self):
        """Each instance gets its own headers dict (dataclass field default_factory)."""
        r1 = ProxyResponse(status_code=200, content=b"", content_type="text/plain")
        r2 = ProxyResponse(status_code=200, content=b"", content_type="text/plain")
        r1.headers["x-custom"] = "value"
        assert "x-custom" not in r2.headers

    def test_binary_content(self):
        png_bytes = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
        resp = ProxyResponse(
            status_code=200,
            content=png_bytes,
            content_type="image/png",
        )
        assert resp.content == png_bytes
        assert resp.content_type == "image/png"

    def test_redirect_response(self):
        resp = ProxyResponse(
            status_code=302,
            content=b"",
            content_type="text/html",
            headers={"location": "https://s3.amazonaws.com/bucket/key?X-Amz-Signature=abc"},
        )
        assert resp.status_code == 302
        assert "location" in resp.headers


class TestPreparedRequest:
    """Tests for PreparedRequest dataclass."""

    def test_construction(self):
        req = PreparedRequest(
            url="https://api.example.com/data/",
            method="GET",
            headers={"accept": "application/json"},
            params={"count": "true"},
            cookies={"session": "abc"},
            body=None,
            follow_redirects=True,
        )
        assert req.url == "https://api.example.com/data/"
        assert req.method == "GET"
        assert req.headers == {"accept": "application/json"}
        assert req.params == {"count": "true"}
        assert req.cookies == {"session": "abc"}
        assert req.body is None
        assert req.follow_redirects is True

    def test_with_body(self):
        req = PreparedRequest(
            url="https://api.example.com/submit/",
            method="POST",
            headers={},
            params={},
            cookies={},
            body=b'{"key": "value"}',
            follow_redirects=False,
        )
        assert req.body == b'{"key": "value"}'
        assert req.follow_redirects is False

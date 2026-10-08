"""

Tests for the cookies sent to remotes, and the warning for ignored remote auth

The client's Cookie header is never forwarded as-is: the Cookie header sent
upstream holds only what the remote's ``cookies`` setting allows (forwarded
names and injected values), and nothing when it isn't set. Checked end to end
on the streaming (FastAPI), buffered (map_route) and Flask paths against the
``http_server`` fixture, which records the request headers it receives.
``authentication`` isn't applied to remote routes; startup logs a warning when
a config sets it where remotes would ignore it.

License: BSD 3-Clause

"""
#
# IMPORTS
#
import asyncio
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
import yaml
from fastapi.testclient import TestClient

from api_dock import fast_api, flask_api
from api_dock.route_mapper import RouteMapper


#
# CONSTANTS
#
CLIENT_COOKIES: Dict[str, str] = {"session": "s1", "tracking": "t1"}


#
# PUBLIC
#
class TestCookiesSentUpstream:
    """What reaches the remote for each ``cookies`` setting."""

    @pytest.mark.parametrize("setting, expected", [
        (None, None),
        (False, None),
        (True, {"session": "s1", "tracking": "t1"}),
        (["session"], {"session": "s1"}),
        (["other"], None),
        ([{"key": "injected", "value": "i1"}], {"injected": "i1"}),
        (["session", {"key": "injected", "value": "i1"}], {"session": "s1", "injected": "i1"}),
    ])
    def test_streaming(self, tmp_path: Path, http_server: Any, setting: Any,
                       expected: Optional[Dict[str, str]]) -> None:
        config = _config(tmp_path, http_server.url, setting)
        with TestClient(fast_api.create_app(config)) as client:
            client.cookies.update(CLIENT_COOKIES)
            assert client.get("/svc/items").status_code == 200
        assert _received_cookies(http_server) == expected

    @pytest.mark.parametrize("setting, expected", [
        (None, None),
        (["session"], {"session": "s1"}),
        (["session", {"key": "injected", "value": "i1"}], {"session": "s1", "injected": "i1"}),
    ])
    def test_buffered(self, tmp_path: Path, http_server: Any, setting: Any,
                      expected: Optional[Dict[str, str]]) -> None:
        mapper = RouteMapper(_config(tmp_path, http_server.url, setting))
        headers = {"Cookie": "session=s1; tracking=t1", "X-Other": "kept"}
        response = asyncio.run(mapper.map_route(
            "svc", "items", "GET", headers=headers, cookies=dict(CLIENT_COOKIES)
        ))
        assert response.status_code == 200
        assert _received_cookies(http_server) == expected
        assert http_server.requests[-1]["headers"]["X-Other"] == "kept"

    def test_flask(self, tmp_path: Path, http_server: Any) -> None:
        client = flask_api.create_app(_config(tmp_path, http_server.url, ["session"])).test_client()
        for name, value in CLIENT_COOKIES.items():
            client.set_cookie(name, value)
        assert client.get("/svc/items").status_code == 200
        assert _received_cookies(http_server) == {"session": "s1"}

    def test_injected_value_wins(self, tmp_path: Path, http_server: Any) -> None:
        setting = ["session", {"key": "session", "value": "server-side"}]
        with TestClient(fast_api.create_app(_config(tmp_path, http_server.url, setting))) as client:
            client.cookies.update(CLIENT_COOKIES)
            client.get("/svc/items")
        assert _received_cookies(http_server) == {"session": "server-side"}


class TestIgnoredRemoteAuthentication:
    """Startup warns when ``authentication`` would be ignored by remote routes."""

    def test_remote_with_authentication(self, tmp_path: Path, caplog: Any) -> None:
        config = _config(tmp_path, "https://example.org", None,
                         remote_extra={"authentication": {"key": "k", "value": "v"}})
        with caplog.at_level("WARNING"):
            RouteMapper(config)
        assert "Remote 'svc' has an `authentication` setting" in caplog.text

    def test_main_config_authentication(self, tmp_path: Path, caplog: Any) -> None:
        config = _config(tmp_path, "https://example.org", None,
                         main_extra={"authentication": {"key": "k", "value": "v"}})
        with caplog.at_level("WARNING"):
            RouteMapper(config)
        assert "applies to database routes only; remote routes (svc)" in caplog.text

    def test_versioned_remote(self, tmp_path: Path, caplog: Any) -> None:
        _write(tmp_path / "remotes" / "config.yaml", {"remotes": {"svc": {"versions": [
            {"version": "1.0", "url": "https://example.org"},
            {"version": "2.0", "url": "https://example.org",
             "authentication": {"key": "k", "value": "v"}},
        ]}}})
        _write(tmp_path / "config.yaml", {"name": "t", "remotes": ["svc"]})
        with caplog.at_level("WARNING"):
            RouteMapper(str(tmp_path / "config.yaml"))
        assert "Remote 'svc version 2.0'" in caplog.text
        assert "svc version 1.0" not in caplog.text

    def test_no_warning_without_authentication(self, tmp_path: Path, caplog: Any) -> None:
        with caplog.at_level("WARNING"):
            RouteMapper(_config(tmp_path, "https://example.org", ["session"]))
        assert "authentication" not in caplog.text


#
# INTERNAL
#
def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data))


def _config(root: Path, url: str, cookies: Any, remote_extra: Optional[Dict[str, Any]] = None,
            main_extra: Optional[Dict[str, Any]] = None) -> str:
    remote: Dict[str, Any] = {"name": "svc", "url": url, **(remote_extra or {})}
    if cookies is not None:
        remote["cookies"] = cookies
    _write(root / "remotes" / "svc.yaml", remote)
    _write(root / "config.yaml", {
        "name": "t", "remotes": ["svc"], "settings": {"add_trailing_slash": False},
        **(main_extra or {}),
    })
    return str(root / "config.yaml")


def _received_cookies(server: Any) -> Optional[Dict[str, str]]:
    header = {k.lower(): v for k, v in server.requests[-1]["headers"].items()}.get("cookie")
    if header is None:
        return None
    return dict(part.strip().split("=", 1) for part in header.split(";"))

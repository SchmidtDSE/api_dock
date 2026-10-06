"""

Tests for response headers on database routes.

A route's ``headers:`` maps header names to text templates that can use the
route's path variables. The headers are added to a successful response only.
A filled value with a control character or a non-ASCII character fails the
request with ``500 Resolver error``. Header names and template variables are
checked at startup.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

from api_dock.fast_api import create_app as create_fastapi_app
from api_dock.flask_api import create_app as create_flask_app
from api_dock.route_mapper import RouteMapper
from tests.conftest import write_config


#
# CONSTANTS
#
HEADERS: Dict[str, str] = {
    "ETag": '"item-{{id}}"',
    "X-Item-Id": "{{id}}",
    "X-Fixed": "fixed value",
}

RESOLVER_ERROR: Dict[str, str] = {"error": "Resolver error"}


#
# FIXTURES
#
def _route(headers: Any = None, **changes: Any) -> Dict[str, Any]:
    """Build an ``items/{{id}}`` route with headers.

    Args:
        headers: The route's ``headers:`` value; HEADERS if None.
        **changes: Route keys to add or replace.

    Returns:
        Route config.
    """
    route: Dict[str, Any] = {
        "route": "items/{{id}}",
        "sql": "SELECT {{id}} AS id",
        "headers": HEADERS if headers is None else headers,
    }
    route.update(changes)
    return route


def _config_path(tmp_path: Path, route: Optional[Dict[str, Any]] = None) -> str:
    """Write a config with one versioned DuckDB database ``shop``.

    Args:
        tmp_path: Pytest temp directory.
        route: The database's only route; _route() if None.

    Returns:
        Path of config.yaml.
    """
    config = {"name": "shop", "routes": [route or _route()]}
    return write_config(tmp_path, {"shop": {"versions": {"1.0": config}}})


async def _get(mapper: RouteMapper, path: str, **query: str) -> Any:
    """Send a database request to ``shop``.

    Args:
        mapper: Route mapper.
        path: Path after the database name.
        **query: Query params.

    Returns:
        The ProxyResponse.
    """
    return await mapper.map_database_route(database_name="shop", path=path, query_params=query)


#
# PUBLIC
#
class TestSuccessfulResponse:
    """Headers are filled from path variables and added to a successful response."""

    @pytest.mark.anyio
    async def test_direct_call(self, tmp_path: Path) -> None:
        """ProxyResponse.headers carries the filled headers, unchanged and unquoted."""
        response = await _get(RouteMapper(_config_path(tmp_path)), "1.0/items/7")
        assert response.status_code == 200
        assert json.loads(response.content) == [{"id": "7"}]
        assert response.headers == {"ETag": '"item-7"', "X-Item-Id": "7", "X-Fixed": "fixed value"}

    def test_fastapi(self, tmp_path: Path) -> None:
        """FastAPI sends the headers."""
        response = TestClient(create_fastapi_app(_config_path(tmp_path))).get("/shop/1.0/items/7")
        assert response.status_code == 200
        assert response.headers["etag"] == '"item-7"'
        assert response.headers["x-item-id"] == "7"

    def test_flask(self, tmp_path: Path) -> None:
        """Flask sends the headers."""
        response = create_flask_app(_config_path(tmp_path)).test_client().get("/shop/1.0/items/7")
        assert response.status_code == 200
        assert response.headers["ETag"] == '"item-7"'
        assert response.headers["X-Item-Id"] == "7"

    def test_if_none_match_is_ignored(self, tmp_path: Path) -> None:
        """A matching If-None-Match still gets 200 and the body, never 304."""
        client = TestClient(create_fastapi_app(_config_path(tmp_path)))
        response = client.get("/shop/1.0/items/7", headers={"If-None-Match": '"item-7"'})
        assert response.status_code == 200
        assert response.json() == [{"id": "7"}]

    @pytest.mark.anyio
    async def test_value_with_quotes_and_braces_is_not_scanned(self, tmp_path: Path) -> None:
        """A path value is written as it is; {{x}} in it is not filled again."""
        response = await _get(RouteMapper(_config_path(tmp_path)), "1.0/items/{{id}}'x")
        assert response.status_code == 200
        assert response.headers["X-Item-Id"] == "{{id}}'x"


class TestErrorResponses:
    """Error responses have no headers: values."""

    @pytest.mark.anyio
    async def test_query_error(self, tmp_path: Path) -> None:
        """A failed query returns its error without the headers."""
        route = _route(sql="SELECT * FROM missing_table WHERE id = {{id}}")
        response = await _get(RouteMapper(_config_path(tmp_path, route)), "1.0/items/7")
        assert response.status_code == 500
        assert response.headers == {}

    @pytest.mark.anyio
    async def test_early_response(self, tmp_path: Path) -> None:
        """A missing required param returns 400 without the headers."""
        route = _route(query_params=[{"q": {"sql": "{{q}} = {{q}}", "required": True}}])
        response = await _get(RouteMapper(_config_path(tmp_path, route)), "1.0/items/7")
        assert response.status_code == 400
        assert response.headers == {}

    @pytest.mark.anyio
    async def test_route_list_and_unknown_route(self, tmp_path: Path) -> None:
        """The route list and an unknown route have no headers."""
        mapper = RouteMapper(_config_path(tmp_path))
        assert (await _get(mapper, "1.0")).headers == {}
        assert (await _get(mapper, "1.0/nope")).headers == {}


class TestRefusedValues:
    """A control or non-ASCII character in a filled value gives 500 Resolver error."""

    @pytest.mark.anyio
    @pytest.mark.parametrize("value", ["a\nb", "a\rb", "a\x00b", "a\tb", "a\x7fb", "café"])
    async def test_refused(self, tmp_path: Path, value: str) -> None:
        """The response is 500 Resolver error, with no headers."""
        response = await _get(RouteMapper(_config_path(tmp_path)), f"1.0/items/{value}")
        assert response.status_code == 500
        assert json.loads(response.content) == RESOLVER_ERROR
        assert response.headers == {}

    @pytest.mark.anyio
    async def test_log_names_route_and_header(
            self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """The server log names the database, the route and the header."""
        caplog.set_level(logging.WARNING, logger="api_dock.route_mapper")
        await _get(RouteMapper(_config_path(tmp_path)), "1.0/items/a\nb")
        assert "shop" in caplog.text
        assert "items/{{id}}" in caplog.text
        assert "ETag" in caplog.text

    # The adapters' path converters don't match a line feed, so it can't
    # reach api_dock through HTTP; a tab and a non-ASCII character can.
    @pytest.mark.parametrize("value", ["a%09b", "caf%C3%A9"])
    def test_through_fastapi(self, tmp_path: Path, value: str) -> None:
        """FastAPI returns 500 Resolver error without the headers."""
        response = TestClient(create_fastapi_app(_config_path(tmp_path))).get(
            f"/shop/1.0/items/{value}"
        )
        assert (response.status_code, response.json()) == (500, RESOLVER_ERROR)
        assert "x-item-id" not in response.headers

    @pytest.mark.parametrize("value", ["a%09b", "caf%C3%A9"])
    def test_through_flask(self, tmp_path: Path, value: str) -> None:
        """Flask returns 500 Resolver error without the headers."""
        response = create_flask_app(_config_path(tmp_path)).test_client().get(
            f"/shop/1.0/items/{value}"
        )
        assert (response.status_code, response.get_json()) == (500, RESOLVER_ERROR)
        assert "X-Item-Id" not in response.headers


class TestStartupCheck:
    """Bad headers: settings stop startup with the database, version, route and reason."""

    @pytest.mark.parametrize("name", [
        "Bad Header", "X:Y", "", "X-é", "X(1)",
    ])
    def test_name_not_a_token(self, tmp_path: Path, name: str) -> None:
        """A header name that is not an HTTP token is refused."""
        self._assert_refused(tmp_path, {name: "x"}, ["header name"])

    @pytest.mark.parametrize("name", [
        "Connection", "transfer-encoding", "Content-Type", "content-length",
        "SET-COOKIE", "Keep-Alive", "Upgrade", "Content-Encoding",
    ])
    def test_reserved_name(self, tmp_path: Path, name: str) -> None:
        """Hop-by-hop headers, Content-Type, Content-Length and Set-Cookie are refused."""
        self._assert_refused(tmp_path, {name: "x"}, [name])

    @pytest.mark.parametrize("template", ["{{q}}", "{{cookies.session}}", "{{other}}"])
    def test_variable_not_a_path_variable(self, tmp_path: Path, template: str) -> None:
        """A query param, a cookie or an unknown name is refused."""
        route = _route(
            headers={"X-Value": template}, query_params=[{"q": {"default": "a"}}],
        )
        self._assert_refused(tmp_path, route=route, expected=["X-Value", template[2:-2]])

    @pytest.mark.parametrize("headers", [["ETag"], "ETag: x", {"X-Count": 5}, {"X-None": None}])
    def test_wrong_shape(self, tmp_path: Path, headers: Any) -> None:
        """headers: must map names to text."""
        self._assert_refused(tmp_path, headers, ["headers"])

    def test_valid_names_accepted(self, tmp_path: Path) -> None:
        """Token characters and mixed case are accepted."""
        headers = {"X-A_b.c!#$%&'*+^`|~9": "x", "etag": "{{id}}", "Cache-Control": "no-store"}
        RouteMapper(_config_path(tmp_path, _route(headers=headers)))

    def _assert_refused(
            self, tmp_path: Path, headers: Any = None,
            expected: Optional[List[str]] = None, route: Optional[Dict[str, Any]] = None) -> None:
        """Assert that startup stops and the error has the expected text.

        Args:
            tmp_path: Pytest temp directory.
            headers: The route's headers: value, used when route is None.
            expected: Text the error must contain, besides database, version and route.
            route: Complete route config.
        """
        with pytest.raises(ValueError) as error:
            RouteMapper(_config_path(tmp_path, route or _route(headers=headers)))
        message = str(error.value)
        for text in ["shop", "1.0", "items/{{id}}"] + (expected or []):
            assert text in message

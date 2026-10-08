"""

Shared pytest fixtures for API Dock tests.

``postgres_server`` runs a throwaway PostgreSQL server for the session (from
the dev environment's conda-forge ``postgresql``). Tests that use it are
skipped when PostgreSQL or the psycopg driver isn't available. ``http_server``
serves JSON from a local ``http.server`` (for HTTP lookups).

License: BSD 3-Clause

"""
#
# IMPORTS
#
import json
import shutil
import socket
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest


#
# CONSTANTS
#
POSTGRES_USER: str = "api_dock_test"
POSTGRES_DATABASE: str = "api_dock_test"


#
# PUBLIC
#
@pytest.fixture(scope="session")
def postgres_server() -> Iterator[Dict[str, str]]:
    """Start a local PostgreSQL server for the test session.

    Yields:
        libpq connection fields (host, port, user, dbname) for a database the
        tests may create tables in. The server and its data are removed after
        the session.
    """
    pytest.importorskip("psycopg")
    pytest.importorskip("psycopg_pool")
    if not (shutil.which("initdb") and shutil.which("pg_ctl")):
        pytest.skip("PostgreSQL (initdb/pg_ctl) is not installed")

    root = Path(tempfile.mkdtemp(prefix="api_dock_pg_"))
    data, port = root / "data", str(_free_port())
    subprocess.run(
        ["initdb", "-D", str(data), "-U", POSTGRES_USER, "--auth=trust", "-E", "UTF8"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["pg_ctl", "-D", str(data), "-w", "-l", str(root / "postgres.log"), "-o",
         f"-p {port} -k {root} -c listen_addresses=127.0.0.1", "start"],
        check=True, capture_output=True,
    )
    fields = {"host": "127.0.0.1", "port": port, "user": POSTGRES_USER}
    try:
        _run_sql({**fields, "dbname": "postgres"}, f"CREATE DATABASE {POSTGRES_DATABASE}")
        yield {**fields, "dbname": POSTGRES_DATABASE}
    finally:
        subprocess.run(["pg_ctl", "-D", str(data), "-m", "immediate", "stop"],
                       capture_output=True)
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def http_server() -> Iterator[Any]:
    """A local HTTP server returning ``server.body`` as JSON with ``server.status``."""

    class State:
        body: Any = []
        status: int = 200
        requests: List[Dict[str, Any]] = []
        url: str = ""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            State.requests.append({"path": self.path, "headers": dict(self.headers)})
            payload = State.body if isinstance(State.body, str) else json.dumps(State.body)
            self.send_response(State.status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload.encode())

        def log_message(self, *args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    State.url = f"http://127.0.0.1:{server.server_address[1]}"
    State.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield State
    finally:
        server.shutdown()
        server.server_close()


#
# INTERNAL
#
def _free_port() -> int:
    """Return a free TCP port on 127.0.0.1."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _run_sql(fields: Dict[str, str], sql: str) -> None:
    """Run SQL with autocommit (needed for CREATE DATABASE)."""
    import psycopg

    with psycopg.connect(**fields, autocommit=True) as conn:
        conn.execute(sql)

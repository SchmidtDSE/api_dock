"""

Shared pytest fixtures for API Dock tests.

The ``postgres_server`` fixture runs one local PostgreSQL server for the whole
test session, from the ``postgresql`` package in the pixi dev environment. It
listens on a free TCP port on 127.0.0.1, keeps its files in a temporary folder
and is stopped when the session ends. Tests fail, rather than skip, when the
PostgreSQL programs are missing.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

import pytest
import yaml


#
# CONSTANTS
#
ADMIN_USER: str = "admin"
ADMIN_PASSWORD: str = "admin_password"
READER_USER: str = "reader"
READER_PASSWORD: str = "reader_password"
DATABASE_NAME: str = "api_dock_test"

# Test tables. The reader login may only SELECT, except on catalog.scratch,
# where it may also DELETE, so a failed DELETE there shows the read-only
# setting at work. "order" is a reserved word.
SETUP_SQL: str = """
CREATE SCHEMA catalog;
CREATE TABLE catalog.items (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    weight NUMERIC,
    added DATE
);
INSERT INTO catalog.items VALUES
    (1, 'apple', 1.5, '2026-01-02'),
    (2, 'O''Brien', 2.0, '2026-02-03'),
    (3, '50% off', 3.25, '2026-03-04'),
    (4, '%s literal', 4.0, '2026-04-05');
CREATE TABLE catalog."order" (id INTEGER PRIMARY KEY, label TEXT);
INSERT INTO catalog."order" VALUES (1, 'first');
CREATE TABLE public.notes (id INTEGER PRIMARY KEY, body TEXT);
INSERT INTO public.notes VALUES (1, 'unqualified');
CREATE TABLE catalog.scratch (id INTEGER PRIMARY KEY);
INSERT INTO catalog.scratch VALUES (1), (2);
CREATE TABLE catalog.kinds (
    id INTEGER, u UUID, n NUMERIC, d DATE, ts TIMESTAMP, tstz TIMESTAMPTZ,
    j JSON, jb JSONB, iv INTERVAL, ip INET, net CIDR,
    uuids UUID[], nums NUMERIC[], dates DATE[], tstzs TIMESTAMPTZ[],
    grid INTEGER[][], with_null TEXT[]
);
INSERT INTO catalog.kinds VALUES (
    1, 'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11', 1.25, '2026-01-02',
    '2026-01-02 03:04:05', '2026-01-02 03:04:05+02',
    '{"a": [1, 2]}', '{"b": {"c": null}}', '1 day 2 hours', '192.168.0.1', '10.0.0.0/8',
    ARRAY['a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11'::uuid], ARRAY[1.5, 2.25],
    ARRAY['2026-01-02'::date], ARRAY['2026-01-02 03:04:05+02'::timestamptz],
    ARRAY[[1, 2], [3, 4]], ARRAY['x', NULL]
);
CREATE ROLE reader LOGIN PASSWORD 'reader_password';
GRANT USAGE ON SCHEMA catalog TO reader;
GRANT SELECT ON ALL TABLES IN SCHEMA catalog TO reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO reader;
GRANT DELETE ON catalog.scratch TO reader;
"""


#
# FIXTURES
#
class PostgresServer:
    """A local PostgreSQL server that a test can stop and start again.

    Attributes:
        host: Address the server listens on.
        port: TCP port the server listens on.
        data_dir: The server's data folder.
    """

    def __init__(self, root: Path) -> None:
        """Create a data folder and choose a port. The server is not started.

        Args:
            root: Empty temporary folder for the server's files.
        """
        self.host = "127.0.0.1"
        self.port = _free_port()
        self.data_dir = root / "data"
        self._log_file = root / "server.log"
        self._programs = _find_programs(["initdb", "pg_ctl"])
        password_file = root / "admin_password"
        password_file.write_text(ADMIN_PASSWORD)
        self._run([
            self._programs["initdb"], "-D", str(self.data_dir), "-U", ADMIN_USER,
            "--auth=scram-sha-256", f"--pwfile={password_file}", "--encoding=UTF8", "--no-locale",
        ])

    def start(self) -> None:
        """Start the server and wait until it accepts connections."""
        options = (
            f"-p {self.port} -c listen_addresses={self.host} "
            "-c unix_socket_directories=''"
        )
        self._run([
            self._programs["pg_ctl"], "-D", str(self.data_dir), "-l", str(self._log_file),
            "-o", options, "-w", "start",
        ])

    def stop(self) -> None:
        """Stop the server at once, closing every connection."""
        self._run([self._programs["pg_ctl"], "-D", str(self.data_dir), "-m", "immediate", "stop"])

    def is_running(self) -> bool:
        """Check whether the server is running.

        Returns:
            True if pg_ctl reports a running server.
        """
        result = subprocess.run(
            [self._programs["pg_ctl"], "-D", str(self.data_dir), "status"],
            capture_output=True,
        )
        return result.returncode == 0

    def connection(
            self, user: str = READER_USER, password: str = READER_PASSWORD) -> Dict[str, str]:
        """Return a ``connection:`` mapping for a database config.

        Args:
            user: Login name.
            password: Login password.

        Returns:
            libpq connection fields for the test database.
        """
        return {
            "host": self.host, "port": str(self.port), "dbname": DATABASE_NAME,
            "user": user, "password": password, "sslmode": "disable",
        }

    def database_config(self, **changes: Any) -> Dict[str, Any]:
        """Return a PostgreSQL database config for the test database.

        Args:
            **changes: Top-level keys to add or replace.

        Returns:
            Database config with ``backend: postgres`` and the reader login.
        """
        config: Dict[str, Any] = {
            "name": "shop", "backend": "postgres", "connection": self.connection(),
            "tables": {"items": "catalog.items"}, "routes": [],
        }
        config.update(changes)
        return config

    def admin_query(self, sql: str) -> List[Tuple[Any, ...]]:
        """Run SQL as the administrative login and return its rows.

        Args:
            sql: SQL text.

        Returns:
            Rows, or an empty list for statements without results.
        """
        import psycopg

        with psycopg.connect(self.admin_conninfo(), autocommit=True) as conn:
            cursor = conn.execute(sql)
            return cursor.fetchall() if cursor.description else []

    def admin_conninfo(self, dbname: str = DATABASE_NAME) -> str:
        """Return a connection string for the administrative login.

        Args:
            dbname: Database to connect to.

        Returns:
            libpq connection string.
        """
        return (
            f"host={self.host} port={self.port} dbname={dbname} "
            f"user={ADMIN_USER} password={ADMIN_PASSWORD} sslmode=disable"
        )

    def _run(self, command: List[str]) -> None:
        """Run a PostgreSQL program and fail the test with its output if it fails.

        Args:
            command: Program and arguments.
        """
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            log = self._log_file.read_text() if self._log_file.exists() else ""
            pytest.fail(f"{command[0]} failed:\n{result.stdout}{result.stderr}{log}")


@pytest.fixture(scope="session")
def postgres_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[PostgresServer]:
    """Run a PostgreSQL server with a test database for the whole session.

    The server is stopped when the session ends, including after failures.

    Args:
        tmp_path_factory: Pytest temporary folder factory.

    Yields:
        The running server.
    """
    server = PostgresServer(tmp_path_factory.mktemp("postgres"))
    server.start()
    try:
        _create_test_database(server)
        yield server
    finally:
        if server.is_running():
            server.stop()


@pytest.fixture
def running_postgres(postgres_server: PostgresServer) -> Iterator[PostgresServer]:
    """Give a test the session server and make sure it is running afterwards.

    Tests that stop the server use this fixture, so later tests still find it
    running.

    Args:
        postgres_server: The session server.

    Yields:
        The running server.
    """
    yield postgres_server
    if not postgres_server.is_running():
        postgres_server.start()


def write_config(
        root: Path,
        databases: Dict[str, Any],
        expose: Any = None) -> str:
    """Write a main config and database configs, and return the main config's path.

    Args:
        root: Folder to write ``config.yaml`` and ``databases/`` into.
        databases: Database name to its config, or to ``{"versions": {version:
            config}}`` for a versioned database.
        expose: Optional ``expose`` setting for catalog listings.

    Returns:
        Path of config.yaml.
    """
    main: Dict[str, Any] = {"name": "test", "databases": list(databases)}
    if expose is not None:
        main["expose"] = expose
    write_yaml(root / "config.yaml", main)
    for name, config in databases.items():
        if "versions" in config:
            for version, version_config in config["versions"].items():
                write_yaml(root / "databases" / name / f"{version}.yaml", version_config)
        else:
            write_yaml(root / "databases" / f"{name}.yaml", config)
    return str(root / "config.yaml")


def write_yaml(path: Path, data: Any) -> None:
    """Write data to a YAML file, creating its folder.

    Args:
        path: File path.
        data: Data to write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data))


#
# INTERNAL
#
def _free_port() -> int:
    """Find a TCP port on 127.0.0.1 that nothing is listening on.

    Returns:
        Port number.
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _find_programs(names: List[str]) -> Dict[str, str]:
    """Find PostgreSQL programs on the PATH, failing the test if one is missing.

    Args:
        names: Program names.

    Returns:
        Program name to full path.
    """
    found = {name: shutil.which(name) for name in names}
    missing = [name for name, path in found.items() if path is None]
    if missing:
        pytest.fail(
            f"PostgreSQL programs not found: {', '.join(missing)}. "
            "Run the tests with `pixi run -e dev pytest`."
        )
    return found


def _create_test_database(server: PostgresServer) -> None:
    """Create the test database, its tables and the reader login.

    Args:
        server: The running server.
    """
    import psycopg

    with psycopg.connect(server.admin_conninfo("postgres"), autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE {DATABASE_NAME}")
    with psycopg.connect(server.admin_conninfo(), autocommit=True) as conn:
        conn.execute(SETUP_SQL)

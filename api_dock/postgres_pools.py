"""

PostgreSQL Pools Module for API Dock

Opens and closes the connection pools of a route mapper's PostgreSQL database
versions, and returns the backend for each version. start() opens the pools and
aclose() closes them, on one event loop. start() imports the PostgreSQL driver,
so this module can be imported without the driver installed.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import asyncio
import importlib
import logging
from types import ModuleType
from typing import Any, Dict, List, Optional, Tuple

from api_dock.database_backends import DatabaseBackend, DatabaseLifecycleError


#
# CONSTANTS
#
logger: logging.Logger = logging.getLogger(__name__)

# A database version: (database name, version, or None if unversioned).
DatabaseKey = Tuple[str, Optional[str]]

NOT_STARTED: str = "not started"
STARTED: str = "started"
CLOSED: str = "closed"

POSTGRES_BACKEND_MODULE: str = "api_dock.postgres_backend"


#
# PUBLIC
#
def database_label(key: DatabaseKey) -> str:
    """Format a (database name, optional version) key for error and log messages."""
    name, version = key
    return f"Database '{name}'" if version is None else f"Database '{name}' version '{version}'"


class PostgresPools:
    """The connection pools of a route mapper's PostgreSQL database versions.

    Pools are created and opened by start() and closed by aclose(), on the
    event loop that called start(). A closed instance can't be started again.
    """

    def __init__(self, configs: Dict[DatabaseKey, Dict[str, Any]]) -> None:
        """Retain checked configs by database/version without opening any pools."""
        self._configs = configs
        self._backends: Dict[DatabaseKey, DatabaseBackend] = {}
        self._pools: List[Any] = []
        self._state = NOT_STARTED
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    async def start(self) -> None:
        """Check settings for every database, then open a pool for each and probe it.

        Every config's settings are built before any pool is created, so a
        config error stops startup with nothing opened. A database that can't
        be reached is logged as a warning, and its pool keeps trying.

        Raises:
            RuntimeError: If the PostgreSQL dependencies are not installed.
            ValueError: If a database's connection settings are invalid.
            DatabaseLifecycleError: If already closed, or started on another
                event loop.
        """
        if self._state == STARTED:
            self._check_loop("started")
            return
        if self._state == CLOSED:
            raise DatabaseLifecycleError(
                "This route mapper is closed and can't be started again; create a new RouteMapper"
            )
        self._loop = asyncio.get_running_loop()
        if self._configs:
            module = _import_postgres_backend()
            settings = {
                key: _build_settings(module, key, config) for key, config in self._configs.items()
            }
            await self._open_pools(module, settings)
        self._state = STARTED

    async def aclose(self) -> None:
        """Close every pool and stop its background tasks. Safe to call more than once."""
        if self._state == CLOSED:
            return
        if self._pools:
            self._check_loop("closed")
        self._state = CLOSED
        await self._close_pools()

    def backend(self, key: DatabaseKey) -> DatabaseBackend:
        """Return the backend for a concrete database version.

        Raise DatabaseLifecycleError before startup, after closure or on another loop.
        """
        label = database_label(key)
        if self._state == NOT_STARTED:
            raise DatabaseLifecycleError(
                f"{label} uses PostgreSQL, but the route mapper was not started. Call "
                "`await mapper.start()` first; the FastAPI app does this in its lifespan."
            )
        if self._state == CLOSED:
            raise DatabaseLifecycleError(f"{label} can't be used: the route mapper is closed")
        self._check_loop("used")
        return self._backends[key]

    async def _open_pools(self, module: ModuleType, settings: Dict[DatabaseKey, Any]) -> None:
        """Open each pool, then probe them concurrently.

        If any step fails, close all pools created so far.
        """
        try:
            for key, pool_settings in settings.items():
                pool = module.create_pool(pool_settings, name=_pool_name(key))
                self._pools.append(pool)
                await pool.open(wait=False)
                self._backends[key] = module.PostgresBackend(pool)
            await self._probe_all(module, settings)
        except BaseException:
            self._state = CLOSED
            await self._close_pools()
            raise

    async def _probe_all(self, module: ModuleType, settings: Dict[DatabaseKey, Any]) -> None:
        """Probe all pools concurrently and warn for each that times out."""
        keys = list(self._backends)
        results = await asyncio.gather(*(
            module.probe_pool(self._backends[key].pool, settings[key].startup_timeout)
            for key in keys
        ))
        for key, connected in zip(keys, results):
            if not connected:
                logger.warning(
                    "%s: No connection obtained within %ss; PostgreSQL routes will return 503 "
                    "until a connection is available", database_label(key),
                    settings[key].startup_timeout,
                )

    async def _close_pools(self) -> None:
        """Close every pool created so far."""
        pools, self._pools, self._backends = self._pools, [], {}
        for pool in pools:
            await pool.close()

    def _check_loop(self, action: str) -> None:
        """Raise DatabaseLifecycleError if used outside the loop that started the pools."""
        if asyncio.get_running_loop() is not self._loop:
            raise DatabaseLifecycleError(
                f"PostgreSQL pools can't be {action} from an event loop other than the one "
                "that started them; use the route mapper on one event loop"
            )


#
# INTERNAL
#
def _import_postgres_backend() -> ModuleType:
    """Import the optional driver and pool dependencies.

    Raise RuntimeError with installation guidance if no usable driver is available.
    """
    try:
        return importlib.import_module(POSTGRES_BACKEND_MODULE)
    except ImportError as error:
        reason = f"'{error.name}' could not be imported" if error.name else "psycopg found no libpq"
        raise RuntimeError(
            "PostgreSQL databases need the optional dependencies: "
            f"pip install 'api_dock[postgres]' ({reason})"
        ) from None


def _build_settings(module: ModuleType, key: DatabaseKey, config: Dict[str, Any]) -> Any:
    """Build settings, adding the database/version to any configuration error."""
    try:
        return module.build_settings(config)
    except ValueError as error:
        raise ValueError(f"{database_label(key)}, {error}") from None


def _pool_name(key: DatabaseKey) -> str:
    """Return name/version for a versioned database, otherwise name."""
    name, version = key
    return name if version is None else f"{name}/{version}"

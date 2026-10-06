"""

Tests for the default apps being built on first use.

Importing api_dock, ``api_dock.fast_api`` or ``api_dock.flask_api`` builds no
app. The first read of ``app``, ``fastapi_app`` or ``flask_app`` builds it, and
later reads return the same object. Each test runs in a new Python process,
because the test session has already imported api_dock.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import subprocess
import sys
import textwrap
from pathlib import Path


#
# PUBLIC
#
class TestLazyApps:
    """Default apps are built on first read and then reused."""

    def test_import_builds_no_app(self, tmp_path: Path) -> None:
        """Importing the package, both app modules and the factories builds no app."""
        _run_python(tmp_path, """
            import fastapi
            import flask

            def refuse(*args, **kwargs):
                raise AssertionError("an app was built during import")

            fastapi.FastAPI.__init__ = refuse
            flask.Flask.__init__ = refuse

            import api_dock
            import api_dock.fast_api
            import api_dock.flask_api
            from api_dock import RouteMapper, create_app, create_fastapi_app, create_flask_app
        """)

    def test_first_read_builds_each_app_once(self, tmp_path: Path) -> None:
        """Each default app is built on its first read; aliases share it."""
        _run_python(tmp_path, """
            import api_dock
            from api_dock import fast_api, flask_api

            builds = []
            fastapi_factory = fast_api.create_app
            flask_factory = flask_api.create_app
            fast_api.create_app = lambda: builds.append("fastapi") or fastapi_factory()
            flask_api.create_app = lambda: builds.append("flask") or flask_factory()

            app = api_dock.app
            assert builds == ["fastapi"], builds
            assert api_dock.fastapi_app is app
            assert fast_api.app is app
            from api_dock.fast_api import app as imported_app
            assert imported_app is app

            flask_app = api_dock.flask_app
            assert flask_app is flask_api.app
            from api_dock.flask_api import app as imported_flask_app
            assert imported_flask_app is flask_app
            assert builds == ["fastapi", "flask"], builds
        """)

    def test_factories_return_new_apps(self, tmp_path: Path) -> None:
        """Factory calls return a new app each time, not the default app."""
        _run_python(tmp_path, """
            import api_dock

            assert api_dock.create_app is api_dock.create_fastapi_app
            assert api_dock.create_app() is not api_dock.app
            assert api_dock.create_app() is not api_dock.create_app()
            assert api_dock.create_flask_app() is not api_dock.flask_app
        """)

    def test_uvicorn_import_string_resolves(self, tmp_path: Path) -> None:
        """uvicorn's api_dock.fast_api:app import string returns the default app."""
        _run_python(tmp_path, """
            from fastapi import FastAPI
            from uvicorn.importer import import_from_string

            import api_dock.fast_api

            app = import_from_string("api_dock.fast_api:app")
            assert isinstance(app, FastAPI)
            assert app is api_dock.fast_api.app
        """)

    def test_unknown_attribute_raises(self, tmp_path: Path) -> None:
        """Reading a name that doesn't exist raises AttributeError."""
        _run_python(tmp_path, """
            import api_dock
            import api_dock.fast_api
            import api_dock.flask_api

            for module in [api_dock, api_dock.fast_api, api_dock.flask_api]:
                try:
                    module.no_such_name
                except AttributeError:
                    pass
                else:
                    raise AssertionError(f"{module.__name__}.no_such_name exists")
        """)


class TestLazyAppsWithPostgres:
    """With a PostgreSQL database configured, only reading the Flask app fails."""

    def test_flask_app_read_raises(self, tmp_path: Path) -> None:
        """Importing and the FastAPI app work; reading flask_app raises the Flask error."""
        _write_postgres_config(tmp_path)
        _run_python(tmp_path, """
            import fastapi
            import api_dock
            from api_dock import fast_api, postgres_pools

            def refuse(*args, **kwargs):
                raise AssertionError("a pool was opened")

            postgres_pools.PostgresPools.start = refuse
            assert isinstance(api_dock.app, fastapi.FastAPI)
            from uvicorn.importer import import_from_string
            assert import_from_string("api_dock.fast_api:app") is api_dock.app
            import api_dock.flask_api

            for read in (lambda: api_dock.flask_app, lambda: api_dock.flask_api.app):
                try:
                    read()
                except RuntimeError as error:
                    assert "use --backbone fastapi" in str(error), error
                else:
                    raise AssertionError("the Flask app was built")
        """)


#
# INTERNAL
#
def _write_postgres_config(root: Path) -> None:
    """Write an api_dock_config folder with one PostgreSQL database.

    Args:
        root: Folder to write api_dock_config into.
    """
    folder = root / "api_dock_config"
    (folder / "databases").mkdir(parents=True)
    (folder / "config.yaml").write_text("name: test\ndatabases: [shop]\n")
    (folder / "databases" / "shop.yaml").write_text(textwrap.dedent("""
        name: shop
        backend: postgres
        connection: {host: 127.0.0.1, dbname: shop, user: reader}
        routes: [{route: items, sql: SELECT 1}]
    """))


def _run_python(cwd: Path, code: str) -> None:
    """Run code in a new Python process and fail the test if it fails.

    Args:
        cwd: Directory to run in. Unless a test writes an api_dock_config
            there, the bundled example config is used.
        code: Python code to run.
    """
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=cwd, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr

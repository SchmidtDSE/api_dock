"""

CLI Module for API Dock

Click-based command-line interface for API Dock operations.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import json
import os
import socket
import sys
from pathlib import Path
from typing import Any, Optional, Tuple

import click
import uvicorn
import yaml

from api_dock.config import load_main_config
from api_dock.config_discovery import find_config, init_config
from api_dock.config import find_remote_config, get_remote_versions, is_versioned_remote
from api_dock.database_config import (
    apply_shared_definitions,
    get_database_versions,
    get_schema_sources,
    is_versioned_database,
    load_database_config,
    load_shared_config,
    merge_query_params,
    SCHEMA_GROUPS_KEY,
    SHARED_CONFIG_KEY,
)
from api_dock.fast_api import create_app as create_fastapi_app
from api_dock.flask_api import create_app as create_flask_app
from api_dock.lookups import load_lookup_specs, LookupContext, run_lookup
from api_dock.route_mapper import RouteMapper
from api_dock.sql_builder import build_sql_query, VARIABLE_PATTERN
from api_dock.types import SqlContext


#
# CONSTANTS
#
DEFAULT_HOST: str = "0.0.0.0"
DEFAULT_PORT: int = 8000
DEFAULT_BACKBONE: str = "fastapi"
MAX_PORT_RETRIES: int = 4
DEFAULT_LOOKUP_ROWS: int = 5
ADMIN_TOKEN_ENV: str = "API_DOCK_ADMIN_TOKEN"


#
# PUBLIC
#
@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx: click.Context) -> None:
    """API Dock - API wrapper using configuration files.

    Run without command to see available configurations.
    """
    if ctx.invoked_subcommand is None:
        _list_configs()


@cli.command()
@click.option("--force", "-f", is_flag=True,
              help="Run even if the folder has configs, replacing files from the example")
def init(force: bool) -> None:
    """Initialize api_dock_config/ directory with default configurations.

    Copies configuration files from the package to api_dock_config/.
    """
    config_dir = Path("api_dock_config")

    # Check if directory exists and has files
    if config_dir.exists() and not force:
        config_files = list(config_dir.glob("*.yaml"))
        if config_files:
            click.echo(f"Error: {config_dir}/ already contains configuration files.", err=True)
            click.echo("Use --force to overwrite.", err=True)
            sys.exit(1)

    # Initialize configuration
    click.echo(f"Initializing {config_dir}/...")

    written = init_config(overwrite=force)
    if written is None:
        click.echo("Error: Failed to initialize configuration", err=True)
        sys.exit(1)
    for path in written:
        click.echo(f"✓ {config_dir}/{path}")
    if not written:
        click.echo("All example files already exist (use --force to replace them).")
    click.echo(f"\nConfiguration initialized in {config_dir}/")


@cli.command()
@click.argument("config_name", required=False)
@click.option("--host", default=DEFAULT_HOST, help="Host to bind server to")
@click.option("--port", default=DEFAULT_PORT, help="Port to bind server to")
@click.option("--backbone", "-b",
              type=click.Choice(["fastapi", "flask"]),
              default=DEFAULT_BACKBONE,
              help="Web framework to use")
@click.option("--log-level",
              type=click.Choice(["critical", "error", "warning", "info", "debug", "trace"]),
              default="info",
              help="Log level for the server")
def start(config_name: Optional[str], host: str, port: int, backbone: str, log_level: str) -> None:
    """Start API Dock server.

    CONFIG_NAME: Optional config name (default: config.yaml)

    Examples:
      api-dock start                 # Use config.yaml
      api-dock start my-config       # Use my-config.yaml
    """
    # Find configuration file
    config_path = find_config(config_name)

    if config_path is None:
        if config_name:
            click.echo(f"Error: Configuration '{config_name}' not found", err=True)
        else:
            click.echo("Error: No configuration file found", err=True)
        click.echo("\nRun 'api-dock init' to create default configuration")
        sys.exit(1)

    # Find available port
    available_port = _find_available_port(port, host)

    if available_port is None:
        click.echo(f"Error: Could not find available port. Tried ports {port} through {port + MAX_PORT_RETRIES}", err=True)
        sys.exit(1)

    if available_port != port:
        click.echo(f"Port {port} in use, using port {available_port} instead")

    try:
        if backbone.lower() == "fastapi":
            app = create_fastapi_app(config_path)
            click.echo(f"Starting API Dock server (FastAPI) on {host}:{available_port}")
            click.echo(f"Using config: {config_path}")

            uvicorn.run(
                app,
                host=host,
                port=available_port,
                log_level=log_level
            )
        elif backbone.lower() == "flask":
            app = create_flask_app(config_path)
            click.echo(f"Starting API Dock server (Flask) on {host}:{available_port}")
            click.echo(f"Using config: {config_path}")

            app.run(
                host=host,
                port=available_port,
                debug=(log_level == "debug")
            )

    except Exception as e:
        click.echo(f"Error starting API Dock: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.argument("config_name", required=False)
def describe(config_name: Optional[str]) -> None:
    """Describe API Dock configuration.

    CONFIG_NAME: Optional config name (default: config.yaml)

    Builds the API as the server would (lookups, startup checks), then shows
    each remote/version with its url and each database/version with its schema,
    its own tables and every route's SQL with [[table]] references expanded
    (`?` marks a bound request value).

    Examples:
      api-dock describe              # Describe config.yaml
      api-dock describe my-config    # Describe my-config.yaml
    """
    # Find configuration file
    config_path = find_config(config_name)

    if config_path is None:
        if config_name:
            click.echo(f"Error: Configuration '{config_name}' not found", err=True)
        else:
            click.echo("Error: No configuration file found", err=True)
        sys.exit(1)

    try:
        mapper = RouteMapper(config_path)
    except Exception as error:
        click.echo(f"Error loading configuration: {error}", err=True)
        sys.exit(1)
    config = mapper.config

    click.echo("=" * 60)
    click.echo(f"API Dock Configuration: {config_path}")
    click.echo("=" * 60)
    click.echo()
    click.echo(f"Name: {config.get('name', 'N/A')}")
    click.echo(f"Description: {config.get('description', 'N/A')}")
    authors = config.get('authors', [])
    if authors:
        click.echo(f"Authors: {', '.join(_author_text(author) for author in authors)}")
    click.echo()

    if mapper.remote_names:
        click.echo("Remotes:")
        for name in mapper.remote_names:
            for version in _versions(is_versioned_remote(name, config, mapper.config_dir),
                                     lambda: get_remote_versions(name, config, mapper.config_dir)):
                label = name if version is None else f"{name}/{version}"
                try:
                    remote = find_remote_config(name, config, mapper.config_dir, version=version)
                    click.echo(f"  - {label}: {remote.get('url', '(no url)')}")
                except FileNotFoundError as error:
                    click.echo(f"  - {label}: error: {error}")
        click.echo()

    if mapper.database_names:
        click.echo("Databases:")
        shared_file = load_shared_config(mapper.config_dir)
        for name in mapper.database_names:
            versions = _versions(is_versioned_database(name, mapper.config_dir),
                                 lambda: get_database_versions(name, mapper.config_dir))
            for version in versions:
                _describe_database(name, version, mapper, shared_file)
        click.echo()

    endpoints = config.get('endpoints', [])
    if endpoints:
        click.echo("Endpoints:")
        for endpoint in endpoints:
            click.echo(f"  - {endpoint}")
        click.echo()
    click.echo("=" * 60)

@cli.command()
@click.argument("plaintext")
@click.option("--method", "-m",
              type=click.Choice(["local_key", "env_key", "aws_kms"]),
              default="local_key",
              help="Encryption method to use")
@click.option("--key-id", help="AWS KMS key ID (for aws_kms method)")
@click.option("--region", default="us-east-1", help="AWS region (for aws_kms method)")
@click.option("--key-file", default=".api_dock_key", help="Key file path (for local_key method)")
@click.option("--key-env", default="API_DOCK_ENCRYPTION_KEY", help="Environment variable name (for env_key method)")
def encrypt(plaintext: str, method: str, key_id: Optional[str], region: str, key_file: str, key_env: str) -> None:
    """Encrypt a plaintext value for use in configuration files.

    PLAINTEXT: The value to encrypt

    Examples:
      api-dock encrypt "my-secret-token"
      api-dock encrypt --method aws_kms --key-id arn:aws:kms:... "secret"
      api-dock encrypt --method env_key --key-env MY_KEY "secret"
    """
    try:
        from api_dock.encryption import create_encryption_provider, EncryptionError

        # Build encryption config
        encryption_config = {"method": method}

        if method == "local_key":
            encryption_config["key_file"] = key_file
        elif method == "env_key":
            encryption_config["key_env"] = key_env
        elif method == "aws_kms":
            if not key_id:
                click.echo("Error: --key-id is required for aws_kms method", err=True)
                sys.exit(1)
            encryption_config["key_id"] = key_id
            encryption_config["region"] = region

        # Create provider and encrypt
        provider = create_encryption_provider(encryption_config)
        encrypted_value = provider.encrypt(plaintext)

        click.echo(f"Encrypted value: {encrypted_value}")

    except EncryptionError as e:
        click.echo(f"Encryption error: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.option("--output", "-o", default=".api_dock_key", help="Output file for the key")
@click.option("--force", "-f", is_flag=True, help="Overwrite existing key file")
def generate_key(output: str, force: bool) -> None:
    """Generate a new encryption key for local encryption.

    Examples:
      api-dock generate-key
      api-dock generate-key --output my_key_file
      api-dock generate-key --force  # Overwrite existing key
    """
    try:
        from api_dock.encryption import LocalKeyEncryption, EncryptionError

        output_path = Path(output)

        # Check if file exists
        if output_path.exists() and not force:
            click.echo(f"Error: Key file '{output}' already exists. Use --force to overwrite.", err=True)
            sys.exit(1)

        # Generate key
        key = LocalKeyEncryption.generate_key()

        # Write key to file
        with open(output_path, 'wb') as f:
            f.write(key)

        # Set restrictive permissions (owner read/write only)
        output_path.chmod(0o600)

        click.echo(f"✓ Generated encryption key: {output}")
        click.echo(f"✓ Set file permissions to 600 (owner read/write only)")

        # Show environment variable option
        click.echo(f"\nTo use this key:")
        click.echo(f"  1. Reference in config: encryption: {{method: local_key, key_file: {output}}}")
        click.echo(f"  2. Or set environment: export API_DOCK_ENCRYPTION_KEY=$(cat {output})")

    except EncryptionError as e:
        click.echo(f"Key generation error: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.argument("ciphertext")
@click.option("--method", "-m",
              type=click.Choice(["local_key", "env_key", "aws_kms"]),
              default="local_key",
              help="Decryption method to use")
@click.option("--key-id", help="AWS KMS key ID (for aws_kms method)")
@click.option("--region", default="us-east-1", help="AWS region (for aws_kms method)")
@click.option("--key-file", default=".api_dock_key", help="Key file path (for local_key method)")
@click.option("--key-env", default="API_DOCK_ENCRYPTION_KEY", help="Environment variable name (for env_key method)")
def decrypt(ciphertext: str, method: str, key_id: Optional[str], region: str, key_file: str, key_env: str) -> None:
    """Decrypt an encrypted value (for testing/debugging).

    CIPHERTEXT: The encrypted value to decrypt

    Examples:
      api-dock decrypt "gAAAAABh..."
      api-dock decrypt --method aws_kms --key-id arn:aws:kms:... "AQICAHh7..."
    """
    try:
        from api_dock.encryption import create_encryption_provider, EncryptionError

        # Build encryption config
        encryption_config = {"method": method}

        if method == "local_key":
            encryption_config["key_file"] = key_file
        elif method == "env_key":
            encryption_config["key_env"] = key_env
        elif method == "aws_kms":
            if not key_id:
                click.echo("Error: --key-id is required for aws_kms method", err=True)
                sys.exit(1)
            encryption_config["key_id"] = key_id
            encryption_config["region"] = region

        # Create provider and decrypt
        provider = create_encryption_provider(encryption_config)
        decrypted_value = provider.decrypt(ciphertext)

        click.echo(f"Decrypted value: {decrypted_value}")

    except EncryptionError as e:
        click.echo(f"Decryption error: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.argument("config_name", required=False)
@click.option("--name", "names", multiple=True, help="Lookup to run or refresh (repeatable)")
@click.option("--rows", "max_rows", default=DEFAULT_LOOKUP_ROWS, show_default=True,
              help="Rows to print per lookup (local runs)")
@click.option("--url", help="Endpoint of a running server (settings.lookups.refresh_route), "
                            "e.g. http://localhost:8000/admin/lookups")
@click.option("--token", envvar=ADMIN_TOKEN_ENV,
              help=f"Endpoint token (default: ${ADMIN_TOKEN_ENV})")
@click.option("--refresh", is_flag=True, help="With --url: refresh lookups on the server")
def lookups(config_name: Optional[str], names: Tuple[str, ...], max_rows: int,
            url: Optional[str], token: Optional[str], refresh: bool) -> None:
    """Run lookups locally, or show/refresh them on a running server.

    CONFIG_NAME: Optional config name (default: config.yaml)

    Without --url, runs each lookup in the config and prints its first rows,
    which is handy for checking a lookup before starting the server. With
    --url, shows the server's lookup status, or refreshes them with --refresh.

    \b
    Examples:
      api-dock lookups                          # run every lookup locally
      api-dock lookups --name model_runs --rows 20
      api-dock lookups --url http://localhost:8000/admin/lookups
      api-dock lookups --url http://localhost:8000/admin/lookups --refresh --name model_runs
    """
    if url:
        sys.exit(_server_lookups(url, token, refresh, list(names)))
    if refresh:
        click.echo("Error: --refresh needs --url (local runs always run the lookups)", err=True)
        sys.exit(2)

    config_path = find_config(config_name)
    if config_path is None:
        click.echo(f"Error: Configuration '{config_name or 'config'}' not found", err=True)
        sys.exit(1)
    config_dir = os.path.dirname(config_path)
    try:
        specs = load_lookup_specs(config_dir)
        main_config = load_main_config(config_path)
    except (ValueError, yaml.YAMLError) as error:
        click.echo(f"Error: {error}", err=True)
        sys.exit(1)
    unknown = [name for name in names if name not in specs]
    if unknown:
        click.echo(f"Error: unknown lookup(s): {', '.join(unknown)}", err=True)
        sys.exit(1)
    if not specs:
        click.echo("No lookups defined.")
        return

    context = LookupContext(
        config_dir, main_config, (main_config.get("settings") or {}).get("duckdb")
    )
    failed = False
    for name in names or specs:
        spec = specs[name]
        click.echo(f"== {name} ({spec.kind}, {spec.source})")
        try:
            rows = run_lookup(spec, context)
        except Exception as error:
            failed = True
            click.echo(f"   failed: {error}", err=True)
            continue
        click.echo(f"   {len(rows)} row(s)")
        for row in rows[:max_rows]:
            click.echo("   " + json.dumps(row, default=str))
        if len(rows) > max_rows:
            click.echo(f"   ... {len(rows) - max_rows} more")
    sys.exit(1 if failed else 0)


def main() -> None:
    """Main CLI entry point."""
    cli()


#
# INTERNAL
#
def _author_text(author: Any) -> str:
    """Format an author entry (a string, or a mapping with name/email).

    Args:
        author: The author entry.

    Returns:
        Display text.
    """
    if isinstance(author, dict):
        name = author.get('name') or author.get('url') or 'Unknown'
        email = author.get('email')
        return f"{name} <{email}>" if email else str(name)
    return str(author)


def _versions(versioned: bool, versions: Any) -> list:
    """The versions to describe: the listed ones, or [None] when unversioned.

    Args:
        versioned: Whether the remote/database is versioned.
        versions: Callable returning its versions.

    Returns:
        List of versions (None for an unversioned one).
    """
    return list(versions()) if versioned else [None]


def _describe_database(name: str, version: Optional[str], mapper: RouteMapper,
                       shared_file: dict) -> None:
    """Print one database/version: schema, own tables and expanded route SQL.

    Args:
        name: Database name.
        version: Version, or None if unversioned.
        mapper: The route mapper (for config and its directory).
        shared_file: The shared database config.
    """
    label = name if version is None else f"{name}/{version}"
    click.echo(f"\n  {label}:")
    try:
        db_config = load_database_config(name, mapper.config_dir, version)
        db_config = apply_shared_definitions(db_config, shared_file, name, version)
    except (FileNotFoundError, ValueError, yaml.YAMLError) as error:
        click.echo(f"    Error loading database config: {error}")
        return
    if db_config.get('schema'):
        click.echo(f"    Schema: {db_config['schema']}")
    tables = db_config.get('tables') or {}
    if tables:
        click.echo("    Tables:")
        for table_name, table in tables.items():
            click.echo(f"      {table_name}: {table}")
    shared = shared_file.get(SHARED_CONFIG_KEY) or {}
    context = SqlContext(name=name, version=version,
                         schema_groups=shared_file.get(SCHEMA_GROUPS_KEY) or {},
                         schema_sources=get_schema_sources(mapper.database_names,
                                                           mapper.config_dir))
    routes = [r for r in db_config.get('routes') or [] if isinstance(r, dict)]
    if routes:
        click.echo("    Routes:")
    for route_config in routes:
        click.echo(f"      {route_config.get('route', '')}:")
        # Stand-in values for the route's variables; values are bound, so they
        # show as markers either way.
        names = set(VARIABLE_PATTERN.findall(json.dumps(route_config)))
        path_values = {n: n for n in names if not n.startswith(("cookies.", "self."))}
        cookie_values = {n[len("cookies."):]: n for n in names if n.startswith("cookies.")}
        try:
            sql, _ = build_sql_query(merge_query_params(route_config, db_config), db_config,
                                     path_params=path_values, cookies=cookie_values,
                                     shared_config=shared, context=context)
        except Exception as error:  # e.g. a selector that needs request values
            sql = f"{route_config.get('sql')}\n        (not expanded: {error})"
        click.echo("        " + str(sql).replace('\n', '\n        '))


def _server_lookups(url: str, token: Optional[str], refresh: bool, names: list) -> int:
    """Show or refresh lookups on a running server.

    Args:
        url: The server's lookup endpoint.
        token: Bearer token.
        refresh: POST (refresh) instead of GET (status).
        names: Lookups to refresh (empty: all).

    Returns:
        Exit code: 0 on success, 1 otherwise.
    """
    import httpx

    if not token:
        click.echo(f"Error: --token (or ${ADMIN_TOKEN_ENV}) is required with --url", err=True)
        return 1
    try:
        response = httpx.request(
            "POST" if refresh else "GET", url, params=[("name", name) for name in names],
            headers={"Authorization": f"Bearer {token}"}, timeout=None if refresh else 30,
        )
    except httpx.HTTPError as error:
        click.echo(f"Error: {error}", err=True)
        return 1
    try:
        click.echo(json.dumps(response.json(), indent=2))
    except ValueError:
        click.echo(response.text)
    return 0 if response.status_code < 400 else 1


def _find_available_port(start_port: int, host: str, max_retries: int = MAX_PORT_RETRIES) -> Optional[int]:
    """Find an available port starting from start_port.

    Args:
        start_port: Initial port to try.
        host: Host address to bind to.
        max_retries: Maximum number of ports to try (will try start_port through start_port + max_retries).

    Returns:
        Available port number if found, None otherwise.
    """
    for port_offset in range(max_retries + 1):
        port = start_port + port_offset
        try:
            # Try to bind to the port
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
            sock.close()
            return port
        except OSError:
            # Port is in use, try next one
            continue

    return None


def _list_configs() -> None:
    """List available configurations."""
    click.echo("API Dock - API wrapper using configuration files")
    click.echo()

    # Check for local configs
    local_dir = Path("api_dock_config")
    local_configs = list(local_dir.glob("*.yaml")) if local_dir.exists() else []

    # Check for bundled example configs
    try:
        import importlib.resources as pkg_resources
        package_dir = Path(pkg_resources.files("api_dock") / "example_api_dock_config")
        package_configs = list(package_dir.glob("*.yaml")) if package_dir.exists() else []
    except Exception:
        package_configs = []

    if local_configs:
        click.echo("📁 Local configurations (api_dock_config/):")
        for config_file in sorted(local_configs):
            click.echo(f"  {config_file.stem}")
        click.echo()
    else:
        click.echo("📁 Local configurations (api_dock_config/): None — run 'api-dock init' to create")
        click.echo()

    if package_configs:
        click.echo("📦 Example configurations (run 'api-dock init' to copy to api_dock_config/):")
        for config_file in sorted(package_configs):
            click.echo(f"  {config_file.stem}")
        click.echo()

    click.echo("Commands:")
    click.echo("  api-dock init                    # Initialize config directory")
    click.echo("  api-dock start [config]          # Start API Dock server")
    click.echo("  api-dock describe [config]       # Describe configuration")
    click.echo("  api-dock generate-key            # Generate encryption key")
    click.echo("  api-dock encrypt [value]         # Encrypt authentication tokens")
    click.echo("  api-dock decrypt [value]         # Decrypt authentication tokens")
    click.echo()
    click.echo("Run 'api-dock --help' for more information")


if __name__ == "__main__":
    main()

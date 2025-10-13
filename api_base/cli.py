"""

CLI Module for API Base

Click-based command-line interface for API Base operations.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import sys
from pathlib import Path
from typing import Optional

import click
import uvicorn
import yaml

from api_base.config import load_main_config
from api_base.config_discovery import find_config, init_config
from api_base.database_config import load_database_config
from api_base.fast_api import create_app as create_fastapi_app
from api_base.flask_api import create_app as create_flask_app
from api_base.sql_builder import build_sql_query


#
# CONSTANTS
#
DEFAULT_HOST: str = "0.0.0.0"
DEFAULT_PORT: int = 8000
DEFAULT_BACKBONE: str = "fastapi"


#
# PUBLIC
#
@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx: click.Context) -> None:
    """API Base - API wrapper using configuration files.

    Run without command to see available configurations.
    """
    if ctx.invoked_subcommand is None:
        _list_configs()


@cli.command()
@click.option("--force", "-f", is_flag=True, help="Overwrite existing files")
def init(force: bool) -> None:
    """Initialize api_base_config/ directory with default configurations.

    Copies configuration files from the package to api_base_config/.
    """
    config_dir = Path("api_base_config")

    # Check if directory exists and has files
    if config_dir.exists() and not force:
        config_files = list(config_dir.glob("*.yaml"))
        if config_files:
            click.echo(f"Error: {config_dir}/ already contains configuration files.", err=True)
            click.echo("Use --force to overwrite.", err=True)
            sys.exit(1)

    # Initialize configuration
    click.echo(f"Initializing {config_dir}/...")

    if init_config():
        click.echo(f"✓ Created {config_dir}/")
        click.echo(f"✓ Created {config_dir}/remotes/")
        click.echo(f"✓ Created {config_dir}/databases/")
        click.echo("✓ Copied default configuration files")
        click.echo(f"\nConfiguration initialized in {config_dir}/")
    else:
        click.echo("Error: Failed to initialize configuration", err=True)
        sys.exit(1)


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
    """Start API Base server.

    CONFIG_NAME: Optional config name (default: config.yaml)

    Examples:
      api-base start                 # Use config.yaml
      api-base start my-config       # Use my-config.yaml
    """
    # Find configuration file
    config_path = find_config(config_name)

    if config_path is None:
        if config_name:
            click.echo(f"Error: Configuration '{config_name}' not found", err=True)
        else:
            click.echo("Error: No configuration file found", err=True)
        click.echo("\nRun 'api-base init' to create default configuration")
        sys.exit(1)

    try:
        if backbone.lower() == "fastapi":
            app = create_fastapi_app(config_path)
            click.echo(f"Starting API Base server (FastAPI) on {host}:{port}")
            click.echo(f"Using config: {config_path}")

            uvicorn.run(
                app,
                host=host,
                port=port,
                log_level=log_level
            )
        elif backbone.lower() == "flask":
            app = create_flask_app(config_path)
            click.echo(f"Starting API Base server (Flask) on {host}:{port}")
            click.echo(f"Using config: {config_path}")

            app.run(
                host=host,
                port=port,
                debug=(log_level == "debug")
            )

    except Exception as e:
        click.echo(f"Error starting API Base: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.argument("config_name", required=False)
def describe(config_name: Optional[str]) -> None:
    """Describe API Base configuration.

    CONFIG_NAME: Optional config name (default: config.yaml)

    Displays formatted configuration with expanded SQL queries.

    Examples:
      api-base describe              # Describe config.yaml
      api-base describe my-config    # Describe my-config.yaml
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
        # Load main configuration
        config = load_main_config(config_path)

        click.echo("=" * 60)
        click.echo(f"API Base Configuration: {config_path}")
        click.echo("=" * 60)
        click.echo()

        # Display basic info
        click.echo(f"Name: {config.get('name', 'N/A')}")
        click.echo(f"Description: {config.get('description', 'N/A')}")

        authors = config.get('authors', [])
        if authors:
            # Handle both string authors and dict authors (with name/email)
            author_strings = []
            for author in authors:
                if isinstance(author, dict):
                    name = author.get('name', 'Unknown')
                    email = author.get('email')
                    if email:
                        author_strings.append(f"{name} <{email}>")
                    else:
                        author_strings.append(name)
                else:
                    author_strings.append(str(author))
            click.echo(f"Authors: {', '.join(author_strings)}")

        click.echo()

        # Display remotes
        remotes = config.get('remotes', [])
        if remotes:
            click.echo("Remotes:")
            for remote in remotes:
                click.echo(f"  - {remote}")
            click.echo()

        # Display databases with expanded SQL
        databases = config.get('databases', [])
        if databases:
            click.echo("Databases:")
            for db_name in databases:
                click.echo(f"\n  {db_name}:")
                try:
                    db_config = load_database_config(db_name)

                    # Display tables
                    tables = db_config.get('tables', {})
                    if tables:
                        click.echo("    Tables:")
                        for table_name, table_path in tables.items():
                            click.echo(f"      {table_name}: {table_path}")

                    # Display routes with expanded SQL
                    routes = db_config.get('routes', [])
                    if routes:
                        click.echo("\n    Routes:")
                        for route_config in routes:
                            route_path = route_config.get('route', '')
                            sql = route_config.get('sql', '')

                            # Expand SQL query
                            try:
                                expanded_sql = build_sql_query(sql, db_config)
                                # Format SQL for display
                                expanded_sql = expanded_sql.replace('\n', '\n        ')
                                click.echo(f"      {route_path}:")
                                click.echo(f"        {expanded_sql}")
                            except Exception:
                                # If expansion fails, show original
                                click.echo(f"      {route_path}:")
                                click.echo(f"        {sql}")

                except Exception as e:
                    click.echo(f"    Error loading database config: {e}")
            click.echo()

        # Display endpoints
        endpoints = config.get('endpoints', [])
        if endpoints:
            click.echo("Endpoints:")
            for endpoint in endpoints:
                click.echo(f"  - {endpoint}")
            click.echo()

        click.echo("=" * 60)

    except Exception as e:
        click.echo(f"Error loading configuration: {e}", err=True)
        sys.exit(1)


def main() -> None:
    """Main CLI entry point."""
    cli()


#
# INTERNAL
#
def _list_configs() -> None:
    """List available configurations."""
    click.echo("API Base - API wrapper using configuration files")
    click.echo()

    # Check for local configs
    local_dir = Path("api_base_config")
    config_dir = Path("config")

    local_configs = list(local_dir.glob("*.yaml")) if local_dir.exists() else []
    config_configs = list(config_dir.glob("*.yaml")) if config_dir.exists() else []

    # Check for package configs
    try:
        import importlib.resources as pkg_resources
        package_dir = Path(pkg_resources.files("api_base") / "config")
        package_configs = list(package_dir.glob("*.yaml")) if package_dir.exists() else []
    except Exception:
        package_configs = []

    if local_configs:
        click.echo("📁 Local configurations (api_base_config/):")
        for config_file in sorted(local_configs):
            click.echo(f"  {config_file.stem}")
        click.echo()
    else:
        click.echo("📁 Local configurations (api_base_config/): None")
        click.echo()

    if config_configs:
        click.echo("📁 Project configurations (config/):")
        for config_file in sorted(config_configs):
            click.echo(f"  {config_file.stem}")
        click.echo()

    if package_configs:
        click.echo("📦 Package configurations:")
        for config_file in sorted(package_configs):
            click.echo(f"  {config_file.stem}")
        click.echo()

    click.echo("Commands:")
    click.echo("  api-base init                    # Initialize config directory")
    click.echo("  api-base start [config]          # Start API Base server")
    click.echo("  api-base describe [config]       # Describe configuration")
    click.echo()
    click.echo("Run 'api-base --help' for more information")


if __name__ == "__main__":
    main()

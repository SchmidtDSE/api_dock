"""

CLI Module for API Dock

Click-based command-line interface for API Dock operations.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import socket
import sys
from pathlib import Path
from typing import Optional

import click
import uvicorn
import yaml

from api_dock.config import load_main_config
from api_dock.config_discovery import find_config, init_config
from api_dock.database_config import load_database_config
from api_dock.fast_api import create_app as create_fastapi_app
from api_dock.flask_api import create_app as create_flask_app
from api_dock.sql_builder import build_sql_query


#
# CONSTANTS
#
DEFAULT_HOST: str = "0.0.0.0"
DEFAULT_PORT: int = 8000
DEFAULT_BACKBONE: str = "fastapi"
MAX_PORT_RETRIES: int = 4


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
@click.option("--force", "-f", is_flag=True, help="Overwrite existing files")
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

    Displays formatted configuration with expanded SQL queries.

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
        # Load main configuration
        config = load_main_config(config_path)

        click.echo("=" * 60)
        click.echo(f"API Dock Configuration: {config_path}")
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


def main() -> None:
    """Main CLI entry point."""
    cli()


#
# INTERNAL
#
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
    config_dir = Path("config")

    local_configs = list(local_dir.glob("*.yaml")) if local_dir.exists() else []
    config_configs = list(config_dir.glob("*.yaml")) if config_dir.exists() else []

    # Check for package configs
    try:
        import importlib.resources as pkg_resources
        package_dir = Path(pkg_resources.files("api_dock") / "config")
        package_configs = list(package_dir.glob("*.yaml")) if package_dir.exists() else []
    except Exception:
        package_configs = []

    if local_configs:
        click.echo("📁 Local configurations (api_dock_config/):")
        for config_file in sorted(local_configs):
            click.echo(f"  {config_file.stem}")
        click.echo()
    else:
        click.echo("📁 Local configurations (api_dock_config/): None")
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

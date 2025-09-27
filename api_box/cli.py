"""

CLI Module for API Box

Command-line interface for launching the API Box server.

License: CC-BY-4.0

"""

#
# IMPORTS
#
import argparse
import sys
from typing import Optional

import uvicorn

from api_box.fast_api import create_app as create_fastapi_app
from api_box.flask_api import create_app as create_flask_app


#
# CONSTANTS
#
DEFAULT_HOST: str = "0.0.0.0"
DEFAULT_PORT: int = 8000
DEFAULT_CONFIG_PATH: Optional[str] = None
DEFAULT_BACKBONE: str = "fastapi"


#
# PUBLIC
#
def main() -> None:
    """Main CLI entry point for API Box."""
    parser = _create_parser()
    args = parser.parse_args()

    try:
        if args.backbone.lower() == "fastapi":
            app = create_fastapi_app(args.config)
            print(f"Starting API Box server (FastAPI) on {args.host}:{args.port}")

            uvicorn.run(
                app,
                host=args.host,
                port=args.port,
                log_level=args.log_level
            )
        elif args.backbone.lower() == "flask":
            app = create_flask_app(args.config)
            print(f"Starting API Box server (Flask) on {args.host}:{args.port}")

            app.run(
                host=args.host,
                port=args.port,
                debug=(args.log_level == "debug")
            )
        else:
            print(f"Error: Unknown backbone '{args.backbone}'. Choose 'fastapi' or 'flask'.", file=sys.stderr)
            sys.exit(1)

    except Exception as e:
        print(f"Error starting API Box: {e}", file=sys.stderr)
        sys.exit(1)


#
# INTERNAL
#
def _create_parser() -> argparse.ArgumentParser:
    """Create and configure argument parser.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(
        description="API Box - API wrapper using configuration files",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )

    parser.add_argument(
        "--host",
        type=str,
        default=DEFAULT_HOST,
        help="Host to bind the server to"
    )

    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="Port to bind the server to"
    )

    parser.add_argument(
        "--config",
        type=str,
        default=DEFAULT_CONFIG_PATH,
        help="Path to main configuration file"
    )

    parser.add_argument(
        "--log-level",
        type=str,
        default="info",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
        help="Log level for the server"
    )

    parser.add_argument(
        "--backbone", "-b",
        type=str,
        default=DEFAULT_BACKBONE,
        choices=["fastapi", "flask"],
        help="Web framework backbone to use (fastapi or flask)"
    )

    return parser


if __name__ == "__main__":
    main()
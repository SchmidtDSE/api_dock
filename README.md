# API Box

A FastAPI-based API proxy that routes requests to multiple remote APIs using YAML configuration files.

## Description

API Box is a flexible API gateway that allows you to proxy requests to multiple remote APIs through a single endpoint. It uses YAML configuration files to define remote API connections, routing rules, and access controls. This makes it easy to manage multiple API integrations and provide a unified interface to your applications.

## Features

- **Multi-API Proxying**: Route requests to different remote APIs based on configuration
- **YAML Configuration**: Simple, human-readable configuration files
- **Access Control**: Define allowed/restricted routes per remote API
- **Version Support**: Handle API versioning in URL paths
- **FastAPI Backend**: Built on FastAPI for high performance and automatic documentation
- **Type Safety**: Full type hints throughout the codebase

## Quick Start

### Installation

## INSTALL/REQUIREMENTS

Requirements are managed through a [Pixi](https://pixi.sh/latest) "project" (similar to a conda environment). After pixi is installed use `pixi run <cmd>` to ensure the correct project is being used. For example,

```bash
# lauch jupyter
pixi run jupyter lab .

# run a script
pixi run python scripts/hello_world.py
```

The first time `pixi run` is executed the project will be installed (note this means the first run will be a bit slower). Any changes to the project will be updated on the subsequent `pixi run`.  It is unnecessary, but you can run `pixi install` after changes - this will update your local environment, so that it does not need to be updated on the next `pixi run`.

Note, the repo's `pyproject.toml`, and `pixi.lock` files ensure `pixi run` will just work. No need to recreate an environment. Additionally, the `pyproject.toml` file includes `api_box = { path = ".", editable = true }`. This line is equivalent to `pip install -e .`, so there is no need to pip install this module.

The project was initially created using a `package_names.txt` and the following steps. Note that this should **NOT** be re-run as it will create a new project (potentially changing package versions).

```bash
#
# IMPORTANT: Do NOT run this unless you explicity want to create a new pixi project
#
# 1. initialize pixi project (in this case the pyproject.toml file had already existed)
pixi init . --format pyproject
# 2. add specified python version
pixi add python=3.11
# 3. add packages (note this will use pixi magic to determine/fix package version ranges)
pixi add $(cat package_names.txt)
# 4. add pypi-packages, if any (note this will use pixi magic to determine/fix package version ranges)
pixi add --pypi $(cat pypi_package_names.txt)
```

### Basic Usage

```bash
# Start API Box with default configuration
pixi run api-box

# Start with custom configuration file
pixi run api-box --config /path/to/config.yaml

# Start on custom host/port
pixi run api-box --host 0.0.0.0 --port 9000

# Run with debug logging
pixi run api-box --log-level debug
```

## Configuration

### Main Configuration (`config/config.yaml`)

```yaml
name: "My API Box"
description: "API proxy for multiple services"
authors: ["Your Name"]
remotes:
  - "service1"
  - "service2"
```

### Remote Configuration (`config/remotes/service1.yaml`)

```yaml
name: "service1"
url: "http://localhost:8001"
description: "First remote service"
```

## API Usage

Once running, API Box provides:

- `GET /` - API metadata
- `GET /{config_key}` - Get configuration values
- `/{remote_name}/{version}/{path}` - Proxy to remote APIs

### Example Requests

```bash
# Get API metadata
curl http://localhost:8000/

# Access remote API through proxy
curl http://localhost:8000/service1/latest/users/

# Access specific API version
curl http://localhost:8000/service1/v2/users/123
```

## Project Structure

```
api_box/
├── api_box/
│   ├── __init__.py      # Module exports
│   ├── main.py          # FastAPI application
│   ├── config.py        # Configuration management
│   └── cli.py           # Command-line interface
├── config/              # Configuration files
├── README.md
└── pyproject.toml
```

## Development

The project follows Python best practices:

- **Type Safety**: Full type hints throughout
- **Documentation**: Comprehensive docstrings
- **Code Style**: PEP 8 compliance
- **Testing**: Ready for unit and integration tests

## License

CC-BY-4.0

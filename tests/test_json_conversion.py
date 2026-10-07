"""

Tests for converting database values to JSON.

Rows from a database route are converted value by value before being sent as
JSON. Containers are converted recursively, and types that ``json`` can't
write (dates, decimals, bytes, UUIDs, intervals, network addresses) become
strings or numbers. Unknown types are not converted.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import ipaddress
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict
from uuid import UUID

import pytest
import yaml

from api_dock.route_mapper import RouteMapper, _make_json_safe


#
# CONSTANTS
#
SAMPLE_UUID: str = "8f14e45f-ceea-467f-a8f1-9f1e2c4c6a3b"

# A route returning DuckDB types that json can't write directly.
TYPES_CONFIG: Dict[str, Any] = {
    "name": "types",
    "routes": [{
        "route": "values",
        "sql": (
            f"SELECT '{SAMPLE_UUID}'::UUID AS id, INTERVAL 90 SECOND AS wait, "
            "[DATE '2024-05-01', NULL] AS days, "
            "{'price': 1.25::DECIMAL(4, 2), 'tags': ['a']} AS details"
        ),
    }],
}


#
# PUBLIC
#
class TestScalarConversion:
    """Single values convert as before, plus UUIDs, intervals and addresses."""

    @pytest.mark.parametrize("value, expected", [
        (None, None),
        ("text", "text"),
        (3, 3),
        (True, True),
        (date(2024, 5, 1), "2024-05-01"),
        (datetime(2024, 5, 1, 12, 30), "2024-05-01T12:30:00"),
        (datetime(2024, 5, 1, 12, 30, tzinfo=timezone.utc), "2024-05-01T12:30:00+00:00"),
        (Decimal("1.25"), 1.25),
        (b"\x00\xff", "AP8="),
        (UUID(SAMPLE_UUID), SAMPLE_UUID),
        (timedelta(minutes=1, seconds=30, microseconds=500000), 90.5),
        (ipaddress.ip_address("192.168.0.1"), "192.168.0.1"),
        (ipaddress.ip_address("::1"), "::1"),
        (ipaddress.ip_interface("10.0.0.5/24"), "10.0.0.5/24"),
        (ipaddress.ip_network("10.0.0.0/8"), "10.0.0.0/8"),
        (ipaddress.ip_network("2001:db8::/32"), "2001:db8::/32"),
    ])
    def test_value_converts(self, value: Any, expected: Any) -> None:
        """Each value converts to the expected JSON-safe value."""
        assert _make_json_safe(value) == expected

    def test_unknown_type_is_left_unchanged(self) -> None:
        """A type with no conversion is returned as is, not turned into a string."""
        value = object()
        assert _make_json_safe(value) is value


class TestContainerConversion:
    """Dictionaries, lists and tuples are converted recursively."""

    def test_nested_containers_convert(self) -> None:
        """Values inside nested lists, tuples and dictionaries convert."""
        value = {
            "ids": [UUID(SAMPLE_UUID), None],
            "grid": [[Decimal("1.5")], (date(2024, 1, 2),)],
            "meta": {"seen": datetime(2024, 1, 2, 3, 4, 5)},
        }
        assert _make_json_safe(value) == {
            "ids": [SAMPLE_UUID, None],
            "grid": [[1.5], ["2024-01-02"]],
            "meta": {"seen": "2024-01-02T03:04:05"},
        }

    def test_tuple_becomes_list(self) -> None:
        """A tuple becomes a list, so it is written as a JSON array."""
        assert _make_json_safe((1, "a")) == [1, "a"]


class TestDatabaseRouteConversion:
    """A database route returns DuckDB values that json can't write directly."""

    @pytest.mark.anyio
    async def test_duckdb_types_convert(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """UUID, INTERVAL, a list of dates and a struct are returned as JSON."""
        config_dir = tmp_path / "api_dock_config"
        (config_dir / "databases").mkdir(parents=True)
        _write_yaml(config_dir / "config.yaml", {"name": "test", "databases": ["types"]})
        _write_yaml(config_dir / "databases" / "types.yaml", TYPES_CONFIG)
        monkeypatch.chdir(tmp_path)

        mapper = RouteMapper(str(config_dir / "config.yaml"))
        result = await mapper.map_database_route("types", "values", {}, {})

        assert result.status_code == 200
        assert json.loads(result.content) == [{
            "id": SAMPLE_UUID,
            "wait": 90.0,
            "days": ["2024-05-01", None],
            "details": {"price": 1.25, "tags": ["a"]},
        }]


#
# INTERNAL
#
def _write_yaml(path: Path, data: Dict[str, Any]) -> None:
    """Write data to a YAML file.

    Args:
        path: File to write.
        data: Data to write.
    """
    path.write_text(yaml.safe_dump(data))

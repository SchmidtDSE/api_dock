"""

Route Headers Module for API Dock

Checks and fills the ``headers:`` of database routes. Each header value is a
text template whose ``{{variables}}`` are path variables of the route. Values
are written as text; they are not quoted or changed.

License: BSD 3-Clause

"""

#
# IMPORTS
#
import re
from typing import Any, Dict, List, Mapping, Optional


#
# CONSTANTS
#
HEADERS_KEY: str = "headers"

# The {{variable}} syntax of sql_builder.VARIABLE_PATTERN. It is repeated here
# because sql_builder imports database_config, which imports this module.
VARIABLE_PATTERN: re.Pattern[str] = re.compile(r'\{\{([^{}]+)\}\}')

# An HTTP token (RFC 9110, section 5.6.2): the characters a header name can have.
HEADER_NAME_PATTERN: re.Pattern[str] = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")

# Headers a route can't set, besides the hop-by-hop headers of route_mapper.
# The adapters set Content-Type and Content-Length from the response, and a
# database route must not set cookies.
EXTRA_RESERVED_HEADERS: frozenset = frozenset({"content-type", "content-length", "set-cookie"})


#
# PUBLIC
#
class HeaderValueError(ValueError):
    """Raised when a filled header value can't be sent.

    Attributes:
        header: The header name.
        reason: Why the value was refused.
    """

    def __init__(self, header: str, reason: str) -> None:
        """Create the error for one header.

        Args:
            header: The header name.
            reason: Why the value was refused.
        """
        self.header = header
        self.reason = reason
        super().__init__(f"header '{header}': {reason}")


def check_route_headers(route_config: Dict[str, Any]) -> None:
    """Check a route's ``headers:`` names and templates.

    Args:
        route_config: Route configuration with a ``route`` pattern.

    Raises:
        ValueError: If headers is not a mapping, a name is not an HTTP token or
            is reserved, a template is not text, or a template uses a name that
            is not a path variable of the route.
    """
    if HEADERS_KEY not in route_config:
        return
    headers = route_config[HEADERS_KEY]
    if not isinstance(headers, dict):
        raise ValueError("headers must be a mapping of header names to text")
    path_variables = route_path_variables(route_config.get("route", ""))
    for name, template in headers.items():
        _check_header_name(name)
        if not isinstance(template, str):
            raise ValueError(f"headers.{name} must be text")
        for variable in VARIABLE_PATTERN.findall(template):
            if variable not in path_variables:
                raise ValueError(
                    f"headers.{name}: '{{{{{variable}}}}}' is not a path variable of the route"
                )


def fill_route_headers(
        headers: Dict[str, str], values: Mapping[str, Optional[str]]) -> Dict[str, str]:
    """Fill header templates with values, as plain text.

    A header with a None value in its template is left out.

    Args:
        headers: Header name to template, as checked by check_route_headers().
        values: Variable name to value.

    Returns:
        Header name to filled value.

    Raises:
        HeaderValueError: If a variable has no value, or a filled value has a
            control character or a non-ASCII character.
    """
    filled: Dict[str, str] = {}
    for name, template in headers.items():
        value = _fill_template(name, template, values)
        if value is not None:
            filled[name] = value
    return filled


def route_path_variables(pattern: str) -> List[str]:
    """List the variable names in a route pattern.

    Args:
        pattern: Route pattern, e.g. ``items/{{id}}``.

    Returns:
        Names of the ``{{name}}`` segments. An unnamed ``{{}}`` segment is left out.
    """
    return [
        segment[2:-2]
        for segment in pattern.strip("/").split("/")
        if segment.startswith("{{") and segment.endswith("}}") and len(segment) > 4
    ]


#
# INTERNAL
#
def _check_header_name(name: Any) -> None:
    """Refuse a header name that is not an HTTP token or that api_dock sets itself."""
    # route_mapper imports this module through database_config, so import here.
    from api_dock.route_mapper import HOP_BY_HOP_HEADERS

    if not isinstance(name, str) or not HEADER_NAME_PATTERN.fullmatch(name):
        raise ValueError(f"headers: '{name}' is not a valid header name")
    if name.lower() in HOP_BY_HOP_HEADERS | EXTRA_RESERVED_HEADERS:
        raise ValueError(f"headers: a route can't set '{name}'")


def _fill_template(
        name: str, template: str, values: Mapping[str, Optional[str]]) -> Optional[str]:
    """Fill one template in one pass; return None if a value in it is None."""
    found_null = False

    def replace_variable(match: re.Match[str]) -> str:
        nonlocal found_null
        variable = match.group(1)
        if variable not in values:
            raise HeaderValueError(name, f"no value for '{variable}'")
        if values[variable] is None:
            found_null = True
            return ""
        return str(values[variable])

    value = VARIABLE_PATTERN.sub(replace_variable, template)
    if found_null:
        return None
    if any(ord(character) < 0x20 or ord(character) >= 0x7F for character in value):
        raise HeaderValueError(name, "the value has a control character or a non-ASCII character")
    return value

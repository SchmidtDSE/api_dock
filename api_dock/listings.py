"""

Listings Module for API Dock

Builds optional catalog endpoints that list the models/versions of configured
databases, remotes, or both ("sources"), driven by the main config's ``expose``
section.

License: BSD 3-Clause

"""
#
# IMPORTS
#
from typing import Any, Dict, List, Optional, Tuple

from api_dock.config import (
    get_remote_names,
    get_remote_versions,
    is_versioned_remote,
    versions_equal,
)
from api_dock.database_config import (
    get_database_names,
    get_database_versions,
    is_versioned_database,
)
from api_dock.types import ListingSpec

#
# CONSTANTS
#
# The listing kinds that may be exposed, and the config keys that select them.
LISTING_KINDS: Tuple[str, ...] = ("databases", "remotes", "sources")

# Keys recognized inside the ``expose`` mapping.
EXPOSE_KEYS: frozenset = frozenset({"dict", *LISTING_KINDS})


#
# PUBLIC
#
def resolve_listing_specs(
        config: Dict[str, Any],
        config_dir: Optional[str] = None) -> Tuple[List[ListingSpec], List[str]]:
    """Resolve the main config's ``expose`` section into listing specs.

    Listings are opt-in: with no ``expose`` key (or ``expose: false``) nothing is
    exposed. ``expose: true`` enables all three defaults; an ``expose`` mapping
    enables each of ``databases``/``remotes``/``sources`` that it names.

    Args:
        config: Main configuration dictionary.
        config_dir: Directory holding the config files. If None, uses default.

    Returns:
        Tuple of (specs, warnings). ``warnings`` are human-readable strings for
        misconfiguration (unknown keys, route collisions, unknown includes) that
        the caller should surface at startup.
    """
    expose = config.get("expose")
    warnings_out: List[str] = []

    if expose is None or expose is False:
        return ([], warnings_out)

    global_dict = True
    entries: Dict[str, Any] = {}

    if expose is True:
        entries = {kind: True for kind in LISTING_KINDS}
    elif isinstance(expose, dict):
        global_dict = bool(expose.get("dict", True))
        for key in expose:
            if key not in EXPOSE_KEYS:
                warnings_out.append(f"expose: unknown key '{key}' ignored")
        for kind in LISTING_KINDS:
            if kind in expose:
                entries[kind] = expose[kind]
    else:
        warnings_out.append(
            f"expose: expected a boolean or mapping, got {type(expose).__name__}; ignoring"
        )
        return ([], warnings_out)

    specs: List[ListingSpec] = []
    for kind in LISTING_KINDS:
        if kind not in entries:
            continue
        spec = _normalize_entry(kind, entries[kind], global_dict, warnings_out)
        if spec is not None:
            specs.append(spec)

    _add_collision_warnings(specs, config, config_dir, warnings_out)
    _add_unknown_include_warnings(specs, config, config_dir, warnings_out)
    return (specs, warnings_out)


def build_listing(
        spec: ListingSpec,
        config: Dict[str, Any],
        config_dir: Optional[str] = None) -> List[Any]:
    """Build the response body for a single listing endpoint.

    Args:
        spec: The resolved listing spec.
        config: Main configuration dictionary.
        config_dir: Directory holding the config files. If None, uses default.

    Returns:
        A list of ``{"model", "version"}`` dicts (when ``spec.as_dict``) or
        ``"model/version"`` strings. Unversioned sources have a ``None`` version
        (dict form) or no ``/version`` suffix (string form).
    """
    if spec.include is False:
        return []

    selectors = _parse_include(spec.include)

    rows: List[Any] = []
    if spec.kind in ("databases", "sources"):
        rows.extend(_source_rows("databases", config, config_dir, selectors, spec.as_dict))
    if spec.kind in ("remotes", "sources"):
        rows.extend(_source_rows("remotes", config, config_dir, selectors, spec.as_dict))
    return rows


#
# INTERNAL
#
def _normalize_entry(
        kind: str,
        value: Any,
        global_dict: bool,
        warnings_out: List[str]) -> Optional[ListingSpec]:
    """Normalize one ``expose.<kind>`` value into a ListingSpec (or None to skip).

    Args:
        kind: The listing kind ("databases", "remotes", or "sources").
        value: The raw config value (bool, str, list, or mapping).
        global_dict: The top-level ``dict`` setting used as the default.
        warnings_out: List to append warnings to.

    Returns:
        A ListingSpec, or None if the entry is disabled or invalid.
    """
    route = kind
    as_dict = global_dict
    include: Any = True

    if value is False:
        return None
    if value is True:
        pass
    elif isinstance(value, str):
        route = value
    elif isinstance(value, list):
        include = value
    elif isinstance(value, dict):
        route = value.get("route", kind)
        include = value.get("include", True)
        as_dict = bool(value.get("dict", global_dict))
    else:
        warnings_out.append(f"expose.{kind}: invalid value {type(value).__name__}; skipping")
        return None

    route = str(route).strip().lstrip("/")
    if not route:
        warnings_out.append(f"expose.{kind}: empty route; skipping")
        return None

    return ListingSpec(kind=kind, route=route, as_dict=as_dict, include=include)


def _add_collision_warnings(
        specs: List[ListingSpec],
        config: Dict[str, Any],
        config_dir: Optional[str],
        warnings_out: List[str]) -> None:
    """Warn when a listing route shadows a proxy or duplicates another listing.

    Args:
        specs: The resolved listing specs.
        config: Main configuration dictionary.
        config_dir: Directory holding the config files. If None, uses default.
        warnings_out: List to append warnings to.
    """
    proxy_names = (
        set(get_remote_names(config, config_dir)) | set(get_database_names(config, config_dir))
    )
    seen_routes: Dict[str, str] = {}

    for spec in specs:
        # Names may contain "/": the listing route shadows a name it equals or
        # one whose path starts with the route.
        for name in sorted(proxy_names):
            if name == spec.route or name.startswith(f"{spec.route}/") or \
                    spec.route.startswith(f"{name}/"):
                warnings_out.append(
                    f"expose.{spec.kind}: route '/{spec.route}' shadows the proxy "
                    f"for '{name}'"
                )
        if spec.route in seen_routes:
            warnings_out.append(
                f"expose: route '/{spec.route}' is used by both "
                f"'{seen_routes[spec.route]}' and '{spec.kind}'"
            )
        else:
            seen_routes[spec.route] = spec.kind


def _add_unknown_include_warnings(
        specs: List[ListingSpec],
        config: Dict[str, Any],
        config_dir: Optional[str],
        warnings_out: List[str]) -> None:
    """Warn when an ``include`` list names a database/remote that doesn't exist.

    Args:
        specs: The resolved listing specs.
        config: Main configuration dictionary.
        config_dir: Directory holding the config files. If None, uses default.
        warnings_out: List to append warnings to.
    """
    db_names = set(get_database_names(config, config_dir))
    remote_names = set(get_remote_names(config, config_dir))

    for spec in specs:
        if not isinstance(spec.include, list):
            continue
        if spec.kind == "databases":
            valid = db_names
        elif spec.kind == "remotes":
            valid = remote_names
        else:
            valid = db_names | remote_names

        for name in _parse_include(spec.include) or {}:
            if name not in valid:
                warnings_out.append(
                    f"expose.{spec.kind}: include names unknown '{name}'"
                )


def _parse_include(include: Any) -> Optional[Dict[str, Optional[List[Any]]]]:
    """Parse an ``include`` value into a name -> allowed-versions mapping.

    Args:
        include: True/False or a list of selectors. Each selector is a name
            string, or a single-key mapping ``{name: {versions: [...]}}``.

    Returns:
        None to include all names/versions, or a mapping from name to a list of
        allowed version specs (or None for all versions of that name).
    """
    if not isinstance(include, list):
        return None

    result: Dict[str, Optional[List[Any]]] = {}
    for item in include:
        if isinstance(item, str):
            result[item] = None
        elif isinstance(item, dict) and len(item) == 1:
            name, spec = next(iter(item.items()))
            if isinstance(spec, dict) and isinstance(spec.get("versions"), list):
                result[str(name)] = spec["versions"]
            else:
                result[str(name)] = None
    return result


def _source_rows(
        source_type: str,
        config: Dict[str, Any],
        config_dir: Optional[str],
        selectors: Optional[Dict[str, Optional[List[Any]]]],
        as_dict: bool) -> List[Any]:
    """Build listing rows for one source type (databases or remotes).

    Args:
        source_type: "databases" or "remotes".
        config: Main configuration dictionary.
        config_dir: Directory holding the config files. If None, uses default.
        selectors: Name -> allowed-versions mapping, or None for all.
        as_dict: Output format flag (see build_listing).

    Returns:
        List of row entries for the given source type.
    """
    if source_type == "databases":
        names = get_database_names(config, config_dir)

        def versions_of(name: str) -> List[str]:
            if not is_versioned_database(name, config_dir):
                return []
            return get_database_versions(name, config_dir)
    else:
        names = get_remote_names(config, config_dir)

        def versions_of(name: str) -> List[str]:
            if not is_versioned_remote(name, config, config_dir):
                return []
            return get_remote_versions(name, config, config_dir)

    rows: List[Any] = []
    for name in names:
        if selectors is not None and name not in selectors:
            continue
        allowed = selectors.get(name) if selectors is not None else None
        stems = versions_of(name)

        if not stems:
            # Unversioned source. Skip it if the config asked for specific versions.
            if allowed is None:
                rows.append(_row(name, None, as_dict))
            continue

        for stem in stems:
            if allowed is not None and not _version_selected(stem, allowed):
                continue
            rows.append(_row(name, stem, as_dict))
    return rows


def _row(name: str, version: Optional[str], as_dict: bool) -> Any:
    """Format a single listing row.

    Args:
        name: The model name.
        version: The version string, or None for unversioned sources.
        as_dict: Output format flag (see build_listing).

    Returns:
        A ``{"model", "version"}`` dict or a ``"model/version"`` string.
    """
    if as_dict:
        return {"model": name, "version": str(version) if version is not None else None}
    return f"{name}/{version}" if version is not None else name


def _version_selected(stem: str, allowed: List[Any]) -> bool:
    """Check whether a version stem matches any allowed version spec.

    Args:
        stem: A version stem from the filesystem (e.g. "2.4", "0.5.0").
        allowed: Allowed version specs from the config (str/int/float).

    Returns:
        True if the stem matches any allowed spec.
    """
    return any(versions_equal(stem, spec) for spec in allowed)

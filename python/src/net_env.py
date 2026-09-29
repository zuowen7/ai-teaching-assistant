"""Network environment hygiene shared by every entry point.

httpx parses the ``NO_PROXY`` bypass list when it builds a client, and it reads
each entry as ``host[:port]``.  A bracketed IPv6 entry such as ``[::1]`` — which
proxy tools commonly write — makes that parse raise::

    httpx.InvalidURL: Invalid port: ':1]'

The failure happens at client construction, so it takes down anything that opens
an HTTP client: the API's health probes, the literature providers, and the demo
CLI (which died with a traceback instead of a usable error).

Normalizing the list once at process start keeps the bypass semantics the user
asked for, minus the entries httpx cannot represent.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_PROXY_ENV_NAMES = ("NO_PROXY", "no_proxy")


def _entry_is_parsable(entry: str) -> bool:
    """Return whether httpx can build a bypass pattern from this entry."""

    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx is a hard dependency in practice
        return not (entry.startswith("[") and entry.endswith("]"))

    pattern = getattr(httpx, "URLPattern", None)
    if pattern is None:  # pragma: no cover - older httpx without the helper
        return not (entry.startswith("[") and entry.endswith("]"))
    try:
        pattern(entry)
    except Exception:
        return False
    return True


def normalize_proxy_env() -> dict[str, str]:
    """Drop ``NO_PROXY`` entries httpx cannot parse; return what was changed."""

    changed: dict[str, str] = {}
    for name in _PROXY_ENV_NAMES:
        raw = os.environ.get(name)
        if not raw:
            continue
        kept = [
            entry
            for entry in (item.strip() for item in raw.split(","))
            if entry and _entry_is_parsable(entry)
        ]
        normalized = ",".join(kept)
        if normalized != raw:
            os.environ[name] = normalized
            changed[name] = normalized
            logger.info(
                "%s normalized for httpx: kept %d of %d entries",
                name,
                len(kept),
                len(raw.split(",")),
            )
    return changed

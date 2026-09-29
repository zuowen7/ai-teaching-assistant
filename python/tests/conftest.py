"""Global test configuration.

Several development machines export a ``NO_PROXY`` list that contains the
bracketed IPv6 form ``[::1]``.  httpx parses every entry as ``host[:port]`` and
raises ``InvalidURL: Invalid port: ':1]'``, so any outbound probe fails for a
reason that has nothing to do with the code under test — for example the Ollama
health route and the cloud-edit SSE flow.

Normalizing the list here keeps those tests honest about the product instead of
about the local proxy setup.  Only entries that httpx cannot parse are dropped;
the rest of the user's bypass list is preserved.
"""

from __future__ import annotations

import os


def normalize_no_proxy(value: str) -> str:
    """Drop proxy-bypass entries httpx would parse as a host with a bad port."""

    kept: list[str] = []
    for item in value.split(","):
        entry = item.strip()
        if not entry:
            continue
        if entry.startswith("[") and entry.endswith("]"):
            continue
        kept.append(entry)
    return ",".join(kept)


def _apply() -> None:
    for name in ("NO_PROXY", "no_proxy"):
        raw = os.environ.get(name)
        if not raw:
            continue
        normalized = normalize_no_proxy(raw)
        if normalized != raw:
            os.environ[name] = normalized


_apply()

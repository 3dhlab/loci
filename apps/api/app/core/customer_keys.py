"""Resolve subscriber partitions from an explicit host allowlist.

Localhost uses the demo partition. CUSTOMER_KEYS_JSON adds deployment hosts;
unknown hosts fail closed. The reverse proxy must validate its incoming Host.
"""
from __future__ import annotations

import json
import os
from typing import Final


# Hard-coded allowlist. Adding a new built-in entry requires a code deploy
# (intentional — the canonical surfaces are listed in the repo so a code
# review captures any addition).
_BUILTIN_CUSTOMER_KEYS: Final[dict[str, str]] = {
    # Local development. Both forms supported because npm run dev binds
    # to 0.0.0.0 and the browser may resolve to either.
    "localhost": "demo",
    "127.0.0.1": "demo",
}


def _load_overrides() -> dict[str, str]:
    """Parse the CUSTOMER_KEYS_JSON env var into a host→key dict.

    Returns an empty dict if:
      - the env var is unset or whitespace-only
      - the env var is not valid JSON
      - the parsed value is not a JSON object (dict)

    Each entry is normalized: keys (hosts) are lowercased; non-string keys
    or values are dropped; empty strings on either side are dropped.

    Format example:
        CUSTOMER_KEYS_JSON='{"loci.museum-x.org": "museum-x"}'
    """
    raw = os.environ.get("CUSTOMER_KEYS_JSON", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    cleaned: dict[str, str] = {}
    for k, v in parsed.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        host = k.strip().lower()
        key = v.strip()
        if not host or not key:
            continue
        cleaned[host] = key
    return cleaned


# Merge order: built-ins LAST so they take precedence over env overrides for
# any host they declare. Ops can ADD partners via env but cannot reroute a
# canonical surface to a different partner key by mistake.
_CUSTOMER_KEYS: Final[dict[str, str]] = {
    **_load_overrides(),
    **_BUILTIN_CUSTOMER_KEYS,
}


def _normalize_host(raw: str) -> str:
    """Strip port + lowercase + trim whitespace from a Host header value."""
    host = raw.strip().lower()
    if ":" in host:
        host = host.split(":", 1)[0]
    return host


def resolve_customer_key(host: str | None) -> str | None:
    """Return the customer key for a given host, or None if not allowlisted.

    The endpoint that calls this MUST treat None as 400 Bad Request, never
    as a default-partition fallback. Silent partitioning of unknown hosts
    would create a spam bucket attackers can fill at will.
    """
    if not host:
        return None
    normalized = _normalize_host(host)
    if not normalized:
        return None
    return _CUSTOMER_KEYS.get(normalized)


def known_customer_keys() -> set[str]:
    """All customer keys currently configured (built-in + env overrides).

    Useful for admin tooling or smoke tests; not used by the request path.
    """
    return set(_CUSTOMER_KEYS.values())


# ----------------------------------------------------------------------------
# Display-name lookup — used by subscriber digest emails to render
# readable partner labels in subjects and bodies.
# ----------------------------------------------------------------------------

_DISPLAY_NAMES: Final[dict[str, str]] = {
    "demo": "Demo",
}


def customer_display_name(key: str) -> str:
    """Return a UI-friendly display name for a customer key.

    Falls back to title-cased form for unknown keys (e.g., "museum-x" →
    "Museum X"). Email subjects + bodies use this so subscribers see a
    properly-cased partner name, not the lowercased internal key.
    """
    key_norm = (key or "").strip().lower()
    if key_norm in _DISPLAY_NAMES:
        return _DISPLAY_NAMES[key_norm]
    return key_norm.replace("-", " ").replace("_", " ").title() or key

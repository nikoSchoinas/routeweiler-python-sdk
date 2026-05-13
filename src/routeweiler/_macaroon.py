"""Shared macaroon utility — deserialise and extract first-party caveats.

``pymacaroons`` is an optional dependency declared as a required dep in
``pyproject.toml``.  The import is deferred to each call site so that modules
which import this file load without error even if the package is absent in
unusual edge cases (e.g. minimal installs).
"""

from __future__ import annotations


def parse_macaroon_caveats(macaroon_b64: str) -> dict[str, str]:
    """Deserialise a base64 macaroon and return its first-party caveat key→value pairs.

    Returns an empty dict if pymacaroons is not installed or if deserialisation
    fails — callers must handle missing keys gracefully.
    """
    try:
        from pymacaroons import Macaroon  # type: ignore[import-untyped]  # noqa: PLC0415
    except ImportError:
        return {}

    try:
        m = Macaroon.deserialize(macaroon_b64)
    except Exception:
        # pymacaroons raises its own exception hierarchy; catch broadly since
        # this is optional third-party code.
        return {}

    caveats: dict[str, str] = {}
    for caveat in m.caveats:
        raw_id = caveat.caveat_id
        try:
            cid: str = raw_id.decode("utf-8") if isinstance(raw_id, bytes) else str(raw_id)
        except (UnicodeDecodeError, AttributeError):
            continue
        if "=" in cid:
            k, _, v = cid.partition("=")
            caveats[k.strip()] = v.strip()

    return caveats

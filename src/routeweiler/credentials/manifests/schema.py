"""Service-shape manifest schema — Pydantic models for split-URL recovery manifests."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import model_validator

from routeweiler._base import RouteweilerModel
from routeweiler._macaroon import parse_macaroon_caveats
from routeweiler.errors import ManifestParseError

HttpMethod = Literal["GET", "POST", "PUT", "DELETE", "PATCH"]

# "path" extracts from the URL path via regex.
# "macaroon" extracts a named first-party caveat from the L402 macaroon.
# Forward-compatible: "header:..." / "json:..." reserved for future use.
_KNOWN_EXTRACTOR_PREFIXES = {"path", "macaroon"}


class ServiceShapeStep(RouteweilerModel):
    """One step in a service-shape flow — maps a challenge path to a fulfilment path."""

    challenge_path: str
    fulfil_path_template: str
    id_extractor: str
    method: HttpMethod = "GET"

    @model_validator(mode="after")
    def _validate_id_extractor(self) -> ServiceShapeStep:
        if ":" not in self.id_extractor:
            raise ValueError(
                f"id_extractor must be in 'prefix:pattern' format, got {self.id_extractor!r}"
            )
        prefix, suffix = self.id_extractor.split(":", 1)
        if prefix not in _KNOWN_EXTRACTOR_PREFIXES:
            raise ManifestParseError(
                f"Unknown id_extractor prefix {prefix!r}. "
                f"Supported prefixes: {sorted(_KNOWN_EXTRACTOR_PREFIXES)}"
            )
        if prefix == "path":
            try:
                re.compile(suffix)
            except re.error as exc:
                raise ManifestParseError(
                    f"Invalid regex in id_extractor {self.id_extractor!r}: {exc}"
                ) from exc
        return self

    def extract_id(
        self, url_path: str, credential_payload: dict[str, Any] | None = None
    ) -> str | None:
        """Extract an ID from the URL path or credential payload depending on the prefix.

        ``path:<regex>`` — applies the regex to url_path; returns group(1) or None.
        ``macaroon:<caveat_key>`` — decodes the macaroon from credential_payload and
            returns the value of the named first-party caveat, or None.
        """
        prefix, suffix = self.id_extractor.split(":", 1)

        if prefix == "path":
            stripped = url_path.lstrip("/")
            match = re.search(suffix, stripped)
            if match:
                return match.group(1)
            return None

        if prefix == "macaroon":
            if not credential_payload:
                return None
            macaroon_b64 = credential_payload.get("macaroon")
            if not macaroon_b64 or not isinstance(macaroon_b64, str):
                return None
            caveats = parse_macaroon_caveats(macaroon_b64)
            return caveats.get(suffix)

        return None


class ServiceShape(RouteweilerModel):
    """A declared service shape — domain glob + ordered list of flow steps."""

    name: str
    domain_matches: str
    flow: list[ServiceShapeStep]

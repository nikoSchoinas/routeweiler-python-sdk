"""Service-shape manifest schema — Pydantic models for split-URL recovery manifests."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import model_validator

from routeweiler._base import RouteweilerModel
from routeweiler.errors import ManifestParseError

HttpMethod = Literal["GET", "POST", "PUT", "DELETE", "PATCH"]

# Only "path" is supported at MVP; forward-compatible with "header:..." / "json:..."
_KNOWN_EXTRACTOR_PREFIXES = {"path"}


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
        prefix, pattern = self.id_extractor.split(":", 1)
        if prefix not in _KNOWN_EXTRACTOR_PREFIXES:
            raise ManifestParseError(
                f"Unknown id_extractor prefix {prefix!r}. "
                f"Supported prefixes: {sorted(_KNOWN_EXTRACTOR_PREFIXES)}"
            )
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ManifestParseError(
                f"Invalid regex in id_extractor {self.id_extractor!r}: {exc}"
            ) from exc
        return self

    def extract_id(self, url_path: str) -> str | None:
        """Apply the id_extractor regex to url_path; return group(1) or None."""
        _, pattern = self.id_extractor.split(":", 1)
        stripped = url_path.lstrip("/")
        match = re.search(pattern, stripped)
        if match:
            return match.group(1)
        return None


class ServiceShape(RouteweilerModel):
    """A declared service shape — domain glob + ordered list of flow steps."""

    name: str
    domain_matches: str
    flow: list[ServiceShapeStep]

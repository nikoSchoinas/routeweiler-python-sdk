"""Service-shape manifest loader — builds a ManifestRegistry from bundled or user YAML files."""

from __future__ import annotations

import fnmatch
import urllib.parse
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from routewiler.credentials.manifests.schema import ServiceShape
from routewiler.errors import ManifestParseError


class ManifestRegistry:
    """Immutable collection of ServiceShape objects.

    Build via :meth:`from_bundled` (loads all ``*.yaml`` files packaged under
    ``routewiler.credentials.manifests``) or :meth:`from_paths` (user-supplied files).
    Combine both with ``ManifestRegistry.from_bundled() + ManifestRegistry.from_paths(...)``.

    Lookup is O(n) over shapes — at MVP only one bundled shape exists.
    """

    __slots__ = ("shapes",)

    def __init__(self, shapes: tuple[ServiceShape, ...]) -> None:
        self.shapes = shapes

    # ------------------------------------------------------------------
    # Constructors
    # ------------------------------------------------------------------

    @classmethod
    def from_bundled(cls) -> ManifestRegistry:
        """Load all ``*.yaml`` manifests bundled inside ``routewiler.credentials.manifests``."""
        pkg = files("routewiler.credentials.manifests")
        shapes: list[ServiceShape] = []
        for resource in pkg.iterdir():
            if resource.name.endswith(".yaml") or resource.name.endswith(".yml"):
                raw = resource.read_text(encoding="utf-8")
                shapes.append(_parse_manifest(raw, source=resource.name))
        return cls(tuple(shapes))

    @classmethod
    def from_paths(cls, paths: list[Path]) -> ManifestRegistry:
        """Load manifests from explicit filesystem paths."""
        shapes: list[ServiceShape] = []
        for path in paths:
            raw = path.read_text(encoding="utf-8")
            shapes.append(_parse_manifest(raw, source=str(path)))
        return cls(tuple(shapes))

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def lookup(self, url: str) -> ServiceShape | None:
        """Return the first ServiceShape whose domain_matches glob matches the URL's host.

        Matching is done against ``netloc`` (host + optional port).  A plain
        hostname like ``"mock"`` matches ``"mock"`` exactly; ``"*.example.com"``
        matches ``"api.example.com"`` but not ``"example.com"``.
        """
        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc  # e.g. "api.refinedelement.com" or "mock"
        for shape in self.shapes:
            if fnmatch.fnmatchcase(host, shape.domain_matches):
                return shape
        return None


# ------------------------------------------------------------------
# Private helpers
# ------------------------------------------------------------------


def _parse_manifest(raw_yaml: str, *, source: str) -> ServiceShape:
    """Parse a YAML string into a ServiceShape; raise ManifestParseError on any failure."""
    try:
        data: Any = yaml.safe_load(raw_yaml)
    except yaml.YAMLError as exc:
        raise ManifestParseError(f"Invalid YAML in manifest {source!r}: {exc}") from exc

    if not isinstance(data, dict):
        raise ManifestParseError(
            f"Manifest {source!r} must be a YAML mapping at the top level, "
            f"got {type(data).__name__}"
        )

    try:
        return ServiceShape.model_validate(data)
    except (ValidationError, ManifestParseError) as exc:
        raise ManifestParseError(
            f"Schema validation failed for manifest {source!r}: {exc}"
        ) from exc

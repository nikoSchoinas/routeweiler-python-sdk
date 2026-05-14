"""Service-shape manifest loader — builds a ManifestRegistry from ServiceShape objects."""

from __future__ import annotations

import fnmatch
import urllib.parse

from routeweiler.credentials.manifests._bundled import BUNDLED_SHAPES
from routeweiler.credentials.manifests.schema import ServiceShape


class ManifestRegistry:
    """Immutable collection of ServiceShape objects.

    Build via :meth:`from_bundled` (returns canonical shapes shipped with Routeweiler)
    or pass shapes directly: ``ManifestRegistry(shapes=(ServiceShape(...),))``.

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
        """Return a registry containing all canonical shapes shipped with Routeweiler."""
        return cls(BUNDLED_SHAPES)

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

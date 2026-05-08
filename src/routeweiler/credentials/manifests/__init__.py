"""Service-shape manifests — schema, loader, and bundled YAML files for split-URL recovery."""

from routeweiler.credentials.manifests.loader import ManifestRegistry
from routeweiler.credentials.manifests.schema import ServiceShape, ServiceShapeStep

__all__ = [
    "ManifestRegistry",
    "ServiceShape",
    "ServiceShapeStep",
]

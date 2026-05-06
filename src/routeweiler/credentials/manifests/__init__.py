"""Service-shape manifests — schema, loader, and bundled YAML files for §9.3 split-URL recovery."""

from routeweiler.credentials.manifests.loader import ManifestRegistry
from routeweiler.credentials.manifests.schema import ServiceShape, ServiceShapeStep

__all__ = [
    "ManifestRegistry",
    "ServiceShape",
    "ServiceShapeStep",
]

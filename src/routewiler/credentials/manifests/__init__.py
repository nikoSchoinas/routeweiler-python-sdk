"""Service-shape manifests — schema, loader, and bundled YAML files for §9.3 split-URL recovery."""

from routewiler.credentials.manifests.loader import ManifestRegistry
from routewiler.credentials.manifests.schema import ServiceShape, ServiceShapeStep

__all__ = [
    "ManifestRegistry",
    "ServiceShape",
    "ServiceShapeStep",
]

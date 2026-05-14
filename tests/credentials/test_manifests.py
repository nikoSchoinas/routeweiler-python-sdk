"""Unit tests for the service-shape manifest schema and ManifestRegistry."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from routeweiler.credentials.manifests.loader import ManifestRegistry
from routeweiler.credentials.manifests.schema import ServiceShape, ServiceShapeStep
from routeweiler.errors import ManifestParseError


def _shape(name: str = "shop", domain: str = "*.example.com") -> ServiceShape:
    return ServiceShape(name=name, domain_matches=domain, flow=[])


# ---------------------------------------------------------------------------
# ManifestRegistry — construction and lookup
# ---------------------------------------------------------------------------


def test_registry_accepts_empty_shapes() -> None:
    registry = ManifestRegistry(shapes=())
    assert registry.lookup("https://api.example.com/path") is None


def test_lookup_matches_wildcard_subdomain() -> None:
    registry = ManifestRegistry(shapes=(_shape(domain="*.refinedelement.com"),))
    shape = registry.lookup("https://api.refinedelement.com/checkout/abc")
    assert shape is not None
    assert shape.name == "shop"


def test_lookup_does_not_match_root_domain() -> None:
    registry = ManifestRegistry(shapes=(_shape(domain="*.refinedelement.com"),))
    assert registry.lookup("https://refinedelement.com/checkout/abc") is None


def test_lookup_does_not_match_different_domain() -> None:
    registry = ManifestRegistry(shapes=(_shape(domain="*.refinedelement.com"),))
    assert registry.lookup("https://api.other.com/checkout/abc") is None


def test_lookup_returns_first_matching_shape() -> None:
    first = ServiceShape(name="first", domain_matches="*.example.com", flow=[])
    second = ServiceShape(name="second", domain_matches="*.example.com", flow=[])
    registry = ManifestRegistry(shapes=(first, second))
    shape = registry.lookup("https://api.example.com/path")
    assert shape is not None
    assert shape.name == "first"


def test_from_bundled_returns_non_empty_registry() -> None:
    registry = ManifestRegistry.from_bundled()
    assert len(registry.shapes) >= 1


# ---------------------------------------------------------------------------
# ServiceShapeStep — id extractor
# ---------------------------------------------------------------------------


def test_id_extractor_extracts_order_id() -> None:
    step = ServiceShapeStep(
        challenge_path="/checkout/*",
        fulfil_path_template="/orders/{order_id}/fulfil",
        id_extractor="path:checkout/([^/]+)",
    )
    assert step.extract_id("/checkout/order_123") == "order_123"


def test_id_extractor_returns_none_for_non_matching_path() -> None:
    step = ServiceShapeStep(
        challenge_path="/checkout/*",
        fulfil_path_template="/orders/{order_id}/fulfil",
        id_extractor="path:checkout/([^/]+)",
    )
    assert step.extract_id("/other/path") is None


def test_id_extractor_strips_leading_slash() -> None:
    step = ServiceShapeStep(
        challenge_path="/checkout/*",
        fulfil_path_template="/orders/{order_id}/fulfil",
        id_extractor="path:checkout/([^/]+)",
    )
    assert step.extract_id("checkout/order_abc") == "order_abc"


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


def test_unknown_extractor_prefix_rejected() -> None:
    with pytest.raises((ValueError, ManifestParseError)):
        ServiceShapeStep(
            challenge_path="/checkout/*",
            fulfil_path_template="/orders/{order_id}/fulfil",
            id_extractor="header:X-Order-Id",
        )


def test_missing_extractor_prefix_separator_rejected() -> None:
    with pytest.raises((ValueError, ManifestParseError)):
        ServiceShapeStep(
            challenge_path="/checkout/*",
            fulfil_path_template="/orders/{order_id}/fulfil",
            id_extractor="noprefix",
        )


def test_invalid_regex_in_extractor_rejected() -> None:
    with pytest.raises((ValueError, ManifestParseError)):
        ServiceShapeStep(
            challenge_path="/checkout/*",
            fulfil_path_template="/orders/{order_id}/fulfil",
            id_extractor="path:([unclosed",
        )


def test_schema_validation_fails_for_missing_flow_field() -> None:
    with pytest.raises(ValidationError):
        ServiceShape.model_validate({"name": "shop", "domain_matches": "*.shop.com"})


def test_service_shape_default_method_is_get() -> None:
    step = ServiceShapeStep(
        challenge_path="/buy/*",
        fulfil_path_template="/fulfil/{order_id}",
        id_extractor="path:buy/([^/]+)",
    )
    assert step.method == "GET"

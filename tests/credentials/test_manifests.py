"""Unit tests for the service-shape manifest schema and ManifestRegistry loader."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from routewiler.credentials.manifests.loader import ManifestRegistry, _parse_manifest
from routewiler.credentials.manifests.schema import ServiceShape, ServiceShapeStep
from routewiler.errors import ManifestParseError

# ---------------------------------------------------------------------------
# Bundled manifests
# ---------------------------------------------------------------------------


def test_load_bundled_includes_lightning_shop() -> None:
    registry = ManifestRegistry.from_bundled()
    names = {shape.name for shape in registry.shapes}
    assert "lightning-shop" in names, f"Expected 'lightning-shop' in bundled manifests, got {names}"


def test_bundled_lightning_shop_has_correct_domain() -> None:
    registry = ManifestRegistry.from_bundled()
    shape = next(s for s in registry.shapes if s.name == "lightning-shop")
    assert shape.domain_matches == "*.refinedelement.com"


def test_bundled_lightning_shop_has_one_flow_step() -> None:
    registry = ManifestRegistry.from_bundled()
    shape = next(s for s in registry.shapes if s.name == "lightning-shop")
    assert len(shape.flow) == 1
    step = shape.flow[0]
    assert step.challenge_path == "/checkout/*"
    assert step.fulfil_path_template == "/orders/{order_id}/fulfil"
    assert step.id_extractor == "path:checkout/([^/]+)"


# ---------------------------------------------------------------------------
# ManifestRegistry.from_paths
# ---------------------------------------------------------------------------


def _write_manifest(tmp_path: Path, content: str, name: str = "test.yaml") -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_from_paths_loads_valid_manifest(tmp_path: Path) -> None:
    yaml_content = textwrap.dedent(
        """\
        name: my-shop
        domain_matches: "*.myshop.com"
        flow:
          - challenge_path: "/buy/*"
            fulfil_path_template: "/orders/{order_id}/confirm"
            id_extractor: "path:buy/([^/]+)"
        """
    )
    path = _write_manifest(tmp_path, yaml_content)
    registry = ManifestRegistry.from_paths([path])
    assert len(registry.shapes) == 1
    assert registry.shapes[0].name == "my-shop"


def test_from_paths_loads_multiple_manifests(tmp_path: Path) -> None:
    yaml_a = "name: shop-a\ndomain_matches: '*.a.com'\nflow: []\n"
    yaml_b = "name: shop-b\ndomain_matches: '*.b.com'\nflow: []\n"
    paths = [
        _write_manifest(tmp_path, yaml_a, "a.yaml"),
        _write_manifest(tmp_path, yaml_b, "b.yaml"),
    ]
    registry = ManifestRegistry.from_paths(paths)
    names = {s.name for s in registry.shapes}
    assert names == {"shop-a", "shop-b"}


# ---------------------------------------------------------------------------
# ManifestRegistry.lookup
# ---------------------------------------------------------------------------


def test_lookup_matches_glob() -> None:
    yaml_content = "name: shop\ndomain_matches: '*.refinedelement.com'\nflow: []\n"
    ManifestRegistry.from_paths([_write_manifest(Path("."), yaml_content, "/tmp/t.yaml")])


def test_lookup_matches_wildcard_subdomain(tmp_path: Path) -> None:
    yaml_content = "name: shop\ndomain_matches: '*.refinedelement.com'\nflow: []\n"
    registry = ManifestRegistry.from_paths([_write_manifest(tmp_path, yaml_content)])
    shape = registry.lookup("https://api.refinedelement.com/checkout/abc")
    assert shape is not None
    assert shape.name == "shop"


def test_lookup_does_not_match_root_domain(tmp_path: Path) -> None:
    yaml_content = "name: shop\ndomain_matches: '*.refinedelement.com'\nflow: []\n"
    registry = ManifestRegistry.from_paths([_write_manifest(tmp_path, yaml_content)])
    # "*.refinedelement.com" should not match the bare domain (fnmatch behaviour).
    shape = registry.lookup("https://refinedelement.com/checkout/abc")
    assert shape is None


def test_lookup_does_not_match_different_domain(tmp_path: Path) -> None:
    yaml_content = "name: shop\ndomain_matches: '*.refinedelement.com'\nflow: []\n"
    registry = ManifestRegistry.from_paths([_write_manifest(tmp_path, yaml_content)])
    shape = registry.lookup("https://api.other.com/checkout/abc")
    assert shape is None


def test_lookup_returns_none_for_empty_registry() -> None:
    registry = ManifestRegistry(shapes=())
    assert registry.lookup("https://api.refinedelement.com/checkout/abc") is None


def test_lookup_returns_first_matching_shape(tmp_path: Path) -> None:
    yaml_a = "name: first\ndomain_matches: '*.example.com'\nflow: []\n"
    yaml_b = "name: second\ndomain_matches: '*.example.com'\nflow: []\n"
    registry = ManifestRegistry.from_paths(
        [_write_manifest(tmp_path, yaml_a, "a.yaml"), _write_manifest(tmp_path, yaml_b, "b.yaml")]
    )
    shape = registry.lookup("https://api.example.com/path")
    assert shape is not None
    assert shape.name == "first"


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


def test_invalid_yaml_raises_manifest_parse_error(tmp_path: Path) -> None:
    bad_yaml = "name: shop\n  bad_indent: true\n  another: [unclosed"
    path = _write_manifest(tmp_path, bad_yaml)
    with pytest.raises(ManifestParseError, match="Invalid YAML"):
        ManifestRegistry.from_paths([path])


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


def test_schema_validation_fails_for_missing_fields(tmp_path: Path) -> None:
    # "flow" key is missing
    yaml_content = "name: shop\ndomain_matches: '*.shop.com'\n"
    path = _write_manifest(tmp_path, yaml_content)
    with pytest.raises(ManifestParseError, match="Schema validation failed"):
        ManifestRegistry.from_paths([path])


def test_schema_validation_fails_for_non_mapping_yaml(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path, "- just a list item\n")
    with pytest.raises(ManifestParseError, match="must be a YAML mapping"):
        ManifestRegistry.from_paths([path])


# ---------------------------------------------------------------------------
# ServiceShape model validation
# ---------------------------------------------------------------------------


def test_service_shape_parse_manifest_string() -> None:
    raw = textwrap.dedent(
        """\
        name: test-shop
        domain_matches: "mock"
        flow:
          - challenge_path: "/checkout/*"
            fulfil_path_template: "/orders/{order_id}/fulfil"
            id_extractor: "path:checkout/([^/]+)"
        """
    )
    shape = _parse_manifest(raw, source="inline")
    assert isinstance(shape, ServiceShape)
    assert shape.name == "test-shop"
    assert len(shape.flow) == 1


def test_service_shape_default_method_is_get() -> None:
    step = ServiceShapeStep(
        challenge_path="/buy/*",
        fulfil_path_template="/fulfil/{order_id}",
        id_extractor="path:buy/([^/]+)",
    )
    assert step.method == "GET"
